"""
Everything ZBox does to get mail onto the screen: fetching a folder,
showing the offline cache first, unified-folder assembly, background
body prefetch, IDLE reactions, and the auto-refresh scheduling that
decides when any of it is allowed to run.

Split out of main_frame.py (audit item 11, stage 3). This is a
MIXIN, not the MailController the audit asked for, and the
difference is deliberate. A controller means every "self.mail_panel"
becoming "self.frame.mail_panel" across six hundred lines that share
mutable state -- _envelope_request_id, _known_envelope_ids,
_offline_cache_last_sync -- with three worker threads, and there is
no test coverage of any of it. A missed reference there is an
AttributeError in the one path that matters most, and it would show
up as mail not appearing.

As a mixin the method bodies moved byte for byte: "self" still means
the frame, the worker lanes are untouched, and the ordering is
identical. main_frame.py gets smaller and this logic becomes
readable on its own, which was the actual complaint. Converting it
into a real controller is worth doing once this path has tests --
and this file is the thing that makes those tests possible to write.

Three worker lanes matter here and should not be conflated:
self.worker is interactive and must never queue behind bulk work;
self.sync_worker is low priority (offline cache top-up, prefetch,
blocklist); self.new_mail_worker handles mail that has only just
arrived, so it does not wait behind a sweep already in progress.
"""

import logging
import os
import time

import wx

import himalaya_client
import junk_rules
import lang
from envelope_format import (
    _envelope_is_unread,
    _folder_display_to_himalaya,
    _junk_folder_for_account,
    _parse_envelope_date,
)


# Folders synced up front by File > Offline > Synchronize Now, and
# treated as worth keeping fresh in the unified views. Any other
# folder gets a local copy the first time it is opened while online
# (see _fetch_account_envelopes), so offline mode is no longer limited
# to these three -- which is what made it a dead end for Junk, Trash
# and Archive.
_OFFLINE_SYNCED_FOLDERS = ("INBOX", "Sent", "Drafts")

# Minimum seconds between background offline-cache top-ups for the
# same (account, folder), so a folder left open through many silent
# refreshes is not re-downloaded on every tick.
_OFFLINE_CACHE_MIN_INTERVAL = 300

# How long after the reader last moved through the message list the
# list counts as quiet again. While it is not, background work waits
# (sync_worker's pause_while, silent repaints) and so does the
# preview, so arrowing never competes with anything. New mail is still
# detected and heard; only its repaint waits.
_LIST_INTERACTION_QUIET_SECONDS = 0.5

# Floor between auto-refreshes, whatever asks for one.
_AUTO_REFRESH_MIN_INTERVAL_SECONDS = 5.0

# Pause after a tab closes before the catch-up refresh, so returning
# to the list is not immediately followed by it moving.
_TAB_CLOSE_REFRESH_DELAY_SECONDS = 2.0


class MailFetchMixin:
    """Mixed into ZBoxMainFrame. Every method here runs with the
    frame as self."""

    # Refresh-calm state. Declared at class level so the attributes
    # exist before __init__ runs; each instance writes floats or None,
    # never a shared mutable value.
    _list_last_interaction = 0.0
    _auto_refresh_last_run = 0.0
    _tab_close_refresh_timer = None
    # What the message list is currently showing, as (account_id or
    # unified type, folder). Used to tell "the reader picked a
    # different folder" from "the same folder is being fetched again"
    # -- the second must not blank the list first.
    _displayed_target = None

    # Audit finding 11: paging state for the folder currently on
    # screen. Reset to page 1 on every fresh fetch (folder open,
    # manual refresh, silent auto-refresh); _envelope_has_more is a
    # guess based on whether the last page came back full, not a
    # real server-reported total, so it can be wrong by at most one
    # empty "Load More" round trip.
    _ENVELOPE_PAGE_SIZE = 200
    _envelope_current_page = 1
    _envelope_has_more = False

    def _fetch_account_envelopes(self, account_id, folder, request_id, silent=False):
        account = self._find_account(account_id)
        if account is None:
            return

        backend = "maildir" if self.work_offline else "imap"
        if silent and backend == "imap" and not self._background_allowed(account):
            # Automatic refresh of an account whose automatic checking
            # is off or backing off: its offline copy, or nothing.
            if not self._has_offline_copy(account, folder):
                return
            backend = "maildir"
        # Local-cache-first: for a folder ZBox keeps an offline copy
        # of, show that copy immediately (a local subprocess call,
        # not a network round trip) instead of sitting on "Loading
        # messages..." until the live IMAP fetch responds -- the same
        # "show what's already known, then update" order Thunderbird
        # and most other mail clients use. Only for a real folder
        # open, not the silent 20-second poll (which has something on
        # screen already) and not while already working offline
        # (where the live fetch below already targets the local copy
        # directly, so there's nothing extra to preview).
        target = (account_id, folder)
        # Re-fetching what is already on screen -- a manual refresh, or
        # toggling Work Offline, which re-runs the current selection.
        # Blanking the list in that case makes it vanish and come back
        # for no reason, which for a screen reader reader is not a
        # flicker but a loss of place.
        same_target = (
            target == self._displayed_target
            and self.mail_panel.envelope_panel.has_content()
        )
        # Same reasoning extended to every folder switch, not only a
        # re-fetch of the same one: if the list already has real
        # content on it, a slow account gate (another interactive call
        # holding it, a queued fetch, whatever) must not blank the
        # list while that plays out. Only a panel with nothing useful
        # showing yet -- first load, or an empty/placeholder state --
        # gets "Loading..." at all.
        has_existing_content = self.mail_panel.envelope_panel.has_content()

        showed_cache = False
        if not silent and not self.work_offline and not same_target:
            # Any folder with a local copy, not only Inbox/Sent/Drafts:
            # folders are now synced on demand when opened, so any of
            # them may have one. _show_cached_envelopes_first returns
            # False when there is no copy, so this stays a no-op for
            # folders never visited.
            showed_cache = self._show_cached_envelopes_first(account, folder, request_id)

        if not silent and not showed_cache and not has_existing_content:
            self.mail_panel.envelope_panel.show_placeholder("Loading...")
        if not silent:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "loading_folder",
                default=f"Loading {folder} for {account.identity_email}...",
                folder=folder, account=account.identity_email,
            ))

        def work():
            if request_id != self._envelope_request_id:
                # Superseded by a later folder switch before this job
                # even reached the front of the worker queue -- skip
                # the subprocess/IMAP round trip entirely instead of
                # spending real time on a result nobody will see.
                # self.worker (himalaya_worker.py) is a single serial
                # queue, so without this a burst of folder switches
                # (e.g. Unified Inbox -> a folder -> back to Unified
                # Inbox, which alone re-queues one job per account)
                # piles up real network calls ahead of the fetch that
                # actually matters -- which is what left the list on
                # "Loading..." for a long stretch after returning to
                # Unified Inbox.
                return None
            return himalaya_client.list_envelopes(self.paths, account, folder=folder, backend=backend)

        def on_success(envelopes):
            if backend == "imap" and envelopes is not None:
                self._note_server_result(account)
            if request_id != self._envelope_request_id:
                return  # a newer selection has since been made
            for envelope in envelopes:
                envelope["_zbox_backend"] = backend
            # A fresh fetch always replaces page 1 -- Load More (see
            # _on_load_more_messages) is the only path that advances
            # past it, and it manages this state itself.
            self._envelope_current_page = 1
            self._envelope_has_more = len(envelopes) == self._ENVELOPE_PAGE_SIZE
            if silent:
                # Background auto-refresh: only touch the list when
                # content actually changed, so an idle poll never
                # resets the selection or scroll position -- and only
                # once the reader has stopped arrowing.
                started = time.monotonic()

                def repaint():
                    if request_id != self._envelope_request_id:
                        return
                    changed = self.mail_panel.envelope_panel.populate_if_changed(envelopes)
                    if changed:
                        logging.getLogger("zbox.main").info(
                            "List updated for %s/%s, %.1fs after the check returned.",
                            account.account_id, folder, time.monotonic() - started,
                        )
                        self.GetStatusBar().SetStatusText(lang.t(
                            "main_ui", "messages_in_folder",
                            default=f"{len(envelopes)} message(s) in {folder}.",
                            count=len(envelopes), folder=folder,
                        ))

                self._when_list_quiet(repaint)
            elif not envelopes and self.work_offline and not self._has_offline_copy(account, folder):
                # Offline with nothing synced for this folder. An empty
                # list here looks like a broken folder; it is simply a
                # folder that was never visited while online, and
                # saying so is the difference between a dead end and
                # an instruction.
                self.mail_panel.envelope_panel.show_placeholder(
                    lang.t('dialogs', 'no_offline_copy', default="No offline copy of %s. Open it while online, or use "
                    "File, Offline, Synchronize Now.") % folder
                )
                self._displayed_target = target
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "no_offline_copy",
                    default="No offline copy of %s yet." % folder, folder=folder,
                ))
            else:
                if showed_cache or same_target:
                    # Something correct is already on screen; only
                    # repaint if the live result actually differs, so
                    # an unchanged folder doesn't flicker or lose the
                    # reading position.
                    self.mail_panel.envelope_panel.populate_if_changed(envelopes)
                else:
                    self.mail_panel.envelope_panel.populate(envelopes)
                self._displayed_target = target
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "messages_in_folder",
                    default=f"{len(envelopes)} message(s) in {folder}.",
                    count=len(envelopes), folder=folder,
                ))
                self._maybe_apply_initial_focus()

            if backend == "imap":
                # Sync on demand: a folder you have actually opened
                # while online becomes available offline. This is what
                # _on_sync_offline's docstring already promised and
                # nothing implemented, which is why Work Offline had
                # content for three folders and an empty list for the
                # rest. Throttled per folder and capped, so opening
                # Gmail's All Mail does not download a mailbox.
                self._refresh_offline_cache_quietly(account, folder)

            self._prefetch_message_bodies(account, folder, envelopes, backend, request_id)

        def on_error(exc):
            if backend == "imap":
                self._note_server_result(account, exc)
            if request_id != self._envelope_request_id:
                return
            if silent:
                return  # background polls fail quietly; manual refresh shows errors
            if showed_cache:
                # Already showing the last synced copy; a failed live
                # refresh (e.g. no network right now) just means that
                # copy stays on screen instead of being wiped out to
                # an error placeholder.
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "refresh_failed_cached",
                    default=f"Could not refresh {folder}: showing last synced copy.",
                    folder=folder,
                ))
            elif himalaya_client.is_missing_folder_error(exc):
                # Not a failure -- this account's server never has a
                # mailbox for one of ZBox's generic folder names (e.g.
                # disroot.org accounts have no Archive folder at all).
                # A raw "IMAP SELECT failed: NO Mailbox doesn't exist:
                # Archive (0.133 + 0.000 + 0.132 secs)." is unreadable
                # for a screen reader and looks like a bug; say plainly
                # that the folder isn't there instead.
                self.mail_panel.envelope_panel.show_placeholder(
                    f"This account has no {folder} folder."
                )
                self._displayed_target = target
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "no_such_folder",
                    default=f"No {folder} folder for this account.", folder=folder,
                ))
            else:
                self.mail_panel.envelope_panel.show_placeholder(f"Could not load messages: {exc}")
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "load_failed",
                    default="Failed to load messages. See logs for details.",
                ))

        self.lanes.for_read(account.account_id, backend).submit(work, on_success, on_error)

    def _on_load_more_messages(self, event=None):
        """
        Audit finding 11: fetches the next page_size block of the
        folder currently on screen and appends it below what is
        already there, instead of the list being permanently capped
        at the first page_size messages with no way past it.

        Deliberately its own small fetch rather than routed through
        _fetch_account_envelopes above: that method's job is "show
        the right thing for a folder open" (offline-cache preview,
        placeholders, offline-cache top-up, prefetch) and none of
        that applies to appending one more page to a list that is
        already showing correctly.
        """
        target = self._displayed_target
        if target is None:
            return
        if not self._envelope_has_more:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "no_more_to_load",
                default="No more messages to load in this folder.",
            ))
            return
        account_id, folder = target
        account = self._find_account(account_id)
        if account is None:
            return

        backend = "maildir" if self.work_offline else "imap"
        next_page = self._envelope_current_page + 1
        request_id = self._envelope_request_id
        page_size = self._ENVELOPE_PAGE_SIZE

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "loading_more",
            default=f"Loading more messages in {folder}...", folder=folder,
        ))

        def work():
            return himalaya_client.list_envelopes(
                self.paths, account, folder=folder,
                page_size=page_size, page=next_page, backend=backend,
            )

        def on_success(envelopes):
            if request_id != self._envelope_request_id or target != self._displayed_target:
                return  # the folder changed while this was in flight
            for envelope in envelopes:
                envelope["_zbox_backend"] = backend
            self._envelope_current_page = next_page
            self._envelope_has_more = len(envelopes) == page_size
            if envelopes:
                self.mail_panel.envelope_panel.append(envelopes)
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "more_loaded",
                    default=f"{len(envelopes)} more message(s) loaded.",
                    count=len(envelopes),
                ))
            else:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "no_more_messages",
                    default="No more messages in this folder.",
                ))

        def on_error(exc):
            if request_id != self._envelope_request_id:
                return
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "load_more_failed",
                default=f"Could not load more messages: {exc}", error=exc,
            ))

        self.lanes.for_read(account.account_id, backend).submit(work, on_success, on_error)

    def _show_cached_envelopes_first(self, account, folder, request_id):
        """
        Populates the list from the local offline Maildir copy right
        away, if one exists, while the live IMAP fetch for the same
        folder runs in the background. Returns True if a sync has
        ever happened for this folder (so the caller knows a cached
        preview is on the way and can skip the "Loading..."
        placeholder), regardless of whether that preview turns out
        empty.
        """
        offline_folder_dir = os.path.join(
            self.paths.cache_dir(account.account_id), "offline_mail", folder
        )
        if not os.path.isdir(offline_folder_dir):
            return False  # never synced -- nothing to preview

        def work():
            if request_id != self._envelope_request_id:
                return None  # see the matching comment in _fetch_account_envelopes
            return himalaya_client.list_envelopes(self.paths, account, folder=folder, backend="maildir")

        def on_success(envelopes):
            if request_id != self._envelope_request_id:
                return
            # None when work() found the request superseded. The
            # guard above catches that case today, but only because
            # both read the same counter; a None here must not be an
            # exception either way. See the unified variant below for
            # what that failure actually cost.
            if not envelopes:
                return
            for envelope in envelopes:
                envelope["_zbox_backend"] = "maildir"
            self.mail_panel.envelope_panel.populate(envelopes)
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "messages_in_folder_cached",
                default=f"{len(envelopes)} message(s) in {folder} (last synced copy, updating...)",
                count=len(envelopes), folder=folder,
            ))
            self._maybe_apply_initial_focus()

        def on_error(exc):
            pass  # the live fetch's own placeholder/result takes over as usual

        self.lanes.local.submit(work, on_success, on_error)
        return True

    def _has_offline_copy(self, account, folder):
        """True if this folder has ever been synced for offline use."""
        return os.path.isdir(os.path.join(
            self.paths.cache_dir(account.account_id), "offline_mail", folder
        ))

    def _refresh_offline_cache_quietly(self, account, folder, limit=30):
        """
        Tops up the local offline copy of 'folder' after a successful
        live fetch, throttled to once per _OFFLINE_CACHE_MIN_INTERVAL
        per (account, folder) so a folder left open through repeated
        20-second silent auto-refreshes doesn't re-check on every
        tick. Runs on sync_worker, never the interactive worker, and
        never surfaces errors -- keeping the offline copy fresh is a
        courtesy, not something a flaky connection should interrupt
        the person over.

        limit defaults lower here (30) than the manual "Synchronize
        for Offline" action's 100: sync_folder_offline skips messages
        it already has, so repeat calls are cheap regardless, but the
        very first call for a brand-new account still downloads
        everything within 'limit' in one go, and there's no need for
        that first automatic top-up to be as large as an explicit,
        user-requested sync.
        """
        key = (account.account_id, folder)
        now = time.monotonic()
        if not self._background_allowed(account):
            return
        last = self._offline_cache_last_sync.get(key)
        if last is not None and (now - last) < _OFFLINE_CACHE_MIN_INTERVAL:
            return
        if self._current_message_tab() is not None:
            # Don't download message bodies while the user is reading
            # one -- leave the account to the interactive reads. No
            # cooldown stamp is set, so this retries on a later poll.
            return
        self._offline_cache_last_sync[key] = now

        def work():
            # Background lane: stands down (HimalayaBackgroundSkipped)
            # if interactive work for this account is active or waiting.
            with himalaya_client.background_priority():
                himalaya_client.sync_folder_offline(
                    self.paths, account, folder, limit=limit,
                    max_age_days=self.settings_manager.settings.cache_max_message_age_days,
                )

        def on_error(exc):
            if isinstance(exc, himalaya_client.HimalayaBackgroundSkipped):
                return  # interactive work preempted this round; retried next poll
            self._note_server_result(account, exc)
            logging.getLogger("zbox.main").debug(
                "Quiet offline-cache refresh failed for %s/%s: %s", account.account_id, folder, exc
            )

        self.sync_worker.submit(work, lambda _result: None, on_error)

    def _fetch_unified_envelopes(self, folder_type, request_id, silent=False):
        accounts = self.account_manager.enabled_accounts()
        if not accounts:
            if not silent:
                if self.account_manager.accounts:
                    # Configured, but every one of them is switched
                    # off. Saying "no accounts configured" here would
                    # send someone looking for a setup problem that
                    # does not exist.
                    self.mail_panel.envelope_panel.show_placeholder(
                        lang.t('dialogs', 'envlist_all_disabled', default="Every account is disabled.")
                    )
                else:
                    self.mail_panel.envelope_panel.show_placeholder(
                        lang.t('dialogs', 'envlist_no_accounts', default="No accounts configured yet.")
                    )
            return

        # Each account can have its own real folder name for the same
        # generic folder_type (Gmail's Sent/Archive/etc are named
        # differently from everyone else's) -- see
        # _folder_display_to_himalaya. account_folders holds each
        # account's resolved name so every use below (fetch, offline
        # cache dir, cache-quietly refresh) targets the right real
        # folder per account instead of assuming one name fits all.
        account_folders = {
            account.account_id: _folder_display_to_himalaya(folder_type, account)
            for account in accounts
        }
        backend = "maildir" if self.work_offline else "imap"
        # Per account: an automatic refresh reads the offline copy of
        # an account whose automatic checking is off or backing off,
        # so its rows stay and its server is not contacted.
        account_backends = {}
        for account in accounts:
            account_backend = backend
            if silent and account_backend == "imap" and not self._background_allowed(account):
                account_backend = "maildir"
            account_backends[account.account_id] = account_backend

        target = ("unified", folder_type)
        same_target = (
            target == self._displayed_target
            and self.mail_panel.envelope_panel.has_content()
        )
        # See the matching comment in _fetch_account_envelopes: a
        # panel that already has real content stays up through any
        # folder switch, not only a re-fetch of the same one.
        has_existing_content = self.mail_panel.envelope_panel.has_content()

        showed_cache = False
        if not silent and not self.work_offline and not same_target:
            showed_cache = self._show_cached_unified_envelopes_first(
                accounts, account_folders, folder_type, request_id
            )

        if not silent and not showed_cache and not has_existing_content:
            self.mail_panel.envelope_panel.show_placeholder("Loading...")
        if not silent:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "loading_unified",
                default=f"Loading unified {folder_type}...", folder=folder_type,
            ))

        collected = []
        remaining = [len(accounts)]  # mutable counter closed over below

        def make_success(account):
            account_backend = account_backends[account.account_id]

            def on_success(envelopes):
                if account_backend == "imap" and envelopes is not None:
                    self._note_server_result(account)
                if request_id != self._envelope_request_id:
                    return
                account_rows = []
                for envelope in envelopes:
                    envelope = dict(envelope)
                    envelope["_zbox_account_id"] = account.account_id
                    envelope["_zbox_backend"] = account_backend
                    collected.append(envelope)
                    account_rows.append(envelope)
                if silent:
                    # Shown as soon as this account answers, not when
                    # the slowest server finally does -- and once the
                    # reader has stopped arrowing.
                    started = time.monotonic()

                    def repaint(account_id=account.account_id, rows=account_rows):
                        if request_id != self._envelope_request_id:
                            return
                        if self.mail_panel.envelope_panel.replace_account_rows(account_id, rows):
                            logging.getLogger("zbox.main").info(
                                "Unified list updated for %s, %.1fs after the check returned.",
                                account_id, time.monotonic() - started,
                            )

                    self._when_list_quiet(repaint)
                remaining[0] -= 1
                if remaining[0] == 0:
                    self._finish_unified_fetch(
                        collected, folder_type, request_id, silent=silent, showed_cache=showed_cache
                    )
                    for synced_account in accounts:
                        if account_backends[synced_account.account_id] != "imap":
                            continue
                        synced_folder = account_folders[synced_account.account_id]
                        if synced_folder in _OFFLINE_SYNCED_FOLDERS:
                            self._refresh_offline_cache_quietly(synced_account, synced_folder)
            return on_success

        def make_error(account):
            def on_error(exc):
                if account_backends[account.account_id] == "imap":
                    self._note_server_result(account, exc)
                if request_id != self._envelope_request_id:
                    return
                logger = logging.getLogger("zbox.main")
                logger.warning("Unified fetch failed for %s: %s", account.identity_email, exc)
                remaining[0] -= 1
                if remaining[0] == 0:
                    self._finish_unified_fetch(
                        collected, folder_type, request_id, silent=silent, showed_cache=showed_cache
                    )
            return on_error

        for account in accounts:
            def work(account=account):
                if request_id != self._envelope_request_id:
                    # See the matching comment in _fetch_account_envelopes
                    # -- a Unified folder fans this out to one job per
                    # account, so this short-circuit matters even more
                    # here: leaving Unified and coming back re-queues a
                    # whole account's worth of jobs behind whatever this
                    # same view already queued moments earlier.
                    return None
                return himalaya_client.list_envelopes(
                    self.paths, account, folder=account_folders[account.account_id],
                    backend=account_backends[account.account_id],
                )
            self.lanes.for_read(
                account.account_id, account_backends[account.account_id],
            ).submit(work, make_success(account), make_error(account))

    def _show_cached_unified_envelopes_first(self, accounts, account_folders, folder_type, request_id):
        """
        Unified-view counterpart to _show_cached_envelopes_first:
        combines each account's local offline copy of its own real
        folder (account_folders[account.account_id] -- this can
        differ per account, e.g. Gmail's Sent vs everyone else's), the
        same way _finish_unified_fetch combines live results, and
        shows that immediately. Returns True if at least one account
        has ever been synced for its folder.
        """
        synced_accounts = [
            account for account in accounts
            if account_folders[account.account_id] in _OFFLINE_SYNCED_FOLDERS
            and os.path.isdir(
                os.path.join(
                    self.paths.cache_dir(account.account_id),
                    "offline_mail",
                    account_folders[account.account_id],
                )
            )
        ]
        if not synced_accounts:
            return False

        collected = []
        remaining = [len(synced_accounts)]

        def finish():
            # Chronological, not lexical -- see _parse_envelope_date:
            # a Unified folder mixes accounts/providers with different
            # UTC offsets, where the raw ISO strings don't sort right.
            collected.sort(key=_parse_envelope_date, reverse=True)
            if request_id != self._envelope_request_id:
                return
            self.mail_panel.envelope_panel.populate(collected)
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "messages_in_unified_cached",
                default=f"{len(collected)} message(s) in unified {folder_type} (last synced copy, updating...)",
                count=len(collected), folder=folder_type,
            ))
            self._maybe_apply_initial_focus()

        for account in synced_accounts:
            def work(account=account):
                if request_id != self._envelope_request_id:
                    return None  # see the matching comment in _fetch_account_envelopes
                return himalaya_client.list_envelopes(
                    self.paths, account, folder=account_folders[account.account_id], backend="maildir"
                )

            def on_success(envelopes, account=account):
                # envelopes is None when work() found this request
                # superseded and returned early. Iterating it raised
                # TypeError: 'NoneType' object is not iterable inside
                # the wx.CallAfter callback -- caught and logged by
                # _safe_callback, so it never reached the user, but
                # it also skipped the decrement below. remaining[0]
                # then never hit zero, finish() never ran, and this
                # unified folder's cached preview simply never
                # appeared. Seen live in a startup log: one ERROR and
                # a folder that stayed empty until the live fetch
                # replaced it.
                #
                # Counted, not returned early: every account that was
                # asked has to report back one way or the other, or
                # the ones that did succeed are never shown either.
                for envelope in envelopes or []:
                    envelope = dict(envelope)
                    envelope["_zbox_account_id"] = account.account_id
                    envelope["_zbox_backend"] = "maildir"
                    collected.append(envelope)
                remaining[0] -= 1
                if remaining[0] == 0:
                    finish()

            def on_error(exc, account=account):
                remaining[0] -= 1
                if remaining[0] == 0:
                    finish()

            self.lanes.local.submit(work, on_success, on_error)

        return True

    def _finish_unified_fetch(self, envelopes, folder_type, request_id, silent=False, showed_cache=False):
        if request_id != self._envelope_request_id:
            return
        # Chronological, not lexical -- see _parse_envelope_date.
        envelopes.sort(key=_parse_envelope_date, reverse=True)

        # Prefetch runs unconditionally, before the silent/non-silent
        # display split below -- it used to sit after that split's
        # early "if silent: ... return", which meant a Unified folder
        # sitting on screen through the 20-second auto-refresh never
        # prefetched anything at all, silently, for as long as it
        # stayed selected: only actually switching into the unified
        # view triggered it. A single-account folder never had this
        # gap (_fetch_account_envelopes calls _prefetch_message_bodies
        # unconditionally too), so Unified was quietly worse off.
        by_account = {}
        for envelope in envelopes:
            by_account.setdefault(envelope.get("_zbox_account_id"), []).append(envelope)
        for account_id, account_envelopes in by_account.items():
            account = self._find_account(account_id)
            if account is None:
                continue
            folder = _folder_display_to_himalaya(folder_type, account)
            backend = account_envelopes[0].get("_zbox_backend", "imap")
            self._prefetch_message_bodies(account, folder, account_envelopes, backend, request_id)

        if silent:
            # Each account's rows were swapped in as it answered (see
            # make_success in _fetch_unified_envelopes), and an account
            # whose refresh failed kept its rows. Repainting from
            # `envelopes` here would drop them.
            return
        # Unlike _fetch_account_envelopes, this never recorded itself
        # as the displayed target at all, so returning to a Unified
        # folder was permanently treated as "not what's already on
        # screen" (same_target in _fetch_unified_envelopes) even on a
        # second or third visit -- always paying for a fresh
        # cache-preview-then-placeholder cycle instead of the quiet
        # "nothing changed" path a same_target hit gets. Set only on
        # this non-silent pass, matching _fetch_account_envelopes: a
        # silent auto-refresh never re-targets anything, it only ever
        # refreshes whatever is already displayed.
        self._displayed_target = ("unified", folder_type)
        if showed_cache:
            self.mail_panel.envelope_panel.populate_if_changed(envelopes)
        else:
            self.mail_panel.envelope_panel.populate(envelopes)
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "messages_in_unified",
            default=f"{len(envelopes)} message(s) across accounts in unified {folder_type}.",
            count=len(envelopes), folder=folder_type,
        ))
        self._maybe_apply_initial_focus()

    def _prefetch_message_bodies(self, account, folder, envelopes, backend, request_id, limit=25):
        """
        Warms the message cache for the messages just listed, on the
        low-priority sync_worker, so opening one shortly after a
        folder loads is instant (see prefetch_messages in
        himalaya_client) instead of paying Himalaya's per-message
        subprocess cost right at click time. Already-cached messages
        are skipped almost immediately, so calling this on every
        folder load (including silent auto-refreshes) is cheap.
        Stops early if the user has since selected a different
        folder or account.

        Unread messages are warmed before read ones, each group
        keeping its incoming order (already newest-first, since
        that's how the server/Himalaya returns a folder listing) --
        so within the 'limit' cap this sweeps new mail first. Without
        this, a long folder's older *read* messages, which happen to
        sort earlier only by being listed first, could spend the
        whole prefetch budget while actual new mail sits unwarmed
        until its turn finally comes up -- and a click on one of
        those unwarmed messages pays the full live-read cost right
        then, including whatever the server happens to be doing at
        that moment (confirmed against a real user's debug log: one
        such live read took 27 seconds while its cached neighbors
        opened instantly).

        Before that sweep, this also diffs the incoming envelope ids
        against _known_envelope_ids (whatever this same folder looked
        like last time it was polled) and, if anything new turns up,
        hands just those ids to _prefetch_new_arrivals for immediate
        warming on their own dedicated worker. That fast path exists
        because the sweep below, even with unread mail moved to the
        front, only ever discovers a message that arrived after it
        started on its NEXT run -- up to one 20-second poll away, and
        later still if that run is already mid-fetch on something
        slow. A message that lands moments after a sweep begins would
        otherwise sit unwarmed for however long is left of that
        window purely because of when the poll happened to land, not
        anything about the message itself.
        """
        if not envelopes:
            return

        # This used to stand down entirely while any message tab was
        # open. That was right when every read meant its own
        # himalaya.exe with its own IMAP login, and several of them
        # contending for one account produced the body-read timeouts
        # in the log. It is wrong now, and it was costing exactly the
        # case that matters: with a message open -- which is most of
        # the time someone is actually reading mail -- nothing was
        # ever warmed, so every row went back to being a cold read.
        #
        # Reads go through the pooled IMAP connection now
        # (imap_body_fetch), where a background fetch shares one
        # already-authenticated socket and takes its lock
        # non-blocking: if an interactive read holds it, the prefetch
        # gives up that message rather than making the user wait.
        # And the subprocess path, on the rare fall-back, is still
        # covered by background_priority() below, which stands it
        # down against interactive work for the same account. Both
        # lanes are protected without switching the whole warm-up off.

        # Keyed by backend as well: the offline copy and the server
        # give one message different ids, so an automatic refresh that
        # reads the offline copy of a paused account must not compare
        # against the server's ids and greet every row as new mail.
        key = (account.account_id, folder, backend)
        current_ids = {e.get("id") for e in envelopes if e.get("id") is not None}
        previously_known = self._known_envelope_ids.get(key)
        self._known_envelope_ids[key] = current_ids

        if previously_known is not None:
            new_ids = current_ids - previously_known
            new_envelopes = [e for e in envelopes if e.get("id") in new_ids]
            # A message ZBox itself just moved here -- rescued from
            # Junk, undone, archived -- is not new mail. Greeting it
            # as new set off the sound and toast and ran the filters
            # over it, and the old spam filter used to send rescued
            # mail straight back. See main_frame._expect_arrivals.
            new_envelopes = self._expected_arrivals.filter_new(
                account.account_id, folder, new_envelopes,
            )
            if new_envelopes:
                # One sound/toast per batch of genuinely new arrivals,
                # not one per message -- several messages landing
                # between polls should ding once, the way
                # Thunderbird's own new-mail sound does, not as a
                # chord. Audit finding 47: the account can opt out of
                # just its own sound (Settings' own sounds_enabled is
                # still the overall master switch, checked inside
                # _play_sound itself) and independently opt in to a
                # spoken announcement. Muting the account (Account
                # Settings, or the Accounts menu's quick toggle)
                # overrides all three -- sound, announce and the
                # Windows notification below -- regardless of how
                # those are individually configured.
                if not getattr(account, "notifications_muted", False):
                    if getattr(account, "new_mail_sound_enabled", True):
                        self._play_sound("incoming-message")
                    if getattr(account, "new_mail_announce_enabled", False):
                        self._announce_new_mail(account, new_envelopes)
                    self._show_new_mail_toast(account, new_envelopes)
                self._prefetch_new_arrivals(account, folder, new_envelopes, backend, request_id)
                # Audit finding 45 automatic half: test these same
                # genuinely-new arrivals against this account filters
                # (mark read/flag/move) -- see
                # main_frame._apply_automatic_filters for why this is
                # scoped to Inbox only.
                self._apply_automatic_filters(account, folder, new_envelopes)
                # Spam and phishing filter's automatic half (Tools >
                # Privacy and Content Blocking), same "genuinely new"
                # list, same Inbox-only scoping -- see
                # _apply_spam_filter for why it's off unless both its
                # settings are explicitly on.
                self._apply_spam_filter(account, folder, new_envelopes)

        unread = [e for e in envelopes if _envelope_is_unread(e)]
        read = [e for e in envelopes if not _envelope_is_unread(e)]
        prioritized = unread + read

        if backend == "imap" and not self._background_allowed(account):
            return

        def should_continue():
            # request_id is None for a background account's own
            # refresh (see _on_idle_activity) -- not tied to whatever
            # folder happens to be on screen, so it's never superseded
            # by the user looking at something else. Arrowing through
            # the list ends this sweep between messages; the next
            # refresh picks it up again.
            if self._list_navigation_active():
                return False
            return request_id is None or request_id == self._envelope_request_id

        def work():
            # Background lane: stands down (HimalayaBackgroundSkipped,
            # caught inside prefetch_messages as "skip this round")
            # whenever interactive work for this account is active.
            with himalaya_client.background_priority():
                # Older than the Cache Clean Up age: skipped, or the
                # sweep would re-download what the daily clean up
                # removed every time an old folder is opened.
                return himalaya_client.prefetch_messages(
                    self.paths, account, prioritized, folder,
                    backend=backend, limit=limit, should_continue=should_continue,
                    max_age_days=self.settings_manager.settings.cache_max_message_age_days,
                )

        def on_error(exc):
            logging.getLogger("zbox.main").debug(
                "Message prefetch failed for %s/%s: %s", account.account_id, folder, exc
            )

        self.sync_worker.submit(work, lambda _result: None, on_error)

    def _apply_spam_filter(self, account, folder, new_envelopes):
        """
        The junk rules for genuinely new Inbox mail (Tools > Privacy
        and Content Blocking), which replaced the learned spam filter
        on 16 September 2026 -- see junk_rules.py and docs/decisions.md.
        Nothing here guesses.

        A message Mark as Not Junk exempted, or from a sender it
        allowed, is never touched. A message from a blocked sender is
        marked "Blocked Sender" on its row, or, with
        spam_auto_move_to_junk_enabled on, moved to Junk in one batched
        move with its origin recorded so Mark as Not Junk sends it
        back. Anything else has its links checked against the phishing
        list and is marked "Possible Phishing" on a hit, never moved:
        link lists catch newsletters too.

        The raw message is read only for that link check, so a blocked
        or allowed sender costs no fetch. Only the account's real
        Inbox, same reasoning as _apply_automatic_filters. Runs on
        sync_worker.
        """
        settings = self.settings_manager.settings
        if not settings.spam_filter_enabled or not new_envelopes:
            return
        if folder != _folder_display_to_himalaya("Inbox", account):
            return

        auto_move = bool(settings.spam_auto_move_to_junk_enabled)
        rules = self.junk_rules
        junk_folder = _junk_folder_for_account(account)

        def work():
            phishing = self._get_phishing_list()
            flagged = {}  # message_id -> label
            to_move = []  # (message_id, folder, message_id_header)
            for envelope in new_envelopes:
                message_id = envelope.get("id")
                if message_id is None:
                    continue
                state = rules.state_for(envelope)
                if state == junk_rules.ALLOWED:
                    continue
                if state == junk_rules.BLOCKED:
                    if auto_move and junk_folder and not himalaya_client._same_folder(junk_folder, folder):
                        to_move.append((message_id, folder, envelope.get("message-id")))
                    else:
                        flagged[message_id] = lang.t('main_ui', 'spam_blocked_sender', default="Blocked Sender")
                    continue
                if not phishing.domains:
                    continue
                try:
                    raw_text = himalaya_client.read_message_raw(
                        self.paths, account, message_id, folder=folder, backend="imap",
                    )
                except Exception:
                    logging.getLogger("zbox.main").debug(
                        "Junk rules could not fetch a message in %s/%s to check "
                        "its links.", account.account_id, folder, exc_info=True,
                    )
                    continue
                if raw_text and any(
                    phishing.is_blocked(host) for host in junk_rules.link_hosts(raw_text)
                ):
                    flagged[message_id] = lang.t('main_ui', 'spam_possible_phishing', default="Possible Phishing")

            moved = []
            if to_move:
                moved, failed = himalaya_client.move_messages_batch(
                    self.paths, account, to_move, junk_folder,
                )
                for item in failed:
                    flagged[item[0]] = lang.t('main_ui', 'spam_blocked_sender', default="Blocked Sender")
            return flagged, moved

        def on_success(result):
            flagged, moved = result
            if moved:
                store = self._junk_origin_store(account.account_id)
                for _message_id, origin, header in moved:
                    if header:
                        store.set_origin(header, origin)
                self._expect_arrivals(account, junk_folder, [item[2] for item in moved])
            if flagged:
                self.mail_panel.envelope_panel.mark_ids_spam(flagged)
            if moved:
                self._request_auto_refresh()

        def on_error(exc):
            logging.getLogger("zbox.main").debug(
                "Junk rules failed for %s/%s: %s",
                account.account_id, folder, exc,
            )

        self.sync_worker.submit(work, on_success, on_error)

    def _on_idle_activity(self, account, folder):
        """
        Called (via wx.CallAfter, so always on the main thread) by an
        account's IDLE watcher the instant the server reports its
        watched folder changed -- new mail most of the time, but also
        a deletion or flag change from another client. Runs an
        immediate, independent refresh for that exact account/folder.

        Deliberately not routed through _fetch_account_envelopes: that
        method (and the _envelope_request_id it's gated on) is scoped
        to whichever folder is currently on screen, and a background
        account's result would just be discarded the moment it
        arrives since request_id wouldn't match the live selection.
        This does the same envelope-list-then-prefetch work directly,
        passing request_id=None through to _prefetch_message_bodies
        so it's never treated as superseded by whatever the user
        happens to be looking at -- and that call already handles
        routing any genuinely new arrivals to the new_mail_worker fast
        lane via its own diff against _known_envelope_ids, so IDLE's
        only real job is making that discovery happen right away
        instead of on the next 20-second poll.

        If this account/folder does happen to be the one currently
        displayed, the visible list is updated too, the same as a
        normal poll would.
        """
        logging.getLogger("zbox.main").debug(
            "IDLE activity for %s/%s -- refreshing now.", account.account_id, folder
        )
        if not self._background_allowed(account):
            return

        def work():
            # Background lane: stands down (HimalayaBackgroundSkipped)
            # if interactive work for this account is active or waiting.
            with himalaya_client.background_priority():
                return himalaya_client.list_envelopes(self.paths, account, folder=folder, backend="imap")

        def on_success(envelopes):
            self._note_server_result(account)
            for envelope in envelopes:
                envelope["_zbox_backend"] = "imap"
            if self._current_message_tab() is not None:
                # A message is open and its reads own the account:
                # wait, but never drop it -- there may be no further
                # IDLE push for a long time, which is how new mail used
                # to sit unseen until Refresh All.
                self._defer_idle_activity(account, folder)
                return

            started = time.monotonic()

            def repaint():
                changed = False
                target = self.mail_panel.account_panel.selected_fetch_target()
                is_current = bool(
                    target
                    and target.get("account_id") == account.account_id
                    and target.get("folder") == folder
                )
                if is_current:
                    changed = self.mail_panel.envelope_panel.populate_if_changed(envelopes)
                    if changed:
                        self.GetStatusBar().SetStatusText(lang.t(
                            "main_ui", "messages_in_folder",
                            default=f"{len(envelopes)} message(s) in {folder}.",
                            count=len(envelopes), folder=folder,
                        ))
                elif (
                    target
                    and "unified" in target
                    and self.mail_panel.envelope_panel.has_listing()
                    and himalaya_client._same_folder(
                        folder, _folder_display_to_himalaya(target["unified"], account),
                    )
                ):
                    # A unified view is on screen and this is the folder
                    # it shows for this account: swap this account's rows
                    # in. It used to wait for the next 20-second refresh,
                    # so the new-mail sound played and the row came half
                    # a minute later.
                    rows = []
                    for envelope in envelopes:
                        row = dict(envelope)
                        row["_zbox_account_id"] = account.account_id
                        rows.append(row)
                    changed = self.mail_panel.envelope_panel.replace_account_rows(account.account_id, rows)
                if changed:
                    logging.getLogger("zbox.main").info(
                        "List updated from new-mail watching for %s/%s, %.1fs after the check returned.",
                        account.account_id, folder, time.monotonic() - started,
                    )

            # New mail is never paused: it is detected and heard now,
            # even mid-arrowing. Only the repaint waits for the list
            # to go quiet, so rows never move under the cursor.
            self._when_list_quiet(repaint)
            self._prefetch_message_bodies(account, folder, envelopes, "imap", request_id=None)

        def on_error(exc):
            if isinstance(exc, himalaya_client.HimalayaBackgroundSkipped):
                # Interactive work preempted this round. Retried shortly
                # rather than left for a next push that may never come.
                self._defer_idle_activity(account, folder)
                return
            self._note_server_result(account, exc)
            logging.getLogger("zbox.main").debug(
                "IDLE-triggered refresh failed for %s/%s: %s", account.account_id, folder, exc
            )

        # new_mail_worker, not sync_worker: sync_worker pauses while
        # the list is being arrowed through, and new mail must not.
        # Not self.worker either: an IDLE ping can fire every few
        # seconds, and it must never queue ahead of (or behind) an
        # open-message read.
        self.new_mail_worker.submit(work, on_success, on_error)

    # Seconds between retries of an IDLE refresh that had to wait.
    _IDLE_RETRY_SECONDS = 3.0

    def _defer_idle_activity(self, account, folder):
        """Runs _on_idle_activity again shortly for an account/folder
        whose refresh had to stand down. One pending retry per
        (account, folder): several pushes while the user is busy
        collapse into one refresh once they are not."""
        pending = getattr(self, "_idle_retry_pending", None)
        if pending is None:
            pending = self._idle_retry_pending = set()
        key = (account.account_id, folder)
        if key in pending:
            return
        pending.add(key)

        def retry():
            pending.discard(key)
            if self._find_account(account.account_id) is None:
                return  # removed in the meantime
            if self._background_work_should_stand_down():
                # Still busy: wait again without touching the network.
                self._defer_idle_activity(account, folder)
                return
            self._on_idle_activity(account, folder)

        wx.CallLater(int(self._IDLE_RETRY_SECONDS * 1000), retry)

    def _prefetch_new_arrivals(self, account, folder, new_envelopes, backend, request_id):
        """
        Immediately warms the cache for envelope ids that weren't in
        this folder the last time it was polled -- genuinely new
        mail, as opposed to the same messages simply being relisted.
        Runs on new_mail_worker, a lane of its own separate from both
        the interactive worker (message opens -- new mail never has
        to wait on whatever you're doing right now) and sync_worker
        (the regular unread-first sweep in _prefetch_message_bodies,
        which can already be busy on an older, larger backlog and
        would otherwise make a just-arrived message wait its turn).

        No 'limit' cap here: unlike the regular sweep, this only ever
        covers whatever's actually new since the last poll -- normally
        a handful of messages at most, never the whole folder.
        """
        if not new_envelopes:
            return
        logging.getLogger("zbox.main").info(
            "New mail: %d message(s) in %s/%s.",
            len(new_envelopes), account.account_id, folder,
        )
        if backend == "imap" and not self._background_allowed(account):
            return

        def should_continue():
            # request_id is None for a background account's own
            # refresh (see _on_idle_activity) -- not tied to whatever
            # folder happens to be on screen, so it's never superseded
            # by the user looking at something else.
            return request_id is None or request_id == self._envelope_request_id

        def work():
            # Background lane (dedicated new-mail worker): stands down
            # if interactive work for this account is active or waiting.
            with himalaya_client.background_priority():
                return himalaya_client.prefetch_messages(
                    self.paths, account, new_envelopes, folder,
                    backend=backend, limit=len(new_envelopes), should_continue=should_continue,
                )

        def on_error(exc):
            logging.getLogger("zbox.main").debug(
                "New-mail prefetch failed for %s/%s: %s", account.account_id, folder, exc
            )

        self.new_mail_worker.submit(work, lambda _result: None, on_error)

    def _on_auto_refresh_timer(self, event):
        self._request_auto_refresh()

    def _on_activate(self, event):
        # Alt+Tab back to the window: check for new mail immediately
        # instead of waiting for the next timer tick.
        if event.GetActive():
            self._request_auto_refresh()
        event.Skip()

    def _request_auto_refresh(self):
        """
        Debounced silent-refresh entry point used by the timer, window
        activation and post-tab-close recovery. Stands down while the
        user is reading a message or actively driving the list (see
        _background_work_should_stand_down), and collapses a burst of
        triggers -- timer tick + Alt-Tab + tab close landing in the
        same second -- into a single pass instead of stacking
        overlapping silent fetches.
        """
        if self._background_work_should_stand_down():
            return
        now = time.monotonic()
        if now - self._auto_refresh_last_run < _AUTO_REFRESH_MIN_INTERVAL_SECONDS:
            return  # a silent pass just ran; this trigger is subsumed by it
        self._auto_refresh_last_run = now
        self._auto_refresh_current_folder()

    def _check_own_inboxes_after_send(self, recipients_text):
        """
        After a send to one of the user's own accounts, checks that
        account's Inbox 3, 8 and 15 seconds later instead of waiting
        for the regular check or the server's new-mail push. Gmail's
        push arrived a full minute after a send to itself, live 24
        September 2026. Each check is the same one new-mail watching
        triggers (_on_idle_activity), so the sound, the list and the
        warming all behave exactly as for any other new mail.
        """
        from email.utils import getaddresses

        addresses = {
            address.strip().lower()
            for _name, address in getaddresses([recipients_text or ""])
            if address and address.strip()
        }
        if not addresses:
            return
        for account in self.account_manager.accounts:
            if not getattr(account, "enabled", True):
                continue
            own = {
                (getattr(account, "identity_email", "") or "").strip().lower(),
                (getattr(account, "login_email", "") or "").strip().lower(),
            }
            if not (own & addresses):
                continue
            folder = _folder_display_to_himalaya("Inbox", account)
            logging.getLogger("zbox.main").info(
                "Sent to own account %s; checking its Inbox in 3, 8 and 15 s.",
                account.account_id,
            )

            def check(account=account, folder=folder):
                if getattr(self, "_shutting_down", False):
                    return
                try:
                    self._on_idle_activity(account, folder)
                except RuntimeError:
                    # The frame is being torn down.
                    pass

            for delay_seconds in (3, 8, 15):
                wx.CallLater(delay_seconds * 1000, check)

    def _list_navigation_active(self):
        """True while the reader is arrowing through the message list:
        a selection moved within the last quiet window. Read from
        worker threads too (sync_worker's pause_while, the prefetch
        sweep's should_continue); it only reads two floats."""
        return (time.monotonic() - self._list_last_interaction) < _LIST_INTERACTION_QUIET_SECONDS

    def _when_list_quiet(self, callback):
        """Runs callback now if the list is quiet, otherwise once the
        reader has stopped arrowing for the quiet window. Main thread
        only. The callback checks for itself whether it went stale."""
        if not self._list_navigation_active():
            callback()
            return
        waited = time.monotonic() - self._list_last_interaction
        delay_ms = int((_LIST_INTERACTION_QUIET_SECONDS - waited) * 1000) + 20
        wx.CallLater(max(delay_ms, 20), self._when_list_quiet, callback)

    def _background_work_should_stand_down(self):
        """
        True while a background refresh or prefetch round would get in
        the user's way: a message tab is open (its reads own the
        account) or the user moved through the list within the last
        few seconds -- a repaint underneath an active selection is the
        "list is not smooth" complaint, and a fresh poll would only
        add another himalaya.exe to the pile.
        """
        if self._current_message_tab() is not None:
            return True
        now = time.monotonic()
        return (now - self._list_last_interaction) < _LIST_INTERACTION_QUIET_SECONDS

    def _refresh_after_tab_close(self):
        """Quiet catch-up refresh ~2s after a message tab closes: the
        auto-refresh timer stands down while a tab is open, so this is
        the natural moment to check for mail that arrived during the
        read. Reschedules if one is already pending."""
        timer = self._tab_close_refresh_timer
        if timer is not None and timer.IsRunning():
            timer.Stop()
        self._tab_close_refresh_timer = wx.CallLater(
            int(_TAB_CLOSE_REFRESH_DELAY_SECONDS * 1000),
            self._request_auto_refresh,
        )

    def _auto_refresh_current_folder(self):
        """
        Silently re-fetches whatever folder the user is currently
        looking at and updates the list only when content changed.
        Fired by a timer and on window activation; never shows a
        loading placeholder and never resets the selection on an
        unchanged result.
        """
        target = self.mail_panel.account_panel.selected_fetch_target()
        if not target:
            return
        if not self.mail_panel.envelope_panel.has_listing():
            return  # nothing real loaded yet; don't fight a loading state
        self._envelope_request_id += 1
        request_id = self._envelope_request_id
        if "unified" in target:
            self._fetch_unified_envelopes(target["unified"], request_id, silent=True)
        else:
            self._fetch_account_envelopes(target["account_id"], target["folder"], request_id, silent=True)

    def _schedule_auto_purge(self):
        """
        Permanently removes messages older than settings.
        auto_purge_days from Trash and Junk, for every enabled
        account -- the automatic form of Empty Trash/Empty Junk
        (purge_folder), run once a day (see __init__ for the timer).
        0 disables it. Runs on sync_worker, background-priority per
        account/folder so it stands down rather than competing with
        anything interactive for that same account, and one
        account/folder's failure or stand-down never stops the rest
        of the pass. Never surfaces an error to the user -- a missed
        pass simply tries again on the next one -- but does say what
        it actually removed, since a silent automatic permanent
        delete is not the same kind of housekeeping as a cache prune.
        """
        days = getattr(self.settings_manager.settings, "auto_purge_days", 30)
        if not days:
            return
        accounts = [
            account for account in self.account_manager.enabled_accounts()
            if self._background_allowed(account)
        ]
        cutoff = time.time() - days * 86400

        def work():
            purged_total = 0
            for account in accounts:
                for folder_type in ("Trash", "Junk"):
                    folder = _folder_display_to_himalaya(folder_type, account)
                    trash_folder = _folder_display_to_himalaya("Trash", account)
                    try:
                        with himalaya_client.background_priority():
                            envelopes = himalaya_client.list_envelopes(
                                self.paths, account, folder=folder,
                                page_size=2000, backend="imap",
                            )
                            old = [
                                e for e in envelopes
                                if _parse_envelope_date(e).timestamp() < cutoff
                            ]
                            if old:
                                himalaya_client.purge_folder(
                                    self.paths, account, folder, trash_folder, old
                                )
                                purged_total += len(old)
                    except Exception:
                        logging.getLogger("zbox.main").debug(
                            "Auto-purge skipped %s/%s this pass.",
                            account.account_id, folder_type, exc_info=True,
                        )
                        continue
            return purged_total

        def on_success(purged_total):
            if purged_total:
                logging.getLogger("zbox.main").info(
                    "Auto-purge removed %d message(s) older than %d day(s) "
                    "from Trash/Junk.", purged_total, days,
                )
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "auto_purge_done",
                    default=f"Auto-purge removed {purged_total} old message(s) from Trash/Junk.",
                    count=purged_total,
                ))

        def on_error(exc):
            logging.getLogger("zbox.main").warning("Auto-purge failed: %s", exc)

        self.sync_worker.submit(work, on_success, on_error)

