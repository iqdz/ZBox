"""
ZBox main window.
Three panes: account/folder tree, envelope list, message reader.
Layout only in this step, no live Himalaya data yet.
Styling goal: modern, simple, uncluttered, high contrast, no
decorative chrome that would confuse a screen reader.
"""

import logging
import os
import subprocess
import sys
import time

import wx

from account_manager import AccountManager
from account_wizard import AccountWizard
from account_settings_dialog import AccountSettingsDialog
from signature_dialog import SignatureEditDialog
from compose_panel import ComposePanel
from settings_dialog import SettingsDialog
from unified_folders_dialog import UnifiedFoldersDialog
from cache_cleanup_dialog import (
    CacheCleanupDialog,
    ID_CLEAR_NOW,
    age_choice_label,
    cleanup_due,
)
import himalaya_client
import lang
from himalaya_worker import HimalayaWorker
from account_lanes import AccountLanes, Reachability, is_network_error
import imap_idle_watcher
from contacts import ContactManager
from blocklist import Blocklist, PhishingList
from message_body import threading_headers, thread_ids as _message_thread_ids
from mail_tab_panel import MailTabPanel
from mail_fetch import MailFetchMixin, _OFFLINE_SYNCED_FOLDERS
from window_geometry import resolve_window_geometry
from account_tree_panel import target_to_saved_fields, saved_fields_to_target
from envelope_format import (
    _envelope_backend,
    _envelope_is_flagged,
    _envelope_is_unread,
    _envelope_subject,
    _folder_display_to_himalaya,
    _forward_subject,
    _forwarded_body,
    _quoted_reply_body,
    _reply_all_cc_field,
    _reply_from_identity,
    _reply_recipient,
    _reply_subject,
    _envelope_sender_spoken,
    _ordered_folder_list,
    _resolve_search_folders,
    _junk_folder_for_account,
    parse_mailto,
    _total_unread_count,
)
import filter_rules
from filter_rules_dialog import FilterRulesDialog
from undo_manager import UndoManager, UndoableMove
from accessible import apply_content_background
from announce import announce, speak, configure as configure_announcer, warm_up as warm_up_announcer
from bulk_execution import run_bulk, summarize, failed_message_ids as _failed_message_ids
from bulk_progress_dialog import BulkProgressDialog
import sound_manager
import single_instance
import desktop_notifications
from thread_state import ThreadStateStore
from junk_origin import JunkOriginStore
import junk_rules as junk_rules_module
from tray_icon import ZBoxTaskBarIcon, app_icons
from delete_queue import DeleteQueue
import startup_registration


# What a bulk action is called.
#
# The key of each pair is the identifier the code itself passes
# around and compares against: undo_manager records it, and _on_undo
# reads it to decide whether a junk block has to be reversed along
# with the move. That identifier is never translated. Only the name
# spoken or shown is looked up, so a language file can change what an
# action is called without changing anything it does.
_ACTION_KEYS = {
    "Archive": "action_archive",
    "Delete": "action_delete",
    "Delete Permanently": "action_delete_permanently",
    "Mark as Junk": "action_mark_as_junk",
    "Mark as Not Junk": "action_mark_as_not_junk",
    "Move To": "action_move_to",
    "Copy To": "action_copy_to",
    "Mark Message": "action_mark_message",
    "Undo": "action_undo",
}


def _action_name(label):
    """What this action is called out loud. An action with no entry is
    spoken by its own identifier, so a new one is never silent."""
    key = _ACTION_KEYS.get(label)
    if key is None:
        return label
    return lang.t("actions_announcements", key, default=label)


# No colour constants here on purpose. ACCENT_COLOR and TEXT_COLOR
# were declared and never used, and BG_COLOR was a literal white that
# overrode Windows High Contrast -- see accessible.content_background,
# which asks the system what a content area should look like instead
# of asserting it.

# Menu and command IDs, shared between the menu bar and context menus
# so both routes fire the same handler.
ID_NEW_ACCOUNT = wx.NewIdRef()
ID_ACCOUNT_SETTINGS = wx.NewIdRef()
ID_DISABLE_ACCOUNT = wx.NewIdRef()
ID_REMOVE_ACCOUNT = wx.NewIdRef()
ID_SET_DEFAULT_ACCOUNT = wx.NewIdRef()
ID_CLEAR_MESSAGE_CACHE = wx.NewIdRef()
ID_CACHE_CLEANUP = wx.NewIdRef()
ID_MUTE_ACCOUNT = wx.NewIdRef()
ID_PAUSE_AUTO_CHECK = wx.NewIdRef()
ID_REFRESH_FOLDER = wx.NewIdRef()
ID_NEW_FOLDER = wx.NewIdRef()
ID_RENAME_FOLDER = wx.NewIdRef()
ID_DELETE_FOLDER = wx.NewIdRef()
ID_SUBSCRIBE_FOLDER = wx.NewIdRef()
ID_UNSUBSCRIBE_FOLDER = wx.NewIdRef()
ID_SETTINGS = wx.NewIdRef()
ID_REPLY = wx.NewIdRef()
ID_REPLY_ALL = wx.NewIdRef()
ID_FORWARD = wx.NewIdRef()
ID_DELETE_MESSAGE = wx.NewIdRef()
ID_DELETE_PERMANENT = wx.NewIdRef()
ID_MARK_READ = wx.NewIdRef()
ID_MARK_UNREAD = wx.NewIdRef()
ID_JUMP_UNIFIED = wx.NewIdRef()
ID_UNIFIED_FOLDERS = wx.NewIdRef()
ID_FOLDERS_ALL = wx.NewIdRef()
ID_FOLDERS_UNIFIED = wx.NewIdRef()
ID_SORT_DATE = wx.NewIdRef()
ID_SORT_SUBJECT = wx.NewIdRef()
ID_SORT_FROM = wx.NewIdRef()
ID_SORT_ASCENDING = wx.NewIdRef()
ID_SORT_DESCENDING = wx.NewIdRef()
ID_THREADS_ALL = wx.NewIdRef()
ID_THREADS_WATCHED = wx.NewIdRef()
ID_THREADS_IGNORED = wx.NewIdRef()
ID_EXPAND_ALL_THREADS = wx.NewIdRef()
ID_COLLAPSE_ALL_THREADS = wx.NewIdRef()
ID_WATCH_THREAD = wx.NewIdRef()
ID_IGNORE_THREAD = wx.NewIdRef()
ID_SHORTCUTS = wx.NewIdRef()
ID_TOGGLE_MESSAGE_VIEW = wx.NewIdRef()
ID_WORK_OFFLINE = wx.NewIdRef()
ID_SYNC_OFFLINE = wx.NewIdRef()
ID_TOGGLE_FLAG = wx.NewIdRef()
ID_ARCHIVE = wx.NewIdRef()
ID_MARK_JUNK = wx.NewIdRef()
ID_MARK_NOT_JUNK = wx.NewIdRef()
ID_ADDRESS_BOOK = wx.NewIdRef()
ID_SELECT_ALL = wx.NewIdRef()
ID_REFRESH_ALL_ACCOUNTS = wx.NewIdRef()
ID_OPEN_SAVED_MESSAGE = wx.NewIdRef()
ID_SAVE_MESSAGE_AS = wx.NewIdRef()
ID_EMPTY_TRASH = wx.NewIdRef()
ID_EMPTY_JUNK = wx.NewIdRef()
ID_PRINT_MESSAGE = wx.NewIdRef()
ID_PRINT_PREVIEW_MESSAGE = wx.NewIdRef()
ID_SHOW_REMOTE_CONTENT = wx.NewIdRef()
ID_PRIVACY = wx.NewIdRef()
ID_MASTER_PASSWORD = wx.NewIdRef()
ID_UPDATE_BLOCKLIST = wx.NewIdRef()
ID_ANNOUNCE_SENDER = wx.NewIdRef()
ID_ANNOUNCE_COPY_SENDER = wx.NewIdRef()
ID_EDIT_SIGNATURE = wx.NewIdRef()
ID_INCREASE_MESSAGE_ZOOM = wx.NewIdRef()
ID_DECREASE_MESSAGE_ZOOM = wx.NewIdRef()
ID_RESET_MESSAGE_ZOOM = wx.NewIdRef()
ID_IMPORT_ACCOUNTS = wx.NewIdRef()
ID_EXPORT_ACCOUNTS = wx.NewIdRef()
ID_ADD_SENDER_TO_ADDRESS_BOOK = wx.NewIdRef()
ID_SEARCH_MESSAGES = wx.NewIdRef()
ID_SHOW_RELATED_MESSAGES = wx.NewIdRef()
ID_OPEN_CONVERSATION = wx.NewIdRef()

_END_OF_THREAD_CONFIRM_SECONDS = 10
"""
How long a "press again to close" at the end of a thread stays armed.

Long enough to hear the announcement and decide -- the announcement
dialog itself holds for up to 6 seconds on a slow speech rate -- and
short enough that the same key pressed much later, after the user has
moved on and forgotten, does not close their message unprompted.
"""

_PREVIEW_DEBOUNCE_MS = 500
"""
How long the message list's cursor must sit still before a COLD row's
body is fetched for the reading pane.

EVT_LIST_ITEM_SELECTED fires once per row, so holding Down through
thirty rows fired thirty selections. Each one queued a live read on
the single interactive worker for a message the user had already
passed -- thirty network round trips to display one body. Waiting for
the cursor to settle turns that into one fetch.

250ms is above a fast key-repeat interval (Windows' default is about
30ms, and even a slow repeat is well under 250ms) so a held arrow key
produces exactly one fetch, and below the ~300ms at which a pause
starts to feel like the app is lagging rather than responding. The
selection announcer next door settles at 180ms for the same reason,
against the same keypresses.

Cache hits ignore this entirely and paint immediately -- see
_on_envelope_selected. Debouncing a free repaint would make a warmed
list feel slower than a cold one.
"""
ID_LOAD_MORE_MESSAGES = wx.NewIdRef()
ID_MESSAGE_FILTERS = wx.NewIdRef()
ID_HOTKEYS_DIAGNOSTIC = wx.NewIdRef()

# The three folders sync_folder_offline actually keeps a local copy
# of (see the "Synchronize for Offline" scoping decision). Used both
# to decide when an instant local-cache preview is possible on
# folder open, and when a background offline-cache refresh is worth
# triggering after a live fetch.

# Minimum seconds between background offline-cache top-ups for the
# same (account, folder), so leaving a synced folder open through
# many 20-second silent auto-refreshes doesn't re-download message
# bodies on every tick.

# Refresh-calm tuning (diagnosed from a real debug log: stacked
# background reads caused body-fetch timeouts, and silent list
# repaints under an active selection made the list feel rough).
# A silent auto-refresh stands down while the user is reading a
# message tab or has moved through the list within this many seconds.

# Minimum seconds between silent auto-refresh passes, so a burst of
# triggers (timer tick + window activation + tab-close recovery
# landing in the same second) collapses into one fetch instead of
# stacking overlapping ones.

# Delay before the catch-up refresh scheduled when a message tab is
# closed (see _refresh_after_tab_close) -- long enough for the tab to
# finish closing and focus to settle, short enough that new mail that
# arrived during the read shows up promptly.


class ZBoxMainFrame(MailFetchMixin, wx.Frame):
    """The main window: panes, menus and command handlers.

    Fetching, refreshing and prefetching live in MailFetchMixin
    (mail_fetch.py) -- mixed in rather than held as a controller so
    the move could be made without rewriting six hundred lines of
    thread-touching code. See that module's docstring.
    """

    def __init__(self, paths, settings_manager):
        super().__init__(None, title="ZBox", size=(1100, 700))
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.paths = paths
        self.account_manager = AccountManager(paths)
        # Before anything can try to authenticate: if this folder has
        # arrived on a computer that has never opened it, the stored
        # passwords are sealed to the machine that wrote them and the
        # master password is the only way in. Asking here, rather than
        # after the window is up, keeps every account from failing a
        # first fetch with a bare authentication error.
        self._unlock_secret_store()
        # accounts.json is now encrypted with the same keyset as the
        # passwords, and AccountManager above read it before the
        # unlock ran. On this computer that is fine, because DPAPI
        # opens silently. On a folder that has just arrived from
        # another computer it is not: the first read had no key yet.
        # Now that the master password has been entered, read it
        # again. This is also where a plaintext accounts.json from
        # before encryption gets converted, since load_accounts is
        # what runs the conversion.
        if self.account_manager.load_error:
            self.account_manager.load_accounts()
        # From here on a refused account change says so out loud
        # instead of only reaching the debug log.
        self.account_manager.notify_error = lambda message: wx.MessageBox(
            message,
            lang.t(
                "dialogs", "title_account_list_unavailable",
                default="Account list unavailable",
            ),
            wx.OK | wx.ICON_ERROR, self,
        )
        if self.account_manager.load_error:
            wx.MessageBox(
                lang.t(
                    "errors", "account_list_unreadable",
                    default="ZBox could not open your account list.\n\n"
                    "%s\n\n"
                    "No accounts have been loaded, and the file has not been "
                    "changed, so nothing has been lost. Adding, editing and "
                    "removing accounts is refused until it can be read."
                    % self.account_manager.load_error,
                    error=self.account_manager.load_error,
                ),
                lang.t(
                    "dialogs", "title_account_list_unavailable",
                    default="Account list unavailable",
                ),
                wx.OK | wx.ICON_ERROR, self,
            )
        if self.account_manager.accounts:
            self._ensure_master_password_set(
                lang.t('dialogs', 'mp_prompt_existing', default="Your saved account passwords are protected on this "
                "computer only. Set a master password so they can "
                "also be opened if you move ZBox to another one.")
            )
            # Regenerate himalaya.toml on every launch, not only when
            # an account is added/edited/removed. Without this, a fix
            # to the config-generation logic itself only ever took
            # effect for accounts touched again afterward, leaving
            # stale, possibly broken config content sitting on disk
            # indefinitely for everyone else.
            self.account_manager.write_himalaya_config()
        self.settings_manager = settings_manager
        self.worker = HimalayaWorker()
        # Separate low-priority queue, its own thread, for offline-
        # cache sync work specifically. A first-time sync can mean
        # dozens of full raw-body downloads in a row (each one slow
        # against Gmail in particular -- seconds per message, per the
        # debug log this was diagnosed from); if that ran on the same
        # queue as interactive actions, opening a folder or a message
        # would queue behind it and "loading" would sit for minutes.
        self.sync_worker = HimalayaWorker(pause_while=self._list_navigation_active)
        # A third, dedicated lane just for mail that's just arrived
        # (an id not present the previous time a folder was polled --
        # see _prefetch_message_bodies). sync_worker's regular sweep
        # already prioritizes unread mail, but it works through one
        # folder's backlog at a time and a new message otherwise has
        # to wait for that sweep's next turn -- up to one 20-second
        # poll interval away, and longer still if that sweep is
        # mid-fetch on something slow (confirmed against a real
        # user's debug log: one such fetch took 27 seconds). Routing
        # brand-new arrivals here instead means they warm on their
        # own thread the moment they're noticed, regardless of
        # whatever the regular sweep is doing.
        self.new_mail_worker = HimalayaWorker()
        # Foreground work per account, and local reads on their own
        # thread: one server's trouble never delays another account
        # or a message already on disk. See account_lanes.
        self.lanes = AccountLanes()
        # Backoff for automatic work after an account's server could
        # not be reached.
        self.reachability = Reachability()
        # Real-time discovery to go with the fast-lane warming above:
        # one IMAP IDLE connection per account (see imap_idle_watcher),
        # so new mail is noticed the moment the server pushes it
        # instead of waiting up to 20 seconds for the next poll to
        # even find out it exists. Started below, once the account
        # list and the panes _on_idle_activity touches both exist;
        # kept in sync with the account list from _on_new_account,
        # _on_account_settings and _on_remove_account, and torn down
        # in _on_close.
        self.idle_manager = imap_idle_watcher.IdleWatcherManager(
            self.paths, self._on_idle_activity, reachability=self.reachability,
        )
        self.contacts = ContactManager(paths)
        # Set only by the tray icon's own Quit item and File > Exit
        # (_on_exit) before calling Close() -- _on_close checks this to
        # tell a real quit apart from the X button/Alt+F4/Ctrl+W, which
        # close_to_tray intercepts instead of quitting. See
        # settings_manager.py's close_to_tray docstring for why File >
        # Exit deliberately always really quits regardless of the
        # setting.
        self._quitting = False
        # Set True the moment real teardown begins in _on_close --
        # either real-quit path (_quitting via File>Exit/tray Quit, or
        # a plain X-button close with close_to_tray off in Settings)
        # reaches the same Destroy() cascade below. Destroying the
        # account tree fires the tree's own EVT_TREE_SEL_CHANGED as its
        # items are removed; that event can still be queued when
        # Destroy() has already gone further, so _on_tree_selection_
        # changed checks this and returns early rather than touching a
        # tree that is (or is about to be) a deleted C++ object.
        self._shutting_down = False
        # Always created, regardless of minimize_to_tray/close_to_tray:
        # Quit, Settings and the unread count stay reachable through the
        # tray icon either way (System tray & window in Settings).
        self.tray_icon = ZBoxTaskBarIcon(self)
        # The ZBox logo on the title bar, taskbar and Alt+Tab. None
        # when app\assets\zbox.ico is missing: Windows' default stays.
        icons = app_icons()
        if icons is not None:
            self.SetIcons(icons)
        # Delete and Shift+Delete run in delete_queue: saved to disk,
        # one thread per account on its pooled IMAP connection, retried
        # until the server shows each message gone. Callbacks for
        # batches started in this session, by batch id; a batch carried
        # over from an earlier run has none and is settled by
        # _on_delete_batch_done alone. Started, with its rows hidden,
        # by _resume_pending_deletes once the panes exist.
        self._delete_callbacks = {}
        self.delete_queue = DeleteQueue(
            self.paths, self._delete_queue_account, self._on_delete_batch_done, wx.CallAfter,
        )

        # Ad and tracker domains, used by the HTML message view's
        # content filter. Loading is cheap (one file read); the
        # refresh that follows is not, so it runs on the low-priority
        # sync worker once the window is up -- see
        # _schedule_blocklist_update.
        self.blocklist = Blocklist(paths)
        self.work_offline = False
        # Audit finding 48: single-level undo for Delete/Archive/
        # Mark as Junk/Mark as Not Junk. Populated by the four
        # action methods via _set_undo_record, consumed by _on_undo.
        self.undo_manager = UndoManager()
        self._folder_cache = {}
        # Real per-account server folder lists for the tree (audit
        # findings 42 and 43), keyed by account_id, value the ordered
        # [(display_label, real_name, unread_count), ...] triples from
        # envelope_format._ordered_folder_list. Separate from
        # _folder_cache (Move To / Copy To's flat name list, which
        # deliberately keeps its own subscribed-only, counts-free
        # fetch) so fixing one doesn't risk the other. Empty until the
        # first background 'mailbox list --all --counts' resolves for
        # a given account; the tree falls back to the fixed six names
        # (no counts) until then.
        self._tree_folder_lists = {}
        # ThreadStateStore per account, created lazily by
        # _thread_state_store on first use -- one JSON file per
        # account (thread_state.py), backing View > Threads > Watched/
        # Ignored and (once wired up) the Watch/Ignore Thread commands.
        self._thread_state_stores = {}
        # JunkOriginStore per account, same lazy-cache shape as
        # _thread_state_stores above -- one JSON file per account
        # (junk_origin.py), recording which folder Mark as Junk moved
        # a message out of, so Mark as Not Junk can send it back.
        self._junk_origin_stores = {}
        # Junk rules (junk_rules.py): blocked and allowed senders and
        # exempt messages, shared by every account and read on the
        # sync worker by _apply_spam_filter. The phishing list is
        # loaded lazily, on that same worker, the first time the rules
        # run -- see _get_phishing_list.
        self.junk_rules = junk_rules_module.JunkRules(paths.junk_rules_file)
        self._phishing_list = None
        # Messages ZBox itself is moving into a folder, so the next
        # listing does not greet them as new mail: no sound, no toast,
        # no filters, no junk rules. See _expect_arrivals.
        self._expected_arrivals = junk_rules_module.ExpectedArrivals()
        self._tree_context_target = None
        # Throttle for the background offline-cache top-up that
        # follows a live fetch of Inbox/Sent/Drafts: keyed by
        # (account_id, folder), value is the time.monotonic() of the
        # last sync, so a folder left open through many silent
        # 20-second auto-refreshes doesn't re-download message bodies
        # every single tick.
        self._offline_cache_last_sync = {}
        # Envelope ids seen the last time each folder was polled,
        # keyed by (account_id, folder) -- lets _prefetch_message_bodies
        # tell "just arrived since last check" apart from "still the
        # same mail being relisted", so only genuinely new messages
        # take the fast path above. Never seeded for a folder means
        # its next poll is a first load, not a new-mail event.
        self._known_envelope_ids = {}
        # Bumped on every new fetch request; a result only gets
        # applied if it's still the latest one requested, so quickly
        # clicking through folders can't paint a stale list over a
        # newer selection.
        self._envelope_request_id = 0
        # Message being opened right now, keyed by (account, folder,
        # message id). A second activation of the same message while
        # the read is in flight is ignored, so pressing Enter twice
        # on a slow/large message can't queue a duplicate read and
        # open two tabs.
        self._opening_message_key = None
        # Bumped on every message open. A read whose token is no
        # longer the current one is dropped instead of opened, so a
        # message the user has moved on from cannot appear as a tab
        # behind a newer one, and a read the watchdog below gave up
        # on cannot arrive late.
        self._open_request_id = 0
        # The live open watchdog, and the message it last gave up on.
        # A retry of that same message gets a longer window: a large
        # message over a slow link must not become impossible to
        # open, which a flat cancel on every attempt would make it.
        self._open_watchdog = None
        self._open_timed_out_key = None
        # Bumped on every message-body preview request. Reading a
        # body is not instant, and arrow-navigating a list fires one
        # request per row, so without this the reader pane repaints
        # itself with every message the user passed through on the
        # way -- each stale result landing seconds after the user
        # moved on, in whatever order the reads happened to finish.
        # A result whose token is no longer the current one is
        # dropped instead of painted.
        self._body_request_id = 0
        # Pending debounced preview (see _on_envelope_selected), or
        # None. Cancelled on a cache hit, on a folder change, and
        # whenever a newer selection replaces it.
        self._preview_timer = None
        # The Search tab (finding 39), created lazily on first use.
        # None until Ctrl+Shift+F / Message > Search Messages is used
        # for the first time, and set back to None if its tab gets
        # closed, so a later invocation knows whether to create a
        # new one or just re-select/focus the existing one.
        self._search_tab = None
        # Throttle for progress-tick.wav during a bulk action's
        # progress bar -- see _play_progress_tick. A fast batch can
        # finish items far quicker than a short tick sound plays, and
        # firing one per item would overlap into noise rather than a
        # rhythm.
        self._last_progress_tick_at = 0.0
        # Set once the message list is first given real focus with a
        # message selected after launch (see _maybe_apply_initial_focus),
        # so later folder switches don't keep re-stealing focus back
        # to the list every time a fetch completes.
        self._did_initial_focus = False
        # Current message-list sort (View > Sort By). Applied
        # client-side to whatever is already loaded, and remembered
        # across restarts; EnvelopeListPanel.populate is what enforces
        # it on every later refresh.
        self._sort_key = getattr(settings_manager.settings, "sort_key", "date")
        self._sort_ascending = bool(
            getattr(settings_manager.settings, "sort_ascending", False)
        )
        apply_content_background(self)

        self._build_menu()
        self._build_toolbar()
        self._build_panes()
        self._build_statusbar()
        self._bind_pane_cycling()
        self._bind_commands()

        self._refresh_tree()
        # Audit finding 52: land back on whatever account/folder (or
        # Unified Folders entry) was selected when ZBox last closed,
        # instead of always defaulting to the first account's Inbox.
        # A no-op if nothing was saved yet, or if the saved target no
        # longer exists (account removed, folder renamed/deleted).
        saved_target = saved_fields_to_target(
            self.settings_manager.settings.last_selected_account_id,
            self.settings_manager.settings.last_selected_folder,
            self.settings_manager.settings.last_selected_unified_type,
        )
        if saved_target:
            self.mail_panel.account_panel.select_by_target(saved_target)
        self.idle_manager.sync(self.account_manager.accounts)
        self._resume_pending_deletes()
        self._refresh_all_real_folder_lists()

        # Silent background refresh: re-checks the currently selected
        # folder for new mail without touching the selection, so new
        # messages show up without the user having to use Refresh. A
        # fresh check also runs immediately when the window regains
        # focus (e.g. Alt+Tab back to ZBox).
        self._auto_refresh_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_auto_refresh_timer, self._auto_refresh_timer)
        self._auto_refresh_timer.Start(20000)
        # Blocklist refresh. Daily by the list's own reckoning, so
        # the tick rate only decides how soon after a day has passed
        # ZBox notices -- an hour is close enough and costs one
        # timestamp comparison. The first check runs shortly after
        # launch rather than during it: startup already has the
        # account config, IDLE connections and first folder fetch
        # competing for attention, and a missing blocklist update is
        # never the thing standing between the user and their mail.
        self._blocklist_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_blocklist_timer, self._blocklist_timer)
        self._blocklist_timer.Start(60 * 60 * 1000)
        wx.CallLater(15000, self._schedule_blocklist_update)
        wx.CallLater(45000, self._schedule_phishing_update)
        # Message-cache housekeeping (audit finding 37): same hourly
        # cadence and the same "first check shortly after launch, not
        # during it" reasoning as the blocklist refresh above -- an
        # on-disk cache that grows for one extra hour before its first
        # prune is not a problem worth competing with startup for.
        wx.CallLater(20000, self._schedule_message_cache_prune)
        # Auto-purge (Trash and Junk, older than settings.
        # auto_purge_days -- 0 disables). Same shape as the blocklist
        # and message-cache housekeeping just above: a first pass
        # shortly after launch, then daily while ZBox stays open.
        self._auto_purge_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self._schedule_auto_purge(), self._auto_purge_timer)
        self._auto_purge_timer.Start(24 * 60 * 60 * 1000)
        wx.CallLater(30000, self._schedule_auto_purge)
        # Cache clean up by message date (Tools > Cache Clean Up
        # Configuration). Checked every five minutes; runs at most
        # once a day, and only while ZBox is idle -- see
        # _maybe_run_cache_cleanup and cache_cleanup_dialog.cleanup_due.
        # Launch counts as activity, so it never runs during startup.
        self._app_last_input = time.monotonic()
        self._cache_cleanup_running = False
        self._cache_cleanup_timer = wx.Timer(self)
        self.Bind(
            wx.EVT_TIMER, lambda event: self._maybe_run_cache_cleanup(),
            self._cache_cleanup_timer,
        )
        self._cache_cleanup_timer.Start(5 * 60 * 1000)
        # Where the NVDA controller client lives, so announcements can
        # be spoken without a dialog taking focus. Missing is fine and
        # is logged once: announce() falls back to the dialog.
        configure_announcer(self.paths.apps_files)
        # Load the speech output now, during startup. Built lazily on
        # the first announcement it froze the window for 4.1 s, live on
        # 24 September 2026, in the middle of a delete.
        warm_up_announcer()
        # A second launch never starts an app: it leaves a request
        # and exits (main.py's one-instance guard). Each tick costs
        # one os.remove attempt.
        self._raise_request_timer = wx.Timer(self)
        self.Bind(
            wx.EVT_TIMER, lambda event: self._check_raise_request(),
            self._raise_request_timer,
        )
        # 250 ms since session al: the second launch now waits for
        # this tick to take its request (single_instance
        # .wait_until_consumed), so a shorter tick is a shorter wait
        # between pressing the shortcut and hearing the window.
        self._raise_request_timer.Start(250)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        # Frame-level key fallback for message-tab hotkeys (hk.txt
        # Method 2). The accelerator table above handles these keys
        # for every ordinary control; this hook additionally covers
        # WebView edge cases where the accelerator path is skipped.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_ICONIZE, self._on_iconize)

        self._restored_geometry = None
        self._restore_window_state()
        self.Bind(wx.EVT_SIZE, self._on_frame_geometry_changed)
        self.Bind(wx.EVT_MOVE, self._on_frame_geometry_changed)

        # Default focus, and the accessibility priority from the WX
        # UI blueprint: land directly on the message list on launch,
        # not on the Notebook tab strip (wx's own default focus
        # target, which NVDA announces as "Mail Tab selected. To
        # switch pages, press Control+PageDown" -- confirmed this is
        # exactly what was being announced instead). Deferred via
        # CallAfter so it runs after the frame is shown and wx's own
        # default-focus assignment has already happened, rather than
        # being overridden by it.
        wx.CallAfter(self.mail_panel.envelope_panel.list_ctrl.SetFocus)

    def Destroy(self):
        """Tears down every open message tab's WebView before the
        frame's children are destroyed, so a WebView2 that is still
        settling (a completed-load event racing app shutdown) cannot
        fire wxWebViewEdge's native "Error running JavaScript" dialog
        during the destroy cascade. Tab-close already handles this
        deterministically via _close_tab_at -> prepare_close; this
        covers closing ZBox itself with HTML tabs open. Idempotent
        and exception-safe, like prepare_close itself."""
        try:
            for index in range(self.notebook.GetPageCount()):
                page = self.notebook.GetPage(index)
                prepare_close = getattr(page, "prepare_close", None)
                if prepare_close is not None:
                    prepare_close()
        except Exception:
            pass
        return super().Destroy()

    def _background_allowed(self, account):
        """Whether automatic work may contact this account now: its
        "Check for new messages automatically" is on and it is not
        backing off after a connection failure. Never consulted for
        anything the reader asked for."""
        if account is None or not getattr(account, "auto_check", True):
            return False
        return self.reachability.allowed(account.account_id)

    def _note_server_result(self, account, exc=None):
        """Feeds a server call's outcome to the backoff. A connection
        failure pauses the account's automatic work, and says so once;
        a success resumes it."""
        if account is None:
            return
        if exc is None:
            if self.reachability.succeeded(account.account_id):
                logging.getLogger("zbox.main").info(
                    "%s reachable again; automatic checks resumed.",
                    account.account_id,
                )
            return
        if not is_network_error(exc):
            return
        seconds = self.reachability.failed(account.account_id)
        name = account.display_name or account.identity_email
        logging.getLogger("zbox.main").info(
            "%s unreachable; automatic checks paused for %d s.",
            account.account_id, seconds,
        )
        try:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "account_unreachable",
                default="%s could not be reached. Automatic checks for it "
                        "pause for %d minute(s)." % (name, max(1, seconds // 60)),
                account=name, minutes=max(1, seconds // 60),
            ))
        except RuntimeError:
            pass

    def _maybe_apply_initial_focus(self):
        """
        Selects and focuses the top message the first time the
        message list has real content after launch, so a screen
        reader announces the newest message immediately with no
        extra key presses. One-shot: later folder switches populate
        the list too, and re-stealing focus back to the list on every
        one of those would fight whatever the person is doing by
        then (reading a message, composing, mid-navigation).
        """
        if self._did_initial_focus:
            return
        if not self.mail_panel.envelope_panel.has_content():
            return
        self.mail_panel.envelope_panel.focus_top_message()
        self._did_initial_focus = True

    def _unified_available(self):
        """Whether unified folders mean anything right now.

        One account has nothing to unify: a unified Inbox would be
        that account's Inbox with an extra level of tree above it,
        and Configure Unified Folders would be picking which folder
        types to combine across a set of one. Offering either is
        offering a choice with a single outcome, which for a screen
        reader is a menu item to hear, arrow past and wonder about.
        """
        return len(self.account_manager.accounts) > 1

    def _effective_folder_pane_mode(self):
        """The folder pane mode actually in force.

        Coerced to "all" while there is only one account, WITHOUT
        writing that back to settings. The distinction matters: a
        reader who chose unified, then removed an account, should
        find unified still chosen when they add another one -- their
        preference was not withdrawn just because it briefly stopped
        applying.
        """
        if not self._unified_available():
            return "all"
        return self.settings_manager.settings.folder_pane_mode

    def _refresh_unified_menu_items(self):
        """Greys out the unified commands when there is one account,
        and re-checks the All radio if unified was the stored choice.
        Called from _refresh_tree, which is the one place every
        account add, removal and folder-mode change already passes
        through."""
        menubar = self.GetMenuBar()
        if menubar is None:
            return
        available = self._unified_available()
        menubar.Enable(ID_FOLDERS_UNIFIED, available)
        menubar.Enable(ID_UNIFIED_FOLDERS, available)
        if not available:
            menubar.Check(ID_FOLDERS_ALL, True)

    def _refresh_tree(self):
        self.mail_panel.account_panel.refresh_accounts(
            self.account_manager.accounts,
            self.settings_manager.settings.unified_folder_types,
            self._effective_folder_pane_mode(),
            folders_by_account=self._tree_folder_lists,
        )
        self._refresh_unified_menu_items()
        self._refresh_tray_icon()

    def _refresh_tree_preserving_selection(self):
        """
        Rebuilds the tree (audit finding 42's real folder list has
        just changed for some account) without silently moving the
        person's selection back to the first Inbox if the folder they
        were on still exists afterward -- refresh_accounts always
        rebuilds from scratch and re-picks a default, same as every
        other _refresh_tree() call site already accepts, but those are
        all direct user actions (switching accounts, changing
        settings); this one can fire from a background network
        response while someone is mid-read elsewhere.
        """
        target = self.mail_panel.account_panel.selected_fetch_target()
        self._refresh_tree()
        restored = False
        if target:
            restored = self.mail_panel.account_panel.select_by_target(target)
        if not restored:
            # select_by_target's False used to be ignored. When the
            # old folder no longer matched anything, the tree was left
            # with no selection and stayed that way until restart:
            # hotkeys needing a target did nothing, and
            # _auto_refresh_current_folder returned early, so no
            # refresh ran after a delete either.
            self.mail_panel.account_panel.ensure_selection()

    def _refresh_real_folders_for_account(self, account):
        """
        Background 'mailbox list --all --counts' for one account
        (audit findings 42 and 43 together), replacing the tree's
        fixed six-folder fallback with the account's real folder list
        -- including any custom or unsubscribed folders, and each
        one's unread count where Himalaya could report it -- once it
        resolves. Never blocks the UI: same lazy-background-then-
        refresh shape as _folders_for_account's existing Move To /
        Copy To fetch, on the interactive worker since this is a
        single quick call, not a multi-message loop.
        """
        def work():
            raw = himalaya_client.list_all_folders(self.paths, account, counts=True)
            entries = [f for f in raw if isinstance(f, dict) and f.get("name")]
            if not entries:
                return None
            names = [f.get("name") for f in entries]
            unread_counts = {
                f.get("name"): himalaya_client._mailbox_unread_count(f) for f in entries
            }
            return _ordered_folder_list(account, names, unread_counts=unread_counts)

        def on_success(pairs):
            self._note_server_result(account)
            if not pairs:
                return
            self._tree_folder_lists[account.account_id] = pairs
            self._refresh_tree_preserving_selection()

        def on_error(exc):
            self._note_server_result(account, exc)
            logging.getLogger("zbox.main").warning(
                "Could not list all folders for %s: %s", account.account_id, exc
            )

        self.sync_worker.submit(work, on_success, on_error)

    def _refresh_all_real_folder_lists(self):
        # enabled_accounts, not accounts: this is the loop whose per
        # account folder listing spent 21 seconds timing out against
        # an unreachable server at every launch. A disabled account
        # must not be contacted here at all, nor one whose automatic
        # checking is off or backing off.
        for account in self.account_manager.enabled_accounts():
            if self._background_allowed(account):
                self._refresh_real_folders_for_account(account)

    def _on_tree_selection_changed(self, event):
        if self._shutting_down:
            return
        target = self.mail_panel.account_panel.selected_fetch_target()
        self.mail_panel.reader_panel.reader.SetValue(lang.t('dialogs', 'rdr_select_message', default="Select a message to read it here."))

        if not target:
            # A tree rebuild (folder counts refreshing, an account
            # list change, Empty Trash/Junk's own refresh) briefly
            # clears the selection before re-picking one, which can
            # fire this with nothing selected for a moment even
            # though a real folder was already on screen a moment
            # before and will be again a moment after. Only show the
            # placeholder when there is nothing real to lose --
            # otherwise this would blank (and announce over) a
            # perfectly good list for no reason.
            if not self.mail_panel.envelope_panel.has_content():
                self.mail_panel.envelope_panel.show_placeholder(
                    lang.t('dialogs', 'envlist_select_folder', default="Select an account or folder to load messages.")
                )
            return

        self._save_selected_folder(target)

        self._envelope_request_id += 1
        request_id = self._envelope_request_id
        # Changing folders invalidates anything still being read for
        # the folder being left -- a body preview whose row no longer
        # exists, and a debounced one that has not even started.
        self._body_request_id += 1
        self._cancel_pending_preview()

        if "unified" in target:
            self._fetch_unified_envelopes(target["unified"], request_id)
        else:
            self._fetch_account_envelopes(target["account_id"], target["folder"], request_id)

    def _resolve_envelope_context(self, envelope):
        """
        Returns (account, folder, message_id) for the given envelope
        based on the currently selected tree target, or None if any
        piece can't be resolved. Shared by preview, tab-open, and
        mark read/unread, so they can never drift out of sync with
        each other about which account/folder a message belongs to.
        """
        log = logging.getLogger("zbox.main")
        target = self.mail_panel.account_panel.selected_fetch_target()
        if not target:
            # Every caller of this treats None as "quietly do
            # nothing", which is right for the user but left a whole
            # class of "it just won't open" with no trace at all.
            log.debug("No envelope context: the folder tree has no fetch target selected.")
            return None
        account_id = target.get("account_id") or envelope.get("_zbox_account_id")
        account = self._find_account(account_id)
        if account is None:
            log.debug(
                "No envelope context: account %r not found (target=%r, "
                "envelope account=%r).",
                account_id, target, envelope.get("_zbox_account_id"),
            )
            return None
        folder = target.get("folder")
        if folder is None:
            folder = _folder_display_to_himalaya(target.get("unified"), account)
        message_id = envelope.get("id")
        if message_id is None:
            log.debug(
                "No envelope context: envelope carries no id (keys=%s).",
                sorted(envelope),
            )
            return None
        return account, folder, message_id

    def _is_live_envelope(self, envelope):
        """
        True only if this envelope's data came from the live IMAP
        backend, not the local cache preview (see
        _show_cached_envelopes_first). Every mutation (mark
        read/unread, flag, archive, move, copy, delete) targets the
        IMAP backend unconditionally in himalaya_client.py -- but a
        row can briefly be on screen from the cache preview before
        the live fetch replaces it, and its id is in Maildir's id
        shape, which the IMAP backend rejects outright as an "Invalid
        message UID". Confirmed via a real user's debug log, twice:
        once for reading a message (fixed by making the read itself
        follow the envelope's own backend), and once for the
        mark-as-read that automatically follows a read (this guard).
        Callers skip the action (automatic ones silently; interactive
        ones with a status message) rather than let that error
        surface -- the live fetch already in flight replaces the row
        within about a second, after which the same action works
        normally.
        """
        return _envelope_backend(envelope) == "imap"

    def _on_envelope_selected(self, envelope):
        """
        Previews the message body in the reader pane. Selecting a
        message here -- including while arrow-navigating the list or
        during a multi-select sweep -- no longer marks it read on its
        own; only actually opening it (_on_envelope_activated, Enter
        or double-click) or the explicit Mark as Read/Unread menu
        commands do that now, so browsing the list can't silently
        clear someone's unread flags.
        """
        # A genuine user-driven selection (arrow move or click; the
        # initial-focus selection after a folder load also lands here,
        # which is harmless -- it only delays a background repaint by a
        # few seconds, never the warm-up right after load, which checks
        # only the message-tab state). Stamped so silent auto-refreshes
        # stand down briefly while the person is actively navigating.
        self._list_last_interaction = time.monotonic()

        context = self._resolve_envelope_context(envelope)
        if context is None:
            return
        account, folder, message_id = context
        backend = _envelope_backend(envelope)

        # Every row now starts on the local lane: the cached read and
        # the conversion to text used to run right here, on the main
        # thread, once per arrow press -- a large HTML message made the
        # next press wait behind it, and the list stopped feeling
        # instant. The main thread only puts the finished text in the
        # pane. A row the cache does not have falls through to the
        # debounced live read below, unchanged.
        #
        # The token is bumped HERE, so a read still in flight for an
        # earlier row is discarded the moment the user moves off it.
        self._body_request_id += 1
        request_id = self._body_request_id
        self._cancel_pending_preview()

        def begin():
            self._preview_timer = None
            if request_id != self._body_request_id:
                return
            # show_loading only once the fetch is really starting.
            # Painting it per keypress made the reader pane flicker
            # "Loading message..." all the way down a folder.
            self.mail_panel.reader_panel.show_loading()
            self.lanes.for_read(account.account_id, backend).submit(work, on_success, on_error)
            # Warm the rows around the cursor at the same moment. The
            # only reason this row was cold is that the folder-load
            # sweep's budget did not reach it; following the cursor
            # is what keeps the next one warm.
            self._prefetch_around_cursor(account, folder, backend)

        def work():
            return himalaya_client.read_message(
                self.paths, account, message_id, folder=folder,
                backend=backend,
            )

        def on_success(message):
            if request_id != self._body_request_id:
                return
            self.mail_panel.reader_panel.set_message(message)
            self.contacts.add_from_envelope_from(envelope.get("from"))

        def on_error(exc):
            if request_id != self._body_request_id:
                return
            self.mail_panel.reader_panel.show_error(str(exc))

        stale = object()

        def warm_work():
            # Arrowing past thirty rows queues thirty of these; the
            # ones the cursor has already left cost nothing.
            if request_id != self._body_request_id:
                return stale
            cached = himalaya_client.read_message_cached_only(
                self.paths, account, message_id, folder=folder, backend=backend,
            )
            if cached is None:
                return None
            from message_body import extract_message_body

            return (extract_message_body(cached) or "",)

        def on_warm(result):
            if result is stale or request_id != self._body_request_id:
                return
            if result is None:
                # Cold row: the cursor has already been still for the
                # quiet window, so the live read starts now.
                begin()
                return
            self.mail_panel.reader_panel.show_body_text(result[0])
            self.contacts.add_from_envelope_from(envelope.get("from"))
            self._prefetch_around_cursor(account, folder, backend)

        def on_warm_error(exc):
            if request_id != self._body_request_id:
                return
            logging.getLogger("zbox.main").debug(
                "Cached preview read failed for %s: %s", message_id, exc,
            )
            begin()

        def start():
            self._preview_timer = None
            if request_id != self._body_request_id:
                return
            self.lanes.local.submit(warm_work, on_warm, on_warm_error)

        # Nothing at all runs on an arrow press: the preview starts
        # once the cursor has been still for the quiet window, cached
        # row or not, so arrowing through a folder stays instant.
        self._preview_timer = wx.CallLater(_PREVIEW_DEBOUNCE_MS, start)

    def _prefetch_around_cursor(self, account, folder, backend):
        """
        Warms the messages on either side of the selected row, so
        arrowing on finds them already in the cache.

        The folder-load sweep warms the first 25 unread-first and
        stops. Past that, every row is a cold read at the moment it
        is selected -- which in a 200-message folder is most of it.
        This follows the cursor instead, which is where the next read
        is actually going to come from.

        Cheap by construction: already-cached messages are skipped
        without any I/O, the work runs on sync_worker, and each fetch
        goes through the pooled IMAP connection in background mode,
        where it gives the connection up rather than making an
        interactive read wait behind it.
        """
        envelopes = self.mail_panel.envelope_panel.rows_around_selection()
        if not envelopes:
            return
        live = [
            envelope for envelope in envelopes
            if envelope.get("id") is not None
            and _envelope_backend(envelope) == backend
            and envelope.get("_zbox_account_id", account.account_id) == account.account_id
        ]
        if not live:
            return
        if backend == "imap" and not self._background_allowed(account):
            return

        def work():
            with himalaya_client.background_priority():
                return himalaya_client.prefetch_messages(
                    self.paths, account, live, folder,
                    backend=backend, limit=len(live),
                )

        def on_error(exc):
            logging.getLogger("zbox.main").debug(
                "Cursor prefetch failed for %s/%s: %s",
                account.account_id, folder, exc,
            )

        self.sync_worker.submit(work, lambda _result: None, on_error)

    def _cancel_pending_preview(self):
        """Drops a debounced preview that has not fired yet."""
        if self._preview_timer is not None:
            self._preview_timer.Stop()
            self._preview_timer = None

    def _on_envelope_activated(self, envelope):
        """
        Enter or double-click on a message: opens it in its own tab
        with the message body plus attachment buttons (accessible
        Text view by default; Ctrl+B switches to the rendered HTML
        view), matching Thunderbird's behavior, rather than only
        updating the inline reader pane. A second activation of the
        same message while its read is still in flight is ignored, so
        a repeat Enter on a slow/large message can't queue a
        duplicate read and open two tabs.
        """
        context = self._resolve_envelope_context(envelope)
        if context is None:
            logging.getLogger("zbox.main").debug(
                "Open message: no context for envelope id=%s -- nothing opened.",
                envelope.get("id"),
            )
            return
        account, folder, _message_id = context
        self._on_envelope_activated_for(
            account, folder, envelope,
            thread_envelopes=self.mail_panel.envelope_panel.selected_thread_envelopes(),
        )

    def _on_envelope_activated_for(self, account, folder, envelope, thread_envelopes=None):
        """
        The half of _on_envelope_activated below context resolution,
        split out so a caller that already knows the (account,
        folder) -- a conversation tab, whose rows all belong to one
        thread in one folder -- opens a message by exactly the same
        path, duplicate guard and cache-first behaviour included,
        rather than a parallel copy of it.
        """
        message_id = envelope.get("id")
        if message_id is None:
            return
        backend = _envelope_backend(envelope)

        opening_key = (account.account_id, folder, message_id)
        thread_envelopes = list(thread_envelopes or [])
        # Enter opens at once: a preview still waiting for the cursor
        # to settle is dropped, so nothing queues ahead of the open.
        self._cancel_pending_preview()
        if self._opening_message_key == opening_key:
            logging.getLogger("zbox.main").debug(
                "Open message: %s is already being opened; ignoring the repeat.",
                opening_key,
            )
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "opening_message_already",
                default="Opening message... (already loading)",
            ))
            return
        logging.getLogger("zbox.main").debug(
            "Open message: %s backend=%s", opening_key, backend,
        )

        # A new open supersedes any older one: the previous read is
        # dropped rather than opening a tab behind this one, and its
        # watchdog is stood down.
        self._cancel_open_watchdog()
        self._open_request_id += 1
        request_id = self._open_request_id

        # Instant path, same idea as _on_envelope_selected: a message
        # already warmed by the background prefetch opens with no
        # subprocess call at all.
        cached_message = himalaya_client.read_message_cached_only(
            self.paths, account, message_id, folder=folder, backend=backend,
        )
        if cached_message is not None:
            self._open_message_tab(
                account, folder, envelope, cached_message,
                thread_envelopes=thread_envelopes,
            )
            return

        self._opening_message_key = opening_key
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "opening_message", default="Opening message..."
        ))

        def work():
            return himalaya_client.read_message(
                self.paths, account, message_id, folder=folder,
                backend=backend,
            )

        def clear_key():
            # Only if this request is still the one being tracked.
            # A slow read that fails after the user has already
            # started opening a different message used to clear the
            # newer message's key too, which quietly disabled the
            # duplicate-activation guard for it.
            if self._opening_message_key == opening_key:
                self._opening_message_key = None
            if request_id == self._open_request_id:
                self._cancel_open_watchdog()
            if self._open_timed_out_key == opening_key:
                self._open_timed_out_key = None

        def on_success(message):
            if request_id != self._open_request_id:
                logging.getLogger("zbox.main").debug(
                    "Open message: %s arrived too late; dropped.",
                    opening_key,
                )
                return
            self._open_message_tab(
                account, folder, envelope, message,
                thread_envelopes=thread_envelopes,
            )
            clear_key()

        def on_error(exc):
            if request_id != self._open_request_id:
                logging.getLogger("zbox.main").debug(
                    "Open message: %s failed after being superseded; "
                    "not reported.", opening_key,
                )
                return
            clear_key()
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "could_not_open_message",
                default="Could not open message.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "open_message_failed",
                    default=f"Could not open message.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_open_message", default="Open Message"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_read(account.account_id, backend).submit(
            work, on_success, on_error,
            is_cancelled=lambda: request_id != self._open_request_id,
        )
        self._arm_open_watchdog(opening_key, request_id)

    # How long an open may be silent before ZBox says it is still
    # working on it.
    _OPEN_NOTICE_MS = 2000

    def _arm_open_watchdog(self, opening_key, request_id):
        """Starts the "still opening" notice for one message open."""
        self._cancel_open_watchdog()
        self._open_watchdog = wx.CallLater(
            self._OPEN_NOTICE_MS,
            self._on_open_slow, opening_key, request_id,
        )

    def _cancel_open_watchdog(self):
        if self._open_watchdog is not None:
            self._open_watchdog.Stop()
            self._open_watchdog = None

    def _on_open_slow(self, opening_key, request_id):
        """The read has taken over two seconds. Says so, once, and
        keeps waiting.

        This used to give up after five seconds and cancel the read.
        Live, 24 September 2026, that cancelled a Gmail message that
        was only queued behind a Disroot connect: Enter did nothing,
        and a second Enter was ignored as a repeat. An open is now
        only ever superseded by opening something else; a read that
        fails reports its own error.
        """
        self._open_watchdog = None
        if request_id != self._open_request_id:
            return
        logging.getLogger("zbox.main").debug(
            "Open message: %s still waiting after %d ms.",
            opening_key, self._OPEN_NOTICE_MS,
        )
        text = lang.t(
            "actions_announcements", "message_still_opening",
            default="Opening message. The server is slow.",
        )
        self.GetStatusBar().SetStatusText(text)
        self.announce(
            text,
            title=lang.t("dialogs", "title_open_message", default="Open Message"),
        )

    def open_thread_neighbour(self, panel, step):
        """
        Ctrl+Shift+Page Down / Page Up inside an open message: move to
        the next or previous message of the same thread, REPLACING
        this tab rather than adding one.

        Replacing is the entire design. Opening a thread's messages
        all at once created one MessageViewPanel per message, and
        with the HTML view as the default that is one WebView2 -- a
        whole Edge instance -- per message, each loading
        asynchronously and each taking focus when it landed. A screen
        reader read the first message and then went silent. Flipping
        keeps exactly one message view alive no matter how long the
        thread is, so there is never anything to fight over focus.

        The order is the thread's own list order, captured when the
        message was opened -- not recomputed live. Recomputing would
        re-read flags this very action has been changing (opening
        marks read), so "the next one" would shift underneath the
        user as they moved through it.

        Ctrl+Page Up/Down are deliberately not used: those are
        wx.Notebook's built-in tab switching on Windows, and the
        screen reader announces them as such, so taking them would
        break the way back to the Mail tab.
        """
        envelopes = getattr(panel, "thread_envelopes", None) or []
        current_id = (panel.envelope or {}).get("id")
        if len(envelopes) < 2 or current_id is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "not_in_thread",
                default="This message is not part of a thread.",
            ))
            return

        position = None
        for index, candidate in enumerate(envelopes):
            if candidate.get("id") == current_id:
                position = index
                break
        if position is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "no_longer_in_thread",
                default="This message is no longer in the thread.",
            ))
            return

        target = position + step
        if target < 0 or target >= len(envelopes):
            self._end_of_thread(panel, step, len(envelopes))
            return

        # Any successful move disarms a pending end-of-thread prompt:
        # turning round and coming back should not leave a press
        # armed from several messages ago.
        panel._end_of_thread_prompt = None

        envelope = envelopes[target]
        message_id = envelope.get("id")
        if message_id is None or not self._is_live_envelope(envelope):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "message_still_loading",
                default="That message is still loading.",
            ))
            return
        backend = _envelope_backend(envelope)
        account, folder = panel.account, panel.folder
        position_text = lang.t(
            "main_ui", "thread_position",
            default="Message %d of %d in this thread." % (
                target + 1, len(envelopes),
            ),
            position=target + 1, total=len(envelopes),
        )

        def show(message):
            # Open first, then close: closing first would move focus
            # to whatever the notebook lands on and the screen reader
            # would announce that before the message the user asked
            # for. Both happen in one event-loop turn, so only one
            # message view is ever really alive.
            self._open_message_tab(
                account, folder, envelope, message,
                thread_envelopes=envelopes,
            )
            index = self.notebook.FindPage(panel)
            if index != wx.NOT_FOUND:
                self._close_tab_at(index)
            self.GetStatusBar().SetStatusText(position_text)

        cached = himalaya_client.read_message_cached_only(
            self.paths, account, message_id, folder=folder, backend=backend,
        )
        if cached is not None:
            show(cached)
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "opening_message", default="Opening message..."
        ))

        def work():
            return himalaya_client.read_message(
                self.paths, account, message_id, folder=folder,
                backend=backend,
            )

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "could_not_open_that_message",
                default="Could not open that message.",
            ))
            logging.getLogger("zbox.main").warning(
                "Thread flip could not read message %s: %s", message_id, exc,
            )

        self.lanes.for_read(account.account_id, backend).submit(work, show, on_error)

    def _end_of_thread(self, panel, step, total):
        """
        Ctrl+Shift+Page Down on the last message of a thread (or Page
        Up on the first): say so, and close the message on a second
        press, ending the reading cycle and landing back in the
        message list.

        Two presses, not one. The same key that has been moving
        through the thread would otherwise close the message the
        moment it ran out of thread, which is a bad thing to do by
        accident to someone who cannot see that they have reached the
        end -- and running off the end is exactly when you are
        least sure where you are.

        Said through announce() rather than the status bar, because
        NVDA and JAWS only speak the status bar when status-bar
        reporting is switched on, which is off by default. An
        announcement whose entire purpose is to tell you what the
        next keypress will do cannot be one that most readers never
        hear. announce() puts focus back where it was, so the message
        body keeps it.

        The arming is per-direction and expires: pressing Page Down
        at the end and then Page Up is not a confirmation, and
        neither is coming back several minutes later.
        """
        armed = getattr(panel, "_end_of_thread_prompt", None)
        now = time.monotonic()
        if (
            armed is not None
            and armed[0] == step
            and now - armed[1] <= _END_OF_THREAD_CONFIRM_SECONDS
        ):
            panel._end_of_thread_prompt = None
            index = self.notebook.FindPage(panel)
            if index != wx.NOT_FOUND:
                self._close_tab_at(index)
            self.GetStatusBar().SetStatusText(
                lang.t(
                    "main_ui", "thread_finished_one",
                    default="Finished this thread (1 message).",
                )
                if total == 1
                else lang.t(
                    "main_ui", "thread_finished_many",
                    default="Finished this thread (%d messages)." % total,
                    count=total,
                )
            )
            return

        panel._end_of_thread_prompt = (step, now)
        spoken = (
            lang.t(
                "actions_announcements", "thread_edge_first",
                default="First message in this thread. Press again to close.",
            )
            if step < 0
            else lang.t(
                "actions_announcements", "thread_edge_last",
                default="Last message in this thread. Press again to close.",
            )
        )
        self.GetStatusBar().SetStatusText(spoken)
        self.announce(
            spoken, title=lang.t("dialogs", "title_thread", default="Thread")
        )

    def _open_message_tab(self, account, folder, envelope, message, thread_envelopes=None):
        from message_view_panel import MessageViewPanel

        # Every route that opens a message tab passes through here,
        # so this is the one place that can answer "what opened
        # this?" -- asked after a message tab appeared during plain
        # arrow-key navigation, with no Enter, no Space and no
        # double-click. The four callers are activation from the
        # list, a conversation-tab row, a search result, and a thread
        # flip; a screen reader can also invoke a list item's default
        # action programmatically, which wx delivers as an ordinary
        # EVT_LIST_ITEM_ACTIVATED with no keystroke behind it. The
        # caller chain tells those apart; nothing else does.
        logger = logging.getLogger("zbox.main")
        if logger.isEnabledFor(logging.DEBUG):
            import traceback

            callers = " <- ".join(
                "%s:%d" % (frame.name, frame.lineno)
                for frame in reversed(traceback.extract_stack()[-6:-1])
            )
            logger.debug(
                "Opening message tab for %s in %s/%s. Asked for by: %s",
                envelope.get("id"), account.account_id, folder, callers,
            )

        panel = MessageViewPanel(
            self.notebook, self, account, folder, envelope, message,
            thread_envelopes=thread_envelopes,
        )
        subject = envelope.get("subject") or "(no subject)"
        title = subject if len(subject) <= 30 else subject[:27] + "..."
        # Added unselected on purpose. Adding it selected switched the
        # notebook, moved focus and had the screen reader announce the
        # switch while the HTML document was still hundreds of
        # milliseconds from existing -- announcing the tab being left
        # ("Mail tab selected") rather than the message. The panel
        # brings its own tab forward from _show_own_tab at the one
        # moment there is something to read; focus_view below starts
        # that, immediately for Text and after the page loads for HTML.
        self.open_tab(panel, title, select=False)
        panel.focus_view()
        self.GetStatusBar().SetStatusText(
            lang.t("main_ui", "ready", default="Ready.")
        )
        self.contacts.add_from_envelope_from(envelope.get("from"))
        if self._is_live_envelope(envelope):
            self._mark_read(account, folder, envelope)

    def _mark_read(self, account, folder, envelope):
        message_id = envelope.get("id")
        if message_id is None:
            return
        if not _envelope_is_unread(envelope):
            # Already seen server-side; skip the redundant flag-add
            # subprocess so rapid selection isn't slowed by queued
            # no-op jobs.
            return

        def work():
            himalaya_client.mark_as_read(self.paths, account, message_id, folder=folder)

        def on_success(_result):
            self.mail_panel.envelope_panel.mark_row_read(message_id)

        def on_error(exc):
            # Non-critical: the message still opened successfully,
            # only the read flag failed to update server-side.
            logging.getLogger("zbox.main").warning(
                "Could not mark message %s as read: %s", message_id, exc
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _bind_pane_cycling(self):
        """
        F6 moves focus between the three main panes, in place of the
        MoveAfterInTabOrder call, which only works between siblings
        under the same parent and cannot be used across panels.
        Also sets up tab navigation and closing accelerators here,
        since they share one accelerator table with F6.
        """
        cycle_id = wx.NewIdRef()
        next_tab_id = wx.NewIdRef()
        prev_tab_id = wx.NewIdRef()
        close_tab_id = wx.NewIdRef()
        close_tab_f4_id = wx.NewIdRef()
        close_tab_escape_id = wx.NewIdRef()

        self.Bind(wx.EVT_MENU, self._on_cycle_panes, id=cycle_id)
        self.Bind(wx.EVT_MENU, lambda evt: self._next_tab(), id=next_tab_id)
        self.Bind(wx.EVT_MENU, lambda evt: self._previous_tab(), id=prev_tab_id)
        self.Bind(wx.EVT_MENU, lambda evt: self._close_current_tab(), id=close_tab_id)
        self.Bind(wx.EVT_MENU, lambda evt: self._close_current_tab(), id=close_tab_f4_id)
        self.Bind(wx.EVT_MENU, lambda evt: self._close_current_tab(), id=close_tab_escape_id)

        accel_table = wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_F6, cycle_id),
            (wx.ACCEL_CTRL, wx.WXK_TAB, next_tab_id),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_TAB, prev_tab_id),
            (wx.ACCEL_CTRL, ord("W"), close_tab_id),
            (wx.ACCEL_CTRL, wx.WXK_F4, close_tab_f4_id),
            # Frame-wide Escape: closes whichever tab is open (message
            # view, saved-message view, new/reply/forward compose --
            # anything except the Mail tab at index 0, which
            # _close_current_tab already leaves alone) the same way
            # Ctrl+W/Ctrl+F4 do. Message/saved-message tabs also bind
            # Escape locally (see their own _bind_close_accelerators)
            # so it wins the race against RichEdit/WebView2 swallowing
            # the key; this frame-level entry is what makes Escape
            # work from a compose tab too, which has no such local
            # override and none is needed.
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, close_tab_escape_id),
            (wx.ACCEL_CTRL, ord("B"), ID_TOGGLE_MESSAGE_VIEW),
            (wx.ACCEL_NORMAL, wx.WXK_F5, ID_REFRESH_FOLDER),
            (wx.ACCEL_SHIFT, wx.WXK_F5, ID_REFRESH_ALL_ACCOUNTS),
            (wx.ACCEL_CTRL, ord("S"), ID_SAVE_MESSAGE_AS),
            (wx.ACCEL_CTRL, ord("P"), ID_PRINT_MESSAGE),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("P"), ID_PRINT_PREVIEW_MESSAGE),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("I"), ID_SHOW_REMOTE_CONTENT),
            (wx.ACCEL_CTRL, ord("U"), ID_ANNOUNCE_SENDER),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("U"), ID_ANNOUNCE_COPY_SENDER),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("J"), ID_JUMP_UNIFIED),
            # Clear Offline Message Cache. This was a menu-label
            # accelerator until the item moved into Tools > Cache
            # Clean Up Configuration; it lives here now so the key
            # still works from anywhere. One binding, never both.
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_DELETE, ID_CLEAR_MESSAGE_CACHE),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("Q"), wx.ID_EXIT),
        ])
        self.SetAcceleratorTable(accel_table)
        self._focus_targets = None  # built lazily, after panes exist

    def _on_cycle_panes(self, event):
        """
        F6: move focus to the next of the Mail tab's three panes.

        Also the documented way out of an open message's WebView,
        where it arrives via message_view_panel's hotkey bridge
        rather than this accelerator (WebView2 never lets the key
        reach the frame). Every one of the cycle's targets lives on
        the Mail tab, so when focus starts anywhere else -- an open
        message, a compose, the search tab -- the notebook has to be
        brought to the Mail tab first: SetFocus() on a control that
        belongs to a page the notebook is not showing puts keyboard
        focus somewhere invisible, which for a screen reader is worse
        than not moving at all.
        """
        if self._focus_targets is None:
            self._focus_targets = self.mail_panel.focus_targets()

        current = self.FindFocus()
        targets = self._focus_targets
        if current in targets:
            next_index = (targets.index(current) + 1) % len(targets)
        else:
            next_index = 0
            if self.notebook.GetSelection() != 0:
                self.notebook.SetSelection(0)
        targets[next_index].SetFocus()

    def _on_jump_unified_inbox(self, event):
        """
        View > Folders > Jump to Inbox Messages List, and
        Ctrl+Shift+J: the way back when the reader is several folders
        deep somewhere else and wants their mail again.

        It follows the current view rather than changing it. With
        Unified selected, that is the unified Inbox; with a
        particular account selected, that account's Inbox; with one
        account configured, the only Inbox there is. It used to force
        the folder pane into unified mode first, which meant a key
        pressed to get home also silently rearranged the folder tree
        -- a surprising thing to do to someone navigating by ear.

        Focus lands in the message list, not the folder tree, so a
        screen reader starts on a message rather than on a tree node
        the reader then has to arrow out of. Deferred for the reason
        _maybe_apply_initial_focus defers its own: the selection
        starts a fetch, and focus should land after the list has been
        repainted, not during.

        (The method keeps its old name so the existing binding in
        _bind_commands still finds it.)
        """
        panel = self.mail_panel.account_panel
        target = panel.selected_fetch_target()

        jump = None
        if target and "unified" in target:
            jump = {"unified": "Inbox"}
        elif target and target.get("account_id"):
            account = self._find_account(target.get("account_id"))
            if account is not None:
                jump = {
                    "account_id": account.account_id,
                    "folder": _folder_display_to_himalaya("Inbox", account),
                }

        # Two ways to land here with nothing to go on: no selection
        # at all (a launch that restored none), or a target that no
        # longer exists in the tree -- unified mode with Inbox not
        # among the chosen folder types, or an account since removed.
        # Both fall back to the first account's Inbox, which is what
        # the tree itself defaults to on a rebuild.
        landed = None
        if jump is not None and panel.select_by_target(jump):
            if "unified" in jump:
                landed = lang.t('actions_announcements', 'jump_unified_inbox', default="Unified inbox.")
            else:
                account = self._find_account(jump.get("account_id"))
                name = (account.display_name or account.identity_email) if account else ""
                landed = lang.t(
                    "actions_announcements", "inbox_landed",
                    default="{name} inbox.", name=name,
                ).strip()
        if landed is None:
            accounts = self.account_manager.accounts
            if not accounts:
                self.GetStatusBar().SetStatusText(lang.t(
                    "actions_announcements", "no_account_yet",
                    default="There is no account to jump to yet.",
                ))
                self.announce_action(
                    lang.t(
                        "actions_announcements", "no_account_yet",
                        default="There is no account to jump to yet.",
                    ),
                    title=lang.t("dialogs", "title_inbox", default="Inbox"),
                )
                return
            account = accounts[0]
            panel.select_by_target({
                "account_id": account.account_id,
                "folder": _folder_display_to_himalaya("Inbox", account),
            })
            landed = lang.t(
                "actions_announcements", "inbox_landed",
                default="{name} inbox.",
                name=(account.display_name or account.identity_email),
            )
        # Ctrl+Shift+J used to land somewhere silently, which is the
        # one thing a jump must not do: the whole point is knowing
        # where you now are.
        self.GetStatusBar().SetStatusText(landed)
        self.announce_action(
            landed, title=lang.t("dialogs", "title_inbox", default="Inbox")
        )

        wx.CallAfter(self.mail_panel.envelope_panel.list_ctrl.SetFocus)

    def _flag_check_state(self):
        """
        (enabled, checked) for the Flagged check item, computed from
        whatever is currently selected in the message list. Shared by
        Message > Mark > Flagged (_refresh_flag_menu_item) and the
        message list's own right-click Mark submenu, so the two can
        never disagree. Checked only when every selected message is
        already flagged -- the same rule _toggle_flag_from_selection
        uses to decide which way the next press goes, so the
        checkmark always predicts it. Disabled with nothing selected,
        which a screen reader announces as unavailable.
        """
        envelopes = self.mail_panel.envelope_panel.get_all_selected_envelopes()
        enabled = bool(envelopes)
        checked = enabled and all(_envelope_is_flagged(envelope) for envelope in envelopes)
        return enabled, checked

    def _refresh_flag_menu_item(self):
        """Keeps Message > Mark > Flagged's checkmark in sync with
        whatever is selected in the message list, so the item states
        the message's current flag rather than offering an unlabelled
        toggle. State computed by _flag_check_state, shared with the
        list's own right-click Mark submenu."""
        item = getattr(self, "_flag_menu_item", None)
        if item is None:
            return
        enabled, checked = self._flag_check_state()
        item.Enable(enabled)
        item.Check(checked)

    def _on_about(self, event):
        """Help > About ZBox. An accessible read-only dialog (see
        about_dialog.AboutZBoxDialog) rather than wx.adv.AboutBox or
        a plain wx.MessageBox: the native About dialog puts its
        content in a static block that NVDA and JAWS often leave
        unread until the user goes looking for it, and a message box
        has no room for the license, app-password help, support,
        GitHub and contact buttons this needs now."""
        from about_dialog import AboutZBoxDialog

        dialog = AboutZBoxDialog(self, self, self.paths.license_file)
        dialog.ShowModal()
        dialog.Destroy()

    def _build_menu(self):
        menubar = wx.MenuBar()

        # File menu structure follows Thunderbird's own File menu as
        # closely as ZBox's feature set allows -- same submenu names,
        # same item order, same hotkeys where Thunderbird has one --
        # so a screen-reader user coming from Thunderbird recognizes
        # the layout immediately instead of having to relearn it.
        # Items with no ZBox equivalent (Calendar, multiple address
        # books, folder create/rename/delete/compact, Send Unsent
        # Messages) are left out rather than added as dead entries.
        file_menu = wx.Menu()
        new_submenu = wx.Menu()
        new_submenu.Append(wx.ID_NEW, lang.menu_label("file_new_message", "Message\tCtrl+N"))
        new_submenu.Append(
            ID_NEW_ACCOUNT,
            lang.menu_label("file_new_account", "Email Account...\tCtrl+Shift+N"),
        )
        file_menu.AppendSubMenu(new_submenu, lang.menu_label("file_new", "New"))
        # Open and Save As were one-item submenus (Open > Saved
        # Message, Save As > File): a right-arrow and an extra level
        # of speech to reach a single choice. Flat items instead,
        # named in full so the item says what it does on its own.
        file_menu.Append(
            ID_OPEN_SAVED_MESSAGE,
            lang.menu_label("file_open_saved_message", "Open Saved Message..."),
        )
        file_menu.Append(wx.ID_CLOSE, lang.menu_label("file_close", "Close"))
        file_menu.Append(
            ID_SAVE_MESSAGE_AS,
            lang.menu_label("file_save_message_as", "Save Message As...\tCtrl+S"),
        )
        file_menu.AppendSeparator()
        get_new_submenu = wx.Menu()
        get_new_submenu.Append(
            ID_REFRESH_ALL_ACCOUNTS,
            lang.menu_label("file_get_new_all", "All Accounts\tShift+F5"),
        )
        get_new_submenu.Append(
            ID_REFRESH_FOLDER,
            lang.menu_label("file_get_new_current", "Current Account\tF5"),
        )
        file_menu.AppendSubMenu(
            get_new_submenu, lang.menu_label("file_get_new_for", "Get New Messages for")
        )
        file_menu.AppendSeparator()
        offline_submenu = wx.Menu()
        offline_submenu.AppendCheckItem(
            ID_WORK_OFFLINE, lang.menu_label("file_work_offline", "Work Offline")
        )
        offline_submenu.Append(
            ID_SYNC_OFFLINE, lang.menu_label("file_sync_now", "Synchronize Now")
        )
        file_menu.AppendSubMenu(
            offline_submenu, lang.menu_label("file_offline", "Offline")
        )
        file_menu.AppendSeparator()
        file_menu.Append(
            ID_PRINT_MESSAGE, lang.menu_label("file_print", "Print...\tCtrl+P")
        )
        file_menu.Append(
            ID_PRINT_PREVIEW_MESSAGE,
            lang.menu_label("file_print_preview", "Print Preview...\tCtrl+Shift+P"),
        )
        file_menu.AppendSeparator()
        # Ctrl+Shift+Q rather than Alt+F4. Both are bound, but
        # Alt+F4 is intercepted by close_to_tray (see _on_close)
        # while File > Exit and Ctrl+Shift+Q always really quit, so
        # the label names the key that does what the item says.
        file_menu.Append(
            wx.ID_EXIT, lang.menu_label("file_exit", "Exit\tCtrl+Shift+Q")
        )
        menubar.Append(file_menu, lang.menu_label("bar_file", "&File"))

        # Edit holds Undo (audit finding 48) plus the two commands
        # people reach for in this menu out of habit in any Windows
        # app: Select All and Find. Select All deliberately shows no
        # accelerator -- Ctrl+A belongs to whichever control has
        # focus (a compose body, a search field), and a menu
        # accelerator would claim it frame-wide; the message list
        # binds its own. Position (right after File) matches
        # Thunderbird's own Edit placement.
        edit_menu = wx.Menu()
        self.undo_menu_item = edit_menu.Append(
            wx.ID_UNDO, lang.menu_label("edit_undo", "Undo\tCtrl+Z")
        )
        edit_menu.AppendSeparator()
        # "(Ctrl+A)" is part of the name, not an accelerator: a real
        # menu accelerator would take Ctrl+A frame-wide (see above), but
        # the screen reader should still say which key does this.
        edit_menu.Append(
            ID_SELECT_ALL, lang.menu_label("edit_select_all", "Select All") + " (Ctrl+A)"
        )
        edit_menu.Append(
            ID_SEARCH_MESSAGES,
            lang.menu_label("edit_find_messages", "Find Messages...\tCtrl+Shift+F"),
        )
        menubar.Append(edit_menu, lang.menu_label("bar_edit", "&Edit"))
        self._edit_menu = edit_menu

        # There is no Accounts menu any more. Three items did not
        # earn a top-level slot no other mail client has, and
        # "&Accounts" owned Alt+A, which Archive needs (see
        # _on_char_hook). The three items open Tools instead, where
        # Thunderbird also keeps Account Settings.

        message_menu = wx.Menu()
        # Standard mail-client action order: what you do WITH a
        # message first (reply, forward), then where you put it
        # (archive, delete), then what you mark it as, then the
        # thread and sender commands. Search and Load More moved out
        # -- searching is a Find, and loading more rows is a display
        # action on the list, not something done to a message.
        message_menu.Append(ID_REPLY, lang.menu_label("message_reply", "Reply\tCtrl+R"))
        message_menu.Append(
            ID_REPLY_ALL,
            lang.menu_label("message_reply_all", "Reply All\tCtrl+Shift+R"),
        )
        message_menu.Append(
            ID_FORWARD, lang.menu_label("message_forward", "Forward\tCtrl+L")
        )
        message_menu.AppendSeparator()
        message_menu.Append(
            ID_ARCHIVE, lang.menu_label("message_archive", "Archive")
        )
        # "Delete (Move to Trash)" was the old label. Paired with
        # Delete Permanently directly under it the distinction is
        # already clear, and the parenthetical is one more thing to
        # sit through on every pass down this menu.
        message_menu.Append(
            ID_DELETE_MESSAGE, lang.menu_label("message_delete", "Delete\tDel")
        )
        message_menu.Append(
            ID_DELETE_PERMANENT,
            lang.menu_label(
                "message_delete_permanently", "Delete Permanently\tShift+Del"
            ),
        )
        message_menu.AppendSeparator()
        # Six flat "Mark as ..." items collapsed into one submenu,
        # as Thunderbird does. Flagged is a check item rather than
        # the old "Flag/Unflag Message": a slash-label reads badly
        # and never says which of the two states the message is
        # actually in. Kept in sync by _refresh_flag_menu_item on
        # every open of this menu, the same way Watch/Ignore Thread
        # below already are. Archive stays out of it -- it moves the
        # message, it does not mark it.
        mark_submenu = wx.Menu()
        mark_submenu.Append(ID_MARK_READ, lang.menu_label("mark_read", "Read"))
        mark_submenu.Append(ID_MARK_UNREAD, lang.menu_label("mark_unread", "Unread"))
        self._flag_menu_item = mark_submenu.AppendCheckItem(
            ID_TOGGLE_FLAG, lang.menu_label("mark_flagged", "Flagged")
        )
        mark_submenu.AppendSeparator()
        mark_submenu.Append(ID_MARK_JUNK, lang.menu_label("mark_junk", "Junk"))
        mark_submenu.Append(
            ID_MARK_NOT_JUNK, lang.menu_label("mark_not_junk", "Not Junk")
        )
        message_menu.AppendSubMenu(
            mark_submenu, lang.menu_label("message_mark", "Mark")
        )
        message_menu.AppendSeparator()
        # Watch Thread / Ignore Thread (Phase 4): checkable so the
        # menu itself shows whether the currently selected thread is
        # already watched/ignored -- kept in sync on every open by
        # _refresh_thread_menu_items (see _on_menu_open). Matches
        # Thunderbird's own placement in the Message menu; no visible
        # accelerator text for the same reason S/A/J above have none --
        # the actual bare W/K bindings live in envelope_list_panel's
        # own key handling (only meaningful with the list focused),
        # not a global wx accelerator that would fire from anywhere.
        message_menu.AppendCheckItem(
            ID_WATCH_THREAD, lang.menu_label("message_watch_thread", "Watch Thread")
        )
        message_menu.AppendCheckItem(
            ID_IGNORE_THREAD, lang.menu_label("message_ignore_thread", "Ignore Thread")
        )
        # Same name and same key as Thunderbird's own built-in
        # command, and the same idea: the whole conversation in ONE
        # tab. Enter/Space on a collapsed thread row expands it
        # (matching Thunderbird, and matching Right arrow) rather
        # than opening a tab per message, which is what was jamming
        # the screen reader -- see conversation_panel.py.
        message_menu.Append(
            ID_OPEN_CONVERSATION,
            lang.menu_label(
                "message_open_conversation",
                "Open Message in Conversation\tCtrl+Shift+O",
            ),
        )
        message_menu.Append(
            ID_SHOW_RELATED_MESSAGES,
            lang.menu_label(
                "message_show_related", "Show Related Messages\tCtrl+Shift+T"
            ),
        )
        message_menu.AppendSeparator()
        message_menu.Append(
            ID_ADD_SENDER_TO_ADDRESS_BOOK,
            lang.menu_label(
                "message_add_sender_to_address_book", "Add Sender to Address Book"
            ),
        )
        message_menu.Append(
            ID_ANNOUNCE_SENDER,
            lang.menu_label("message_announce_sender", "Announce Sender\tCtrl+U"),
        )
        message_menu.Append(
            ID_ANNOUNCE_COPY_SENDER,
            lang.menu_label(
                "message_announce_copy_sender",
                "Announce Sender and Copy Address\tCtrl+Shift+U",
            ),
        )
        # Appended to the menu bar further down, after View, so the
        # bar reads File, Edit, View, Message, Tools, Help -- the
        # order every Windows mail client uses -- while the code
        # still builds Message before View.
        self._message_menu = message_menu

        view_menu = wx.Menu()
        folders_submenu = wx.Menu()
        folders_submenu.AppendRadioItem(
            ID_FOLDERS_ALL, lang.menu_label("folders_all", "All")
        )
        folders_submenu.AppendRadioItem(
            ID_FOLDERS_UNIFIED, lang.menu_label("folders_unified", "Unified")
        )
        if self._effective_folder_pane_mode() == "unified":
            folders_submenu.Check(ID_FOLDERS_UNIFIED, True)
        else:
            folders_submenu.Check(ID_FOLDERS_ALL, True)
        # Everything about unified folders now lives inside this one
        # submenu, below a separator that also closes the All/Unified
        # radio group. Before, folder concerns were spread over three
        # items at two different levels of the View menu.
        folders_submenu.AppendSeparator()
        # Not "Jump to Unified Inbox Messages List" any more. The
        # command follows whichever view is selected rather than
        # forcing unified on, so naming one mode in the label would
        # describe only one of the things it does. Enabled with a
        # single account too, where it means that account's inbox --
        # unlike the two items above it, which are greyed out until
        # there is a second account to unify with.
        folders_submenu.Append(
            ID_JUMP_UNIFIED,
            lang.menu_label(
                "folders_jump_inbox", "Jump to Inbox Messages List\tCtrl+Shift+J"
            ),
        )
        # Was "Unified Folders..." -- read by at least one user as a
        # toggle/view switch (like Unified above it) rather than what
        # it actually is: a dialog to pick which folder types get
        # combined. "Configure" upfront makes the verb explicit; the
        # trailing "..." still signals a dialog per convention, same
        # as every other "..." item in this menu.
        folders_submenu.Append(
            ID_UNIFIED_FOLDERS,
            lang.menu_label(
                "folders_configure_unified", "Configure Unified Folders..."
            ),
        )
        view_menu.AppendSubMenu(
            folders_submenu, lang.menu_label("view_folders", "Folders")
        )
        view_menu.AppendSeparator()
        sort_submenu = wx.Menu()
        sort_submenu.AppendRadioItem(ID_SORT_DATE, lang.menu_label("sort_date", "Date"))
        sort_submenu.AppendRadioItem(
            ID_SORT_SUBJECT, lang.menu_label("sort_subject", "Subject")
        )
        sort_submenu.AppendRadioItem(ID_SORT_FROM, lang.menu_label("sort_from", "From"))
        sort_submenu.Check(
            {"subject": ID_SORT_SUBJECT, "from": ID_SORT_FROM}.get(
                self._sort_key, ID_SORT_DATE
            ),
            True,
        )
        sort_submenu.AppendSeparator()
        sort_submenu.AppendRadioItem(
            ID_SORT_DESCENDING,
            lang.menu_label("sort_descending", "Descending (newest first)"),
        )
        sort_submenu.AppendRadioItem(
            ID_SORT_ASCENDING,
            lang.menu_label("sort_ascending", "Ascending (oldest first)"),
        )
        sort_submenu.Check(
            ID_SORT_ASCENDING if self._sort_ascending else ID_SORT_DESCENDING, True
        )
        view_menu.AppendSubMenu(
            sort_submenu, lang.menu_label("view_sort_by", "Sort By")
        )
        threads_submenu = wx.Menu()
        threads_submenu.AppendRadioItem(
            ID_THREADS_ALL, lang.menu_label("threads_all", "All")
        )
        threads_submenu.AppendRadioItem(
            ID_THREADS_WATCHED, lang.menu_label("threads_watched", "Watched Threads")
        )
        threads_submenu.AppendRadioItem(
            ID_THREADS_IGNORED, lang.menu_label("threads_ignored", "Ignored Threads")
        )
        threads_submenu.Check(ID_THREADS_ALL, True)
        threads_submenu.AppendSeparator()
        # Bare "*"/"\" in the message list do the same two things
        # (see envelope_list_panel._on_key_down) -- not shown as a
        # \t accelerator here for the same reason Flag/Unflag's bare
        # S isn't: it's a list-scoped shortcut, not a frame-wide one,
        # documented instead in shortcuts_dialog.py.
        threads_submenu.Append(
            ID_EXPAND_ALL_THREADS,
            lang.menu_label("threads_expand_all", "Expand All Threads"),
        )
        threads_submenu.Append(
            ID_COLLAPSE_ALL_THREADS,
            lang.menu_label("threads_collapse_all", "Collapse All Threads"),
        )
        view_menu.AppendSubMenu(
            threads_submenu, lang.menu_label("view_threads", "Threads")
        )
        view_menu.AppendSeparator()
        view_menu.Append(
            ID_LOAD_MORE_MESSAGES,
            lang.menu_label("view_load_more", "Load More Messages\tCtrl+Shift+L"),
        )
        view_menu.AppendSeparator()
        # Was "Toggle Message View (Text/HTML)". Same command, said
        # as a sentence instead of a label with a parenthetical and a
        # slash in it.
        view_menu.Append(
            ID_TOGGLE_MESSAGE_VIEW,
            lang.menu_label(
                "view_toggle_message_view",
                "Switch Between Text and HTML View\tCtrl+B",
            ),
        )
        view_menu.Append(
            ID_SHOW_REMOTE_CONTENT,
            lang.menu_label(
                "view_show_remote_content",
                "Show Remote Content in This Message\tCtrl+Shift+I",
            ),
        )
        view_menu.AppendSeparator()
        zoom_submenu = wx.Menu()
        zoom_submenu.Append(
            ID_INCREASE_MESSAGE_ZOOM,
            lang.menu_label(
                "zoom_increase", "Increase Message Font Size\tCtrl+Shift+="
            ),
        )
        zoom_submenu.Append(
            ID_DECREASE_MESSAGE_ZOOM,
            lang.menu_label(
                "zoom_decrease", "Decrease Message Font Size\tCtrl+Shift+-"
            ),
        )
        zoom_submenu.Append(
            ID_RESET_MESSAGE_ZOOM,
            lang.menu_label("zoom_reset", "Reset Message Font Size\tCtrl+Shift+0"),
        )
        view_menu.AppendSubMenu(
            zoom_submenu, lang.menu_label("view_message_zoom", "Message Zoom")
        )
        menubar.Append(view_menu, lang.menu_label("bar_view", "&View"))
        menubar.Append(message_menu, lang.menu_label("bar_message", "&Message"))

        # Compose. On the menu bar rather than only on chords,
        # because Alt reaches the menu bar even when focus is inside
        # the composer's WebView, and the WebView is exactly where a
        # chord can be swallowed with no way to tell. Every item here
        # runs the same handler its chord runs -- one implementation
        # per command -- so this is a second door, not a second
        # feature. It stays enabled at all times and says so when
        # there is no compose tab, rather than being greyed out in a
        # way a screen reader reports as a dead end.
        compose_menu = wx.Menu()
        self._compose_commands = {}
        for label, command in (
            ("&Send\tCtrl+Enter", "send"),
            ("Save &Draft\tCtrl+S", "save_draft"),
            ("&Attach File...\tCtrl+Shift+A", "attach"),
            (None, None),
            ("&Bold\tCtrl+B", "fmt:bold"),
            ("&Italic\tCtrl+I", "fmt:italic"),
            ("Strike&through\tCtrl+Shift+X", "fmt:strike"),
            ("B&ulleted List\tCtrl+Shift+8", "fmt:bullet"),
            ("&Numbered List\tCtrl+Shift+7", "fmt:number"),
            ("&Quote\tCtrl+Shift+9", "fmt:quote"),
            ("C&ode\tCtrl+Shift+6", "fmt:code"),
            ("&Heading\tCtrl+Alt+1", "fmt:heading1"),
            ("Insert &Link...\tCtrl+K", "link"),
            ("&Formatting Here\tCtrl+Shift+K", "formatting"),
            (None, None),
            ("Check &Spelling...\tCtrl+Shift+F7", "spellcheck"),
            ("Next &Misspelling\tCtrl+Shift+F9", "spellcheck_next"),
            ("Add &Word to Dictionary\tCtrl+Shift+F8", "spellcheck_add"),
            (None, None),
            ("Insert Si&gnature\tCtrl+Shift+S", "insert_signature"),
            ("Paste as Quotatio&n\tCtrl+Shift+V", "paste_quotation"),
            ("Word Cou&nt\tCtrl+Shift+W", "word_count"),
            ("&Preview...\tCtrl+Shift+P", "preview"),
            (None, None),
            ("Composer Dia&gnostics...", "diagnostics"),
        ):
            if command is None:
                compose_menu.AppendSeparator()
                continue
            item = compose_menu.Append(
                wx.NewIdRef(),
                lang.menu_label("compose_" + command.replace(":", "_"), label),
            )
            self._compose_commands[item.GetId()] = command
            self.Bind(wx.EVT_MENU, self._on_compose_command, item)
        # Editing a signature does not need a compose tab, so this one
        # is bound to the frame like the diagnostic below rather than
        # routed at a compose panel like every item above. It sits
        # here, after the loop, because the loop only builds the items
        # that route.
        compose_menu.AppendSeparator()
        compose_menu.Append(
            ID_EDIT_SIGNATURE,
            lang.menu_label("compose_edit_signature", "Edit Si&gnature..."),
        )
        self.Bind(wx.EVT_MENU, self._on_edit_signature, id=ID_EDIT_SIGNATURE)
        # One switch for the whole application rather than one per
        # tab, which is why this is bound to the frame instead of
        # routed at a compose tab like every item above it. It works
        # with no compose tab open: the next one built reads it and
        # opens already recording.
        self.hotkeys_diagnostic = False
        self._hotkeys_item = compose_menu.AppendCheckItem(
            ID_HOTKEYS_DIAGNOSTIC,
            lang.menu_label(
                "compose_hotkeys_diagnostic", "&Hotkeys Diagnostic\tCtrl+Shift+F12"
            ),
        )
        self.Bind(
            wx.EVT_MENU,
            self._on_toggle_hotkeys_diagnostic,
            id=ID_HOTKEYS_DIAGNOSTIC,
        )
        menubar.Append(compose_menu, lang.menu_label("bar_compose", "&Compose"))
        self._compose_menu = compose_menu

        # Account commands first (this is where Thunderbird keeps
        # Account Settings too, and where ZBox's old Accounts menu
        # went), then the tools proper, then Settings last -- the
        # Windows convention for an Options/Settings item.
        tools_menu = wx.Menu()
        tools_menu.Append(
            ID_ACCOUNT_SETTINGS,
            lang.menu_label("tools_account_settings", "Account Settings..."),
        )
        tools_menu.AppendCheckItem(
            ID_MUTE_ACCOUNT, lang.menu_label("tools_mute_account", "Mute Account")
        )
        tools_menu.AppendCheckItem(
            ID_PAUSE_AUTO_CHECK,
            lang.menu_label("tools_pause_auto_check", "Pause Automatic Checking"),
        )
        tools_menu.Append(
            ID_DISABLE_ACCOUNT,
            lang.menu_label("tools_disable_account", "Disable Account"),
        )
        tools_menu.Append(
            ID_REMOVE_ACCOUNT,
            lang.menu_label("tools_remove_account", "Remove Account..."),
        )
        tools_menu.AppendSeparator()
        tools_menu.Append(
            ID_ADDRESS_BOOK,
            lang.menu_label("tools_address_book", "Address Book...\tCtrl+Shift+B"),
        )
        tools_menu.Append(
            ID_MESSAGE_FILTERS,
            lang.menu_label("tools_message_filters", "Message Filters..."),
        )
        tools_menu.AppendSeparator()
        tools_menu.Append(
            ID_IMPORT_ACCOUNTS,
            lang.menu_label("tools_import", "Import Accounts and Settings..."),
        )
        tools_menu.Append(
            ID_EXPORT_ACCOUNTS,
            lang.menu_label("tools_export", "Export Accounts and Settings..."),
        )
        # Clear Offline Message Cache moved inside this dialog, beside
        # the automatic clean up by message date. Its Ctrl+Shift+Delete
        # moved to the frame's accelerator table at the same time --
        # still one binding, as the Ctrl+W double-binding lesson
        # requires.
        tools_menu.Append(
            ID_CACHE_CLEANUP,
            lang.menu_label("tools_cache_cleanup", "Cache Clean Up Configuration..."),
        )
        tools_menu.AppendSeparator()
        tools_menu.Append(
            ID_PRIVACY,
            lang.menu_label("tools_privacy", "Privacy and Content Blocking..."),
        )
        # No Master Password item here. It opened the same dialog as
        # Settings > Privacy and Security, and two menu entries doing
        # one thing is how one of them ends up wrong (audit finding
        # 17). ID_MASTER_PASSWORD stays bound -- the handler is still
        # reached from Settings.
        tools_menu.Append(
            ID_SETTINGS, lang.menu_label("tools_settings", "Settings...")
        )
        menubar.Append(tools_menu, lang.menu_label("bar_tools", "&Tools"))
        # Mute Account's checkmark follows the tree selection and is
        # refreshed whenever this menu opens -- see _on_menu_open.
        self._tools_menu = tools_menu

        help_menu = wx.Menu()
        # F1 opens this list. It used to open ZBox Documentation,
        # which was removed from Help: it is not something an end user
        # needs, and the shortcuts are.
        help_menu.Append(
            ID_SHORTCUTS,
            lang.menu_label("help_shortcuts", "Keyboard Shortcuts...\tF1"),
        )
        help_menu.AppendSeparator()
        help_menu.Append(wx.ID_ABOUT, lang.menu_label("help_about", "About ZBox..."))
        menubar.Append(help_menu, lang.menu_label("bar_help", "&Help"))

        self.SetMenuBar(menubar)
        self._refresh_undo_menu_item()

    def _on_compose_command(self, event):
        """Runs a Compose menu item against whichever compose tab is
        open.

        It hands the command name to the panel's own chord dispatcher,
        so the menu and the keystroke reach the same handler. That is
        the point of the menu existing: when a chord is swallowed
        inside the WebView, this is a door into the identical code
        rather than a parallel implementation that can drift.
        """
        command = self._compose_commands.get(event.GetId())
        if not command:
            return
        index = self.notebook.GetSelection()
        page = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        dispatch = getattr(page, "_run_chord", None)
        if dispatch is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "compose_tab_needed_hint",
                default="That command needs a compose tab. Press Ctrl+N to "
                        "write a message.",
            ))
            self.announce_action(
                lang.t(
                    "actions_announcements", "compose_tab_needed",
                    default="That command needs a compose tab.",
                ),
                title=lang.t("dialogs", "title_compose", default="Compose"),
            )
            return
        dispatch(command)

    def _build_toolbar(self):
        toolbar = self.CreateToolBar(style=wx.TB_TEXT | wx.TB_FLAT)
        toolbar.SetName(lang.t("dialogs", "mf_toolbar", default="Main toolbar"))
        toolbar.AddTool(wx.ID_NEW, "New", wx.ArtProvider.GetBitmap(wx.ART_NEW))
        toolbar.AddTool(ID_REPLY, "Reply", wx.ArtProvider.GetBitmap(wx.ART_GO_BACK))
        toolbar.AddTool(ID_DELETE_MESSAGE, "Delete", wx.ArtProvider.GetBitmap(wx.ART_DELETE))
        toolbar.Realize()

    def _build_panes(self):
        # Plain wx.Notebook, not wx.aui.AuiNotebook. AuiNotebook's
        # internal tab strip (wxAuiTabCtrl) is a real focusable
        # window with no accessible name, so it sits in the Tab
        # order and screen readers announce its raw class name as
        # extra chatter. wx.Notebook wraps the native Windows tab
        # control instead, which is properly exposed to NVDA/JAWS.
        # Trade-off: no built-in mouse-clickable close "x" per tab;
        # Ctrl+W and Ctrl+F4 below still close tabs either way.
        self.notebook = wx.Notebook(self, style=wx.NB_TOP)
        self.notebook.SetName(lang.t("dialogs", "mf_tabs", default="ZBox tabs"))

        # {(account_id, folder): {uid: expiry}} -- rows a batched
        # Delete or Shift+Delete has taken off the list. Read through
        # _pending_hidden_keys, the list's hidden_keys_provider.
        self._pending_removals = {}

        self.mail_panel = MailTabPanel(
            self.notebook,
            self._show_tree_context_menu,
            self._show_list_context_menu,
            self._on_envelope_selected,
            self._on_tree_selection_changed,
            self._on_envelope_activated,
            self._on_list_delete_key,
            selection_announce_handler=self._on_selection_announce,
            list_bare_key_shortcuts_enabled=lambda: self.settings_manager.settings.bare_key_shortcuts_enabled,
            list_thread_flags_provider=self._current_thread_flags,
            list_hidden_keys_provider=self._pending_hidden_keys,
            outer_sash_position=self.settings_manager.settings.outer_sash_position,
            inner_sash_position=self.settings_manager.settings.inner_sash_position,
        )
        self.notebook.AddPage(self.mail_panel, lang.t('dialogs', 'tab_mail', default="Mail"), select=True)
        # Audit finding 52: persist a dragged sash position. Bound
        # here (once the splitters exist) rather than inside
        # MailTabPanel itself, which knows nothing about
        # settings_manager -- same "pane owns controls, frame owns
        # persistence" split every other pane in this file follows.
        self.mail_panel.outer_splitter.Bind(
            wx.EVT_SPLITTER_SASH_POS_CHANGED, self._on_sash_position_changed
        )
        self.mail_panel.inner_splitter.Bind(
            wx.EVT_SPLITTER_SASH_POS_CHANGED, self._on_sash_position_changed
        )
        # Hand the saved sort order to the list before any mail
        # arrives, so the very first folder load is already in the
        # order the reader last chose rather than snapping into it a
        # moment later.
        self.mail_panel.envelope_panel.set_sort(
            self._sort_key, self._sort_ascending
        )
        # The Mail tab is protected from closing in _close_current_tab
        # below (index 0 is never removed).

    def open_tab(self, panel, title, select=True):
        """
        Adds a new closable tab. Other features (opening a message
        in its own tab, search results, calendar, tasks) call this.
        """
        index = self.notebook.AddPage(panel, title, select=select)
        return index

    def _close_current_tab(self):
        return self._close_tab_at(self.notebook.GetSelection())

    def _close_tab_at(self, index):
        """
        Closes one notebook tab and puts keyboard focus wherever the
        notebook actually lands.

        Every way of closing a tab routes through here -- Escape,
        Ctrl+W and Ctrl+F4 from the frame, and message_view_panel's
        own close_tab (the WebView hotkey bridge, the Close button,
        and Delete's after-success) -- so an unsent compose only has
        to be defended once (Escape in particular was silently
        discarding half-written mail), and closing a message behaves
        the same whichever route asked for it.

        Those two used to disagree: close_tab always forced
        SetSelection(0) and focus into the Mail tab's message list,
        so a message opened from the Search tab dumped the reader on
        Mail instead of back in their results, and skipped the
        catch-up refresh -- the same keystroke landing somewhere
        different depending on whether the message was in Text or
        HTML view.
        """
        if index is None or index <= 0:
            if (
                index == 0
                and self.settings_manager.settings.close_to_tray
            ):
                # Ctrl+W/Ctrl+F4 on the Mail tab has nothing to close --
                # under close_to_tray this is the same "nothing left to do
                # but hide" gesture as the X button/Alt+F4 on _on_close.
                self._hide_to_tray()
            return  # nothing to close, or it's the Mail tab
        page = self.notebook.GetPage(index)
        if hasattr(page, "confirm_discard") and not page.confirm_discard():
            page.SetFocus()
            return
        # Give the page a deterministic chance to stop its WebView before
        # DeletePage destroys it from C++ (which never routes through the
        # Python Destroy() override). MessageViewPanel.prepare_close sets
        # its _webview_closing flag and tears the WebView down, so the
        # completed-load event WebView2 fires during its shutdown cannot
        # run script against a half-closed controller -- the "Error
        # running JavaScript" (0x8007139f) dialog. See that method.
        prepare_close = getattr(page, "prepare_close", None)
        if prepare_close is not None:
            prepare_close()
        self.notebook.DeletePage(index)
        if self.notebook.GetSelection() == 0:
            # Back on the Mail tab: focus the message list so the
            # screen reader resumes where the user was, and schedule
            # a quiet catch-up refresh, since the auto-refresh timer
            # stood down while the tab was open.
            self.mail_panel.envelope_panel.list_ctrl.SetFocus()
            self._refresh_after_tab_close()
            return
        # Landed on some other tab (a search tab the message was
        # opened from, another open message): focus that page's own
        # preferred control, so the reader lands on its content
        # rather than on whatever wx picks first.
        new_page = self.notebook.GetCurrentPage()
        if new_page is None:
            return
        focus_default = getattr(new_page, "focus_default", None)
        if focus_default is not None:
            focus_default()
        else:
            new_page.SetFocus()

    def _current_message_tab(self):
        """The currently selected notebook page if it is a message
        view tab (has close_tab), else None. Index 0 (the Mail tab)
        is never a message tab."""
        index = self.notebook.GetSelection()
        if index <= 0:
            return None
        page = self.notebook.GetPage(index)
        if hasattr(page, "close_tab"):
            return page
        return None

    def _on_toggle_message_view(self, event):
        """Ctrl+B / View > Toggle Message View: swaps the current
        message tab between accessible Text and rendered HTML."""
        page = self._current_message_tab()
        if page is None or not hasattr(page, "toggle_view_mode"):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "open_message_first_view",
                default="Open a message first, then press Ctrl+B to switch "
                        "its view.",
            ))
            return
        page.toggle_view_mode()

    def _on_show_remote_content(self, event):
        """Ctrl+Shift+I / View > Show Remote Content: loads the
        blocked images and styles for the message in the current tab.
        Known trackers stay blocked, and the choice does not carry to
        the next message."""
        page = self._current_message_tab()
        if page is None or not hasattr(page, "show_remote_content"):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "open_message_first_remote",
                default="Open a message first, then press Ctrl+Shift+I to "
                        "load its remote content.",
            ))
            return
        page.show_remote_content()

    # --- Blocklist refresh --------------------------------------------

    def _on_blocklist_timer(self, _event):
        self._schedule_blocklist_update()
        self._schedule_phishing_update()
        self._schedule_message_cache_prune()

    def _schedule_phishing_update(self, force=False):
        """Queues a phishing-list refresh on the low-priority worker,
        daily by the list's own reckoning, and only while the junk
        rules are on: the feed is around eleven megabytes, which is
        nobody's business to download for a feature that is off."""
        if not force:
            if not self.settings_manager.settings.spam_filter_enabled:
                return
            if self.work_offline:
                return

        def work():
            return self._get_phishing_list().update(force=force)

        def on_success(result):
            _changed, message = result
            if force:
                self.GetStatusBar().SetStatusText(message)

        def on_error(exc):
            logging.getLogger("zbox.main").warning("Phishing list refresh failed: %s", exc)
            if force:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "phishing_update_failed",
                    default="Could not update the phishing list: %s" % exc,
                    error=exc,
                ))

        self.sync_worker.submit(work, on_success, on_error)

    def _schedule_blocklist_update(self, force=False):
        """Queues a blocklist refresh on the low-priority worker.
        Downloading a couple of megabytes must never sit in front of
        the user opening a message, so it shares the queue that
        offline cache sync already uses."""
        settings = self.settings_manager.settings
        if not force:
            if not getattr(settings, "blocklist_enabled", True):
                return
            if not getattr(settings, "blocklist_auto_update", True):
                return
            if not self.blocklist.needs_update():
                return
        if self.work_offline and not force:
            return

        blocklist = self.blocklist

        def work():
            return blocklist.update(force=force)

        def on_success(result):
            changed, message = result
            if changed:
                logging.getLogger("zbox.main").info("%s", message)
            # The daily update is scheduled, so it is logged only; the
            # status line is for Update Now, which the user started.
            if force:
                self.GetStatusBar().SetStatusText(message)

        def on_error(exc):
            logging.getLogger("zbox.main").warning(
                "Blocklist refresh failed: %s", exc
            )
            if force:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "blocklist_update_failed",
                    default="Could not update the blocklist: %s" % exc,
                    error=exc,
                ))

        self.sync_worker.submit(work, on_success, on_error)

    def _schedule_message_cache_prune(self):
        """
        Enforces the size and age cap on every account's on-disk
        message cache (audit finding 37), on the low-priority worker
        so it never competes with an interactive fetch or read. Purely
        housekeeping -- disk space, not correctness -- so a failure
        here is logged and otherwise invisible; it never reaches the
        status bar or a dialog.
        """
        accounts = list(self.account_manager.accounts)

        def work():
            for account in accounts:
                # Size cap only. Age is handled once a day by the
                # clean up by message date (_maybe_run_cache_cleanup),
                # which judges a message by its own Date rather than
                # by when its cache file happened to be written.
                himalaya_client.prune_message_cache(
                    self.paths, account, max_age_seconds=None,
                )

        def on_error(exc):
            logging.getLogger("zbox.main").warning(
                "Message cache prune failed: %s", exc
            )

        self.sync_worker.submit(work, lambda _result: None, on_error)

    def _maybe_run_cache_cleanup(self):
        """
        Timer tick for the clean up by message date. Runs it on the
        low-priority worker when cleanup_due says so: a day since the
        last run, no keypress in ZBox for ten minutes, and no message
        tab open. The tab check matters because reading a message in
        the HTML view with a screen reader sends ZBox no keys at all,
        so without it someone mid-read would look idle.

        The last-run stamp is written only after the pass finishes,
        so a pass cut short by closing ZBox simply runs again at the
        next idle window. Housekeeping only: failures are logged and
        never reach the status bar or a dialog.
        """
        if self._cache_cleanup_running:
            return
        settings = self.settings_manager.settings
        now = time.time()
        idle_seconds = time.monotonic() - self._app_last_input
        if not cleanup_due(
            now, settings.cache_cleanup_last_run, idle_seconds,
            self._current_message_tab() is not None,
        ):
            return
        self._cache_cleanup_running = True
        days = settings.cache_max_message_age_days

        def work():
            return himalaya_client.prune_caches_by_message_date(self.paths, days)

        def on_success(_result):
            self._cache_cleanup_running = False
            self.settings_manager.settings.cache_cleanup_last_run = now
            self.settings_manager.save()

        def on_error(exc):
            self._cache_cleanup_running = False
            logging.getLogger("zbox.main").warning(
                "Cache clean up by message date failed: %s", exc
            )

        self.sync_worker.submit(work, on_success, on_error)

    def _on_cache_cleanup_configuration(self, event):
        """
        Tools > Cache Clean Up Configuration. The age choice is saved
        on OK, and also when Clear Offline Message Cache Now closes
        the dialog, so a choice made just before pressing it is not
        lost. The clear itself runs after the dialog is gone, so its
        confirmation and spoken result belong to the main window
        rather than a modal it is hidden behind.
        """
        dialog = CacheCleanupDialog(self, self.settings_manager.settings)
        result = dialog.ShowModal()
        if result in (wx.ID_OK, ID_CLEAR_NOW):
            self.settings_manager.settings = dialog.apply_to_settings()
            self.settings_manager.save()
            days = self.settings_manager.settings.cache_max_message_age_days
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "auto_cache_cleanup",
                default="Automatic cache clean up: messages older than %s."
                        % age_choice_label(days),
                age=age_choice_label(days),
            ))
        dialog.Destroy()
        if result == ID_CLEAR_NOW:
            self._on_clear_message_cache(None)

    def _on_char_hook(self, event):
        """
        Frame-level key fallback (hk.txt Method 2). The accelerator
        table already covers Ctrl+W / Ctrl+F4 / Ctrl+B / Escape
        everywhere, but while the HTML WebView has focus WebView2
        intercepts keys before wx ever sees them; the page's own JS
        bridge (_HOTKEY_BRIDGE_JS) handles that case. This hook is
        the belt-and-suspenders path for every other focused control
        and for builds where the JS bridge cannot be injected.
        """
        # Every keypress ZBox sees counts as activity for the idle
        # check that gates the daily cache clean up.
        self._app_last_input = time.monotonic()
        key = event.GetKeyCode()
        ctrl = event.ControlDown()
        shift = event.ShiftDown()

        if ctrl and not shift and key in (ord("W"), ord("w")):
            self._close_current_tab()
            return
        if ctrl and not shift and key == wx.WXK_F4:
            self._close_current_tab()
            return
        if key == wx.WXK_ESCAPE:
            # A thread expanded in the message list gets first claim
            # on Escape, but only while focus is actually in that
            # list -- collapse_focused_thread() looks at the list's
            # current *selection*, which can still point at an
            # expanded thread from earlier even while the user is
            # now in a different tab entirely, and Escape there must
            # keep its normal meaning (closing that tab), not reach
            # back into the Mail tab's list behind it.
            if (
                wx.Window.FindFocus() is self.mail_panel.envelope_panel.list_ctrl
                and self.mail_panel.envelope_panel.collapse_focused_thread()
            ):
                return
            # Otherwise: closes whatever tab is open -- message view,
            # saved message, or a new/reply/forward compose in
            # progress -- the same as Ctrl+W/Ctrl+F4.
            # _close_current_tab already leaves the Mail tab (index 0)
            # alone, which is also correct here: Escape with focus in
            # the message list but no expanded thread to collapse is
            # a no-op, same as it always has been.
            self._close_current_tab()
            return
        if ctrl and not shift and key in (ord("B"), ord("b")):
            self._on_toggle_message_view(event)
            return
        if event.AltDown() and not ctrl and not shift and key in (ord("A"), ord("a")):
            # Archives whichever message is open in its own tab, but
            # ONLY while keyboard focus is actually in that message's
            # own body/WebView. The menu-bar mnemonic that used to
            # own Alt+A ("&Accounts") is gone -- its items moved into
            # Tools -- but the focus guard stays: archiving has to be
            # tied to the message the reader is actually inside, so
            # this falls through to the normal event.Skip() below
            # everywhere else (message list focused, tree focused,
            # header block focused, no message open at all).
            # This mirrors message_view_panel._dispatch_
            # hotkey's own ALT_A for the HTML/WebView case -- that one
            # fires via the JS bridge since EVT_CHAR_HOOK is not
            # reliably seen while the WebView holds native focus, this
            # one is the belt-and-suspenders path for the Text view
            # (a plain wx.TextCtrl) and any build where the bridge
            # could not be injected.
            tab = self._current_message_tab()
            if tab is not None and hasattr(tab, "envelope"):
                focus = wx.Window.FindFocus()
                if focus is not None and (
                    focus is getattr(tab, "body", None)
                    or focus is getattr(tab, "webview", None)
                ):
                    self._run_archive(
                        tab.account, tab.folder, tab.envelope,
                        after_success=tab.close_tab,
                    )
                    return
        event.Skip()


    def _next_tab(self):
        count = self.notebook.GetPageCount()
        if count < 2:
            return
        index = (self.notebook.GetSelection() + 1) % count
        self.notebook.SetSelection(index)

    def _previous_tab(self):
        count = self.notebook.GetPageCount()
        if count < 2:
            return
        index = (self.notebook.GetSelection() - 1) % count
        self.notebook.SetSelection(index)

    def _build_statusbar(self):
        status = self.CreateStatusBar()
        status.SetStatusText(lang.t("main_ui", "ready", default="Ready."))

    def _bind_commands(self):
        self.Bind(wx.EVT_MENU, self._on_new_message, id=wx.ID_NEW)
        self.Bind(wx.EVT_MENU, self._on_exit, id=wx.ID_EXIT)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_MENU, self._on_jump_unified_inbox, id=ID_JUMP_UNIFIED)
        self.Bind(wx.EVT_MENU, self._on_unified_folders, id=ID_UNIFIED_FOLDERS)
        self.Bind(wx.EVT_MENU, self._on_folders_all, id=ID_FOLDERS_ALL)
        self.Bind(wx.EVT_MENU, self._on_folders_unified, id=ID_FOLDERS_UNIFIED)
        self.Bind(wx.EVT_MENU, self._on_sort_by, id=ID_SORT_DATE)
        self.Bind(wx.EVT_MENU, self._on_sort_by, id=ID_SORT_SUBJECT)
        self.Bind(wx.EVT_MENU, self._on_sort_by, id=ID_SORT_FROM)
        self.Bind(wx.EVT_MENU, self._on_sort_order, id=ID_SORT_ASCENDING)
        self.Bind(wx.EVT_MENU, self._on_sort_order, id=ID_SORT_DESCENDING)
        self.Bind(wx.EVT_MENU, self._on_thread_filter, id=ID_THREADS_ALL)
        self.Bind(wx.EVT_MENU, self._on_thread_filter, id=ID_THREADS_WATCHED)
        self.Bind(wx.EVT_MENU, self._on_thread_filter, id=ID_THREADS_IGNORED)
        self.Bind(wx.EVT_MENU, self._on_expand_all_threads, id=ID_EXPAND_ALL_THREADS)
        self.Bind(wx.EVT_MENU, self._on_collapse_all_threads, id=ID_COLLAPSE_ALL_THREADS)
        self.Bind(wx.EVT_MENU, self._on_undo, id=wx.ID_UNDO)
        self.Bind(wx.EVT_MENU_OPEN, self._on_menu_open)
        self.Bind(wx.EVT_MENU, self._on_new_account, id=ID_NEW_ACCOUNT)
        self.Bind(wx.EVT_MENU, self._on_account_settings, id=ID_ACCOUNT_SETTINGS)
        self.Bind(wx.EVT_MENU, self._on_toggle_mute_account, id=ID_MUTE_ACCOUNT)
        self.Bind(wx.EVT_MENU, self._on_toggle_pause_auto_check, id=ID_PAUSE_AUTO_CHECK)
        self.Bind(wx.EVT_MENU, self._on_disable_account, id=ID_DISABLE_ACCOUNT)
        self.Bind(wx.EVT_MENU, self._on_set_default_account, id=ID_SET_DEFAULT_ACCOUNT)
        self.Bind(wx.EVT_MENU, self._on_clear_message_cache, id=ID_CLEAR_MESSAGE_CACHE)
        self.Bind(wx.EVT_MENU, self._on_cache_cleanup_configuration, id=ID_CACHE_CLEANUP)
        self.Bind(wx.EVT_MENU, self._on_remove_account, id=ID_REMOVE_ACCOUNT)
        self.Bind(wx.EVT_MENU, self._on_refresh_folder, id=ID_REFRESH_FOLDER)
        self.Bind(wx.EVT_MENU, self._on_new_folder, id=ID_NEW_FOLDER)
        self.Bind(wx.EVT_MENU, self._on_rename_folder, id=ID_RENAME_FOLDER)
        self.Bind(wx.EVT_MENU, self._on_delete_folder, id=ID_DELETE_FOLDER)
        self.Bind(wx.EVT_MENU, self._on_subscribe_folder, id=ID_SUBSCRIBE_FOLDER)
        self.Bind(wx.EVT_MENU, self._on_unsubscribe_folder, id=ID_UNSUBSCRIBE_FOLDER)
        self.Bind(wx.EVT_MENU, self._on_refresh_all_accounts, id=ID_REFRESH_ALL_ACCOUNTS)
        self.Bind(wx.EVT_MENU, lambda e: self._close_current_tab(), id=wx.ID_CLOSE)
        self.Bind(wx.EVT_MENU, self._on_open_saved_message, id=ID_OPEN_SAVED_MESSAGE)
        self.Bind(wx.EVT_MENU, self._on_save_message_as, id=ID_SAVE_MESSAGE_AS)
        self.Bind(wx.EVT_MENU, self._on_empty_trash, id=ID_EMPTY_TRASH)
        self.Bind(wx.EVT_MENU, self._on_empty_junk, id=ID_EMPTY_JUNK)
        self.Bind(wx.EVT_MENU, self._on_print_message, id=ID_PRINT_MESSAGE)
        self.Bind(wx.EVT_MENU, self._on_print_preview_message, id=ID_PRINT_PREVIEW_MESSAGE)
        self.Bind(wx.EVT_MENU, self._on_settings, id=ID_SETTINGS)
        self.Bind(wx.EVT_MENU, self._on_reply, id=ID_REPLY)
        self.Bind(wx.EVT_MENU, self._on_reply_all, id=ID_REPLY_ALL)
        self.Bind(wx.EVT_MENU, self._on_forward, id=ID_FORWARD)
        self.Bind(wx.EVT_MENU, self._on_delete_message, id=ID_DELETE_MESSAGE)
        self.Bind(wx.EVT_MENU, self._on_delete_permanent, id=ID_DELETE_PERMANENT)
        self.Bind(wx.EVT_MENU, self._on_mark_read, id=ID_MARK_READ)
        self.Bind(wx.EVT_MENU, self._on_mark_unread, id=ID_MARK_UNREAD)
        self.Bind(wx.EVT_MENU, lambda e: self._toggle_flag_from_selection(), id=ID_TOGGLE_FLAG)
        self.Bind(wx.EVT_MENU, lambda e: self._archive_from_selection(), id=ID_ARCHIVE)
        self.Bind(wx.EVT_MENU, lambda e: self._toggle_watch_thread(), id=ID_WATCH_THREAD)
        self.Bind(wx.EVT_MENU, lambda e: self._toggle_ignore_thread(), id=ID_IGNORE_THREAD)
        self.Bind(wx.EVT_MENU, lambda e: self._mark_junk_from_selection(), id=ID_MARK_JUNK)
        self.Bind(wx.EVT_MENU, lambda e: self._mark_not_junk_from_selection(), id=ID_MARK_NOT_JUNK)
        self.Bind(wx.EVT_MENU, self._on_toggle_message_view, id=ID_TOGGLE_MESSAGE_VIEW)
        self.Bind(wx.EVT_MENU, self._on_show_remote_content, id=ID_SHOW_REMOTE_CONTENT)
        self.Bind(wx.EVT_MENU, self._on_increase_message_zoom, id=ID_INCREASE_MESSAGE_ZOOM)
        self.Bind(wx.EVT_MENU, self._on_decrease_message_zoom, id=ID_DECREASE_MESSAGE_ZOOM)
        self.Bind(wx.EVT_MENU, self._on_reset_message_zoom, id=ID_RESET_MESSAGE_ZOOM)
        self.Bind(wx.EVT_MENU, self._on_import_accounts, id=ID_IMPORT_ACCOUNTS)
        self.Bind(wx.EVT_MENU, self._on_export_accounts, id=ID_EXPORT_ACCOUNTS)
        self.Bind(wx.EVT_MENU, self._on_privacy, id=ID_PRIVACY)
        self.Bind(wx.EVT_MENU, self._on_master_password, id=ID_MASTER_PASSWORD)
        self.Bind(wx.EVT_MENU, self._on_announce_sender, id=ID_ANNOUNCE_SENDER)
        self.Bind(
            wx.EVT_MENU, self._on_announce_copy_sender,
            id=ID_ANNOUNCE_COPY_SENDER,
        )
        self.Bind(
            wx.EVT_MENU, self._on_add_sender_to_address_book,
            id=ID_ADD_SENDER_TO_ADDRESS_BOOK,
        )
        self.Bind(wx.EVT_MENU, self._on_about, id=wx.ID_ABOUT)
        self.Bind(wx.EVT_MENU, self._on_toggle_work_offline, id=ID_WORK_OFFLINE)
        self.Bind(wx.EVT_MENU, self._on_sync_offline, id=ID_SYNC_OFFLINE)
        self.Bind(wx.EVT_MENU, self._on_address_book, id=ID_ADDRESS_BOOK)
        self.Bind(wx.EVT_MENU, self._on_search, id=ID_SEARCH_MESSAGES)
        self.Bind(wx.EVT_MENU, self._on_show_related_messages, id=ID_SHOW_RELATED_MESSAGES)
        self.Bind(wx.EVT_MENU, self._on_open_conversation, id=ID_OPEN_CONVERSATION)
        self.Bind(wx.EVT_MENU, self._on_load_more_messages, id=ID_LOAD_MORE_MESSAGES)
        self.Bind(wx.EVT_MENU, self._on_message_filters, id=ID_MESSAGE_FILTERS)
        self.Bind(wx.EVT_MENU, self._on_shortcuts, id=ID_SHORTCUTS)
        self.Bind(wx.EVT_MENU, lambda e: self.mail_panel.envelope_panel.select_all(), id=ID_SELECT_ALL)

    # --- Context menus -------------------------------------------------

    def _tree_item_at_event(self, control, event):
        """
        Resolves which tree item a context-menu invocation targets:
        HitTest at the click position for a real right-click, falling
        back to the current selection for the Application/Menu key or
        Shift+F10 (which report wx.DefaultPosition -- see
        _popup_menu's own handling of the same case). Returns
        (item, data) with data possibly None if the item carries none
        (shouldn't happen for any node this tree creates, but a
        missing data dict is treated as "no folder/account target"
        rather than crashing the context menu).
        """
        position = event.GetPosition()
        item = wx.TreeItemId()
        if position != wx.DefaultPosition:
            item, _flags = control.HitTest(control.ScreenToClient(position))
        if not item.IsOk():
            item = control.GetSelection()
        if not item.IsOk():
            return None, None
        return item, control.GetItemData(item)

    def _show_tree_context_menu(self, control, event):
        _item, data = self._tree_item_at_event(control, event)
        self._tree_context_target = data

        menu = wx.Menu()
        menu.Append(
            ID_NEW_ACCOUNT, lang.menu_label("tree_new_account", "New Account...")
        )
        menu.Append(
            ID_ACCOUNT_SETTINGS,
            lang.menu_label("tree_account_settings", "Account Settings..."),
        )
        mute_item = menu.AppendCheckItem(
            ID_MUTE_ACCOUNT, lang.menu_label("tree_mute_account", "Mute Account")
        )
        target_account = self._find_account((data or {}).get("account_id"))
        mute_item.Enable(target_account is not None)
        mute_item.Check(bool(target_account and target_account.notifications_muted))
        pause_item = menu.AppendCheckItem(
            ID_PAUSE_AUTO_CHECK,
            lang.menu_label("tree_pause_auto_check", "Pause Automatic Checking"),
        )
        pause_item.Enable(target_account is not None)
        pause_item.Check(
            bool(target_account and not getattr(target_account, "auto_check", True))
        )
        disable_item = menu.Append(
            ID_DISABLE_ACCOUNT,
            lang.menu_label("tree_enable_account", "Enable Account")
            if (target_account and not getattr(target_account, "enabled", True))
            else lang.menu_label("tree_disable_account", "Disable Account"),
        )
        disable_item.Enable(target_account is not None)
        default_item = menu.Append(
            ID_SET_DEFAULT_ACCOUNT,
            lang.menu_label("tree_set_default_account", "Set as Default Account"),
        )
        default_item.Enable(
            target_account is not None
            and bool(self.account_manager.accounts)
            and self.account_manager.accounts[0].account_id != target_account.account_id
        )
        menu.Append(
            ID_REMOVE_ACCOUNT,
            lang.menu_label("tree_remove_account", "Remove Account..."),
        )
        menu.AppendSeparator()
        menu.Append(ID_REFRESH_FOLDER, lang.menu_label("tree_refresh", "Refresh"))

        # Folder management (audit finding 42). "New Folder..." is
        # offered for the account root or any folder under it (a
        # top-level folder either way -- see create_mailbox's own
        # docstring on why nesting isn't supported yet). Rename/
        # Delete/Subscribe/Unsubscribe only make sense targeting an
        # actual folder row, and are disabled outright on the six
        # special folders -- renaming or deleting your own Inbox is
        # not a mistake ZBox should let a screen-reader user make with
        # one wrong menu choice.
        is_folder_row = bool(data) and not data.get("is_account_root") and data.get("unified") is None
        has_account = bool(data) and data.get("account_id") is not None
        if has_account:
            menu.AppendSeparator()
            menu.Append(
                ID_NEW_FOLDER, lang.menu_label("tree_new_folder", "New Folder...")
            )
            if is_folder_row:
                folder_label = data.get("folder_label")
                if folder_label == "Trash":
                    menu.Append(
                        ID_EMPTY_TRASH,
                        lang.menu_label("tree_empty_trash", "Empty Trash"),
                    )
                elif folder_label == "Junk":
                    menu.Append(
                        ID_EMPTY_JUNK,
                        lang.menu_label("tree_empty_junk", "Empty Junk"),
                    )
                is_special = bool(data.get("is_special_folder"))
                rename_item = menu.Append(
                    ID_RENAME_FOLDER,
                    lang.menu_label("tree_rename_folder", "Rename Folder..."),
                )
                delete_item = menu.Append(
                    ID_DELETE_FOLDER,
                    lang.menu_label("tree_delete_folder", "Delete Folder..."),
                )
                subscribe_item = menu.Append(
                    ID_SUBSCRIBE_FOLDER,
                    lang.menu_label("tree_subscribe_folder", "Subscribe to Folder"),
                )
                unsubscribe_item = menu.Append(
                    ID_UNSUBSCRIBE_FOLDER,
                    lang.menu_label(
                        "tree_unsubscribe_folder", "Unsubscribe from Folder"
                    ),
                )
                if is_special:
                    rename_item.Enable(False)
                    delete_item.Enable(False)
                    subscribe_item.Enable(False)
                    unsubscribe_item.Enable(False)

        self._popup_menu(control, menu, event)

    def _show_list_context_menu(self, control, event):
        menu = wx.Menu()
        menu.Append(ID_REPLY, lang.menu_label("list_reply", "Reply"))
        menu.Append(ID_REPLY_ALL, lang.menu_label("list_reply_all", "Reply All"))
        menu.Append(ID_FORWARD, lang.menu_label("list_forward", "Forward"))
        menu.AppendSeparator()
        # Read/Unread/Flagged/Junk/Not Junk collapsed into one "Mark"
        # submenu, matching the Message menu's own Mark submenu
        # (_build_menu). Archive stays out here too, for the same
        # reason it stays out there -- it moves the message, it does
        # not mark it. Junk/Not Junk is dynamic: only one of the two
        # is offered, chosen by whether the folder currently being
        # viewed is Junk, since the other action would either be a
        # silent no-op or need explaining why it's greyed out.
        mark_submenu = wx.Menu()
        mark_submenu.Append(ID_MARK_READ, lang.menu_label("mark_read", "Read"))
        mark_submenu.Append(ID_MARK_UNREAD, lang.menu_label("mark_unread", "Unread"))
        flag_item = mark_submenu.AppendCheckItem(
            ID_TOGGLE_FLAG, lang.menu_label("mark_flagged", "Flagged")
        )
        flag_enabled, flag_checked = self._flag_check_state()
        flag_item.Enable(flag_enabled)
        flag_item.Check(flag_checked)
        mark_submenu.AppendSeparator()
        target = self.mail_panel.account_panel.selected_fetch_target()
        viewing_junk = bool(target) and (
            target.get("folder_label") == "Junk" or target.get("unified") == "Junk"
        )
        if viewing_junk:
            mark_submenu.Append(
                ID_MARK_NOT_JUNK, lang.menu_label("mark_not_junk", "Not Junk")
            )
        else:
            mark_submenu.Append(ID_MARK_JUNK, lang.menu_label("mark_junk", "Junk"))
        menu.AppendSubMenu(mark_submenu, lang.menu_label("list_mark", "Mark"))
        menu.Append(ID_ARCHIVE, lang.menu_label("list_archive", "Archive"))
        menu.AppendSeparator()
        thread_state = self._current_thread_watch_ignore_state()
        watch_item = menu.AppendCheckItem(
            ID_WATCH_THREAD, lang.menu_label("message_watch_thread", "Watch Thread")
        )
        ignore_item = menu.AppendCheckItem(
            ID_IGNORE_THREAD, lang.menu_label("message_ignore_thread", "Ignore Thread")
        )
        watch_item.Enable(thread_state is not None)
        ignore_item.Enable(thread_state is not None)
        if thread_state is not None:
            watch_item.Check(thread_state[0])
            ignore_item.Check(thread_state[1])
        menu.AppendSeparator()
        self._append_move_copy_submenus(menu)
        menu.AppendSeparator()
        menu.Append(
            ID_DELETE_MESSAGE,
            lang.menu_label("list_delete_to_trash", "Delete (Move to Trash)"),
        )
        menu.Append(
            ID_DELETE_PERMANENT,
            lang.menu_label("list_delete_permanently", "Delete Permanently"),
        )
        self._popup_menu(control, menu, event)

    def _append_move_copy_submenus(self, menu):
        """
        Adds 'Move To' and 'Copy To' submenus to the context menu,
        populated from the current folder list. Each submenu contains
        all available folders except the current source folder(s) of
        the selected message(s).
        
        When multiple messages from different folders are selected,
        each message is moved/copied from its own source folder to the
        target, allowing flexible bulk operations across the message set.
        (Q3 answer: smart bulk)
        """
        envelopes = self.mail_panel.envelope_panel.get_all_selected_envelopes()
        if not envelopes:
            return
        
        # Skip cache-preview rows
        live_envelopes = [e for e in envelopes if self._is_live_envelope(e)]
        if not live_envelopes:
            return
        
        # Resolve all contexts
        contexts = []
        for envelope in live_envelopes:
            context = self._resolve_envelope_context(envelope)
            if context is not None:
                contexts.append((envelope, context))
        
        if not contexts:
            return
        
        # Get first account for folder list (assumes all are same account
        # for Move To/Copy To purposes; can be refined if needed).
        first_account = contexts[0][1][0]
        
        # Build menus
        move_menu = wx.Menu()
        copy_menu = wx.Menu()
        
        for target_folder in self._folders_for_account(first_account):
            # Resolve display name to real folder (for Gmail mapping, etc.)
            real_folder = _folder_display_to_himalaya(target_folder, first_account)
            
            # Skip if target is a source folder for any selected message
            # (don't show "Move to INBOX" if INBOX is where it already is)
            skip_target = False
            for envelope, (account, source_folder, _) in contexts:
                if real_folder == source_folder:
                    skip_target = True
                    break
            if skip_target:
                continue
            
            # The six standard folders follow the chosen language
            # (the same folder_type_ entries the unified tree uses); a
            # custom folder keeps its own name. Only the text shown and
            # announced changes -- real_folder is what the move uses.
            shown = lang.t(
                "dialogs", "folder_type_" + str(target_folder).lower().replace(" ", "_"),
                default=target_folder,
            )

            # Move To submenu
            move_id = wx.NewIdRef()
            move_menu.Append(move_id, shown)
            move_menu.Bind(
                wx.EVT_MENU,
                lambda evt, tf=real_folder, label=shown: 
                    self._bulk_move_messages(contexts, tf, label),
                id=move_id,
            )
            
            # Copy To submenu
            copy_id = wx.NewIdRef()
            copy_menu.Append(copy_id, shown)
            copy_menu.Bind(
                wx.EVT_MENU,
                lambda evt, tf=real_folder: self._bulk_copy_messages(contexts, tf),
                id=copy_id,
            )
        
        menu.AppendSubMenu(move_menu, lang.menu_label("list_move_to", "Move To"))
        menu.AppendSubMenu(copy_menu, lang.menu_label("list_copy_to", "Copy To"))

    def _popup_menu(self, control, menu, event):
        """
        Works for both invocation methods. The Application/Menu key
        and Shift+F10 report wx.DefaultPosition, so the menu opens
        near the control's current focus point instead of at 0,0.
        Right-click carries the actual click position.
        """
        position = event.GetPosition()
        if position == wx.DefaultPosition:
            position = wx.Point(20, 20)
        control.PopupMenu(menu, position)
        menu.Destroy()

    # --- Command handlers, wired for both menu bar and context menu ---

    def _on_new_message(self, event):
        panel = ComposePanel(self.notebook, self)
        self.open_tab(panel, lang.t('dialogs', 'tab_new_message', default="New Message"))

    def open_compose_from_mailto(self, url):
        """
        Opens a compose tab pre-filled from a mailto: link clicked
        inside a rendered HTML message.

        These used to go to wx.LaunchDefaultBrowser along with every
        other link, which hands the address to whatever the system has
        registered as its mail client -- another program, or nothing
        at all. In a mail client, a "send mail" link in a message is
        the one kind of link that should never leave the app.

        Only to/cc/bcc/subject/body are honoured; see parse_mailto for
        why arbitrary headers named by a link inside an untrusted
        message are ignored.
        """
        fields = parse_mailto(url)
        panel = ComposePanel(
            self.notebook, self,
            to=fields["to"],
            cc=fields["cc"],
            bcc=fields["bcc"],
            subject=fields["subject"],
            body=fields["body"],
        )
        subject = fields["subject"]
        title = subject if subject else lang.t('dialogs', 'tab_new_message', default="New Message")
        if len(title) > 30:
            title = title[:27] + "..."
        self.open_tab(panel, title)
        if fields["to"]:
            # The recipient is already filled in, so ComposePanel's own
            # landing spot (the To field) would make the reader tab
            # past what the link already answered. Start in the body.
            panel.focus_default()
        return panel

    def _on_exit(self, event):
        self._quitting = True
        self.Close()

    def _on_close(self, event):
        # Close to tray first: the X button and Alt+F4 go straight to
        # the tray, closing every tab but Mail on the way, with only a
        # Save Draft or Discard prompt for unsent text (_hide_to_tray).
        # File > Exit and the tray's Quit set _quitting and fall
        # through to a real quit below.
        if self.settings_manager.settings.close_to_tray and not self._quitting:
            if event.CanVeto():
                event.Veto()
            self._hide_to_tray()
            return
        # Quitting with unsent mail open loses it just as surely as
        # closing the tab does. Each unsent compose is shown in turn,
        # selected first so the reader can see which one is being
        # asked about, and any "keep editing" cancels the quit.
        for index in range(self.notebook.GetPageCount() - 1, 0, -1):
            page = self.notebook.GetPage(index)
            if not hasattr(page, "confirm_discard"):
                continue
            if not page.has_unsaved_changes():
                continue
            self.notebook.SetSelection(index)
            if not page.confirm_discard():
                if event.CanVeto():
                    event.Veto()
                page.SetFocus()
                return
        self._shutting_down = True
        self._save_window_state()
        self.idle_manager.stop_all()
        # Kill any Himalaya subprocess still running. The three worker
        # threads are daemons -- at interpreter exit they are frozen
        # mid-call, never drained -- so without this a quit during a
        # 90-second message read or a 60-second send left himalaya.exe
        # running, with its IMAP/SMTP connection open, after ZBox's
        # window was already gone.
        # Stop the workers delivering results before the widgets
        # those results would touch are destroyed -- otherwise every
        # job killed by the shutdown() below comes back as an
        # "Unhandled exception in a worker result callback" against a
        # half-torn-down frame. See HimalayaWorker.stop.
        # Before the pooled connections close: its threads stop at the
        # next step and their batches stay saved for the next start.
        self.delete_queue.stop()
        for worker in (self.worker, self.sync_worker, self.new_mail_worker):
            worker.stop()
        self.lanes.stop()
        himalaya_client.shutdown()
        # Explicit RemoveIcon()+Destroy(): a wx.adv.TaskBarIcon left to
        # be garbage-collected can leave a ghost icon sitting in the
        # Windows tray until the user mouses over it -- a well-known
        # wxTaskBarIcon pitfall, not specific to this app.
        self.tray_icon.RemoveIcon()
        self.tray_icon.Destroy()
        self.Destroy()
        # End the message loop even if some window nobody can see is
        # still alive: live, 24 September 2026, the loop kept running
        # after a quit with nothing on screen, so the process -- and the
        # console launcher.bat started it from -- stayed open.
        app = wx.GetApp()
        if app is not None:
            wx.CallAfter(app.ExitMainLoop)

    def _on_iconize(self, event):
        if event.IsIconized() and self.settings_manager.settings.minimize_to_tray:
            # CallAfter: hiding the frame while Windows is mid-way
            # through its own minimize transition has been a source of
            # timing bugs elsewhere in this app (WebView2 teardown,
            # focus handoff) -- deferring one tick sidesteps the same
            # class of problem here.
            wx.CallAfter(self._hide_to_tray)
        event.Skip()

    def _hide_to_tray(self):
        """Every way into the tray ends here: close to tray (X,
        Alt+F4, Ctrl+W on the Mail tab) and minimize to tray. Closes
        every tab but Mail, newest first, with the same teardown a
        normal tab close uses. An unsent compose asks Save Draft or
        Discard (compose_panel.confirm_for_tray); one whose save
        failed is kept, so nothing typed is lost. Then the Mail tab
        and its message list, which is where reopening lands."""
        for index in range(self.notebook.GetPageCount() - 1, 0, -1):
            try:
                page = self.notebook.GetPage(index)
                confirm = getattr(page, "confirm_for_tray", None)
                if confirm is not None and not confirm():
                    continue
                prepare_close = getattr(page, "prepare_close", None)
                if prepare_close is not None:
                    prepare_close()
                self.notebook.DeletePage(index)
            except RuntimeError:
                logging.getLogger("zbox.main").warning(
                    "A tab was already gone while going to the tray.",
                    exc_info=True,
                )
        if self.notebook.GetSelection() != 0:
            self.notebook.SetSelection(0)
        self.mail_panel.envelope_panel.list_ctrl.SetFocus()
        self._refresh_after_tab_close()
        self.Hide()

    def restore_from_tray(self):
        """Un-hides/un-minimizes ZBox from the tray icon's left-click
        (or Enter/Space on a keyboard-selected tray icon) or its Open
        menu item -- the one path back from either minimize_to_tray's
        Hide() or close_to_tray's Hide()-instead-of-Destroy()."""
        if self.IsIconized():
            self.Iconize(False)
        if not self.IsShown():
            self.Show()
        self.Raise()
        # Always back to the message list, whatever tab was showing:
        # the Mail tab first, because focusing the list while another
        # tab is showing puts focus on a control nobody can see.
        if self.notebook.GetSelection() != 0:
            self.notebook.SetSelection(0)
        self.mail_panel.envelope_panel.list_ctrl.SetFocus()

    def open_settings_from_tray(self):
        self.restore_from_tray()
        self._on_settings(None)

    def quit_from_tray(self):
        self._quitting = True
        self.Close()

    def _refresh_tray_icon(self):
        self.tray_icon.refresh(_total_unread_count(self._tree_folder_lists))

    # --- Window state persistence (audit finding 52) -------------------

    def _restore_window_state(self):
        """Applies the size/position/maximized state saved when ZBox
        last closed, in place of the fixed 1100x700-centered launch
        every previous session used. Runs once, from __init__, in the
        same spot the old unconditional self.Centre() call used to
        sit -- Centre() still runs (via resolve_window_geometry
        returning x=y=None) whenever there is nothing valid to
        restore, so a fresh install/settings.json behaves exactly as
        before."""
        settings = self.settings_manager.settings
        display_rects = [
            (rect.x, rect.y, rect.width, rect.height)
            for rect in (
                wx.Display(i).GetGeometry() for i in range(wx.Display.GetCount())
            )
        ]
        width, height, x, y, maximized = resolve_window_geometry(
            settings.window_x, settings.window_y,
            settings.window_width, settings.window_height,
            settings.window_maximized, display_rects,
        )
        self.SetSize((width, height))
        if x is not None and y is not None:
            self.SetPosition((x, y))
        else:
            self.Centre()
        if settings.stay_maximized:
            # Always keep ZBox in full screen (Settings): overrides
            # whatever resolve_window_geometry decided above. Deliberately
            # a plain Maximize(True), not wx.Frame.ShowFullScreen -- the
            # menu bar and title bar stay exactly as they are, so every
            # Alt-key menu shortcut keeps working unchanged.
            self.Maximize(True)
        # Recorded now (rather than left None) so a maximized launch
        # that the reader never un-maximizes before quitting still has
        # sensible restored bounds to save -- see
        # _on_frame_geometry_changed's own docstring for why this
        # can't just be read back off GetSize()/GetPosition() at
        # close time once maximized.
        position = self.GetPosition()
        self._restored_geometry = (position.x, position.y, width, height)
        if maximized:
            self.Maximize(True)

    def _on_frame_geometry_changed(self, event):
        """Tracks the frame's own restored-state bounds on every
        resize/move, but only while NOT maximized. wx's GetSize()/
        GetPosition() report the full maximized bounds once
        Maximize(True) has been called, not the size/position the
        window would return to on un-maximizing -- so this is the
        only reliable moment to capture "the bounds to come back to"
        for _save_window_state to persist later."""
        if (
            self.settings_manager.settings.stay_maximized
            and not self.IsMaximized()
        ):
            # A resize/move landed the frame un-maximized -- the title
            # bar Restore button, a double-click, or a drag. CallAfter
            # avoids re-entering Maximize() from inside the very EVT_SIZE
            # it's reacting to.
            wx.CallAfter(self.Maximize, True)
            event.Skip()
            return
        if not self.IsMaximized():
            position = self.GetPosition()
            size = self.GetSize()
            self._restored_geometry = (
                position.x, position.y, size.width, size.height,
            )
        event.Skip()

    def _save_window_state(self):
        """Called from _on_close, before Destroy(): persists the
        maximized flag plus the last known non-maximized bounds
        (_restored_geometry, kept current by
        _on_frame_geometry_changed) so the next launch can restore
        both independently -- maximized should reopen maximized, but
        un-maximizing it afterward should land back at a real
        previous size, not whatever the maximized bounds happened to
        be."""
        settings = self.settings_manager.settings
        settings.window_maximized = self.IsMaximized()
        if self._restored_geometry is not None:
            x, y, width, height = self._restored_geometry
            settings.window_x = x
            settings.window_y = y
            settings.window_width = width
            settings.window_height = height
        self.settings_manager.save()

    def _save_selected_folder(self, target):
        """Persists the tree selection immediately (like
        _set_folder_pane_mode's own sort/pane-mode saves) rather than
        deferring to _on_close, since a folder switch is a discrete,
        infrequent event -- not a continuous stream the way window
        resize/move is, so there's no write-storm risk to batch
        against. A no-op write is skipped so opening ZBox and never
        switching folders doesn't touch settings.json at all."""
        account_id, folder, unified_type = target_to_saved_fields(target)
        settings = self.settings_manager.settings
        if (settings.last_selected_account_id == account_id
                and settings.last_selected_folder == folder
                and settings.last_selected_unified_type == unified_type):
            return
        settings.last_selected_account_id = account_id
        settings.last_selected_folder = folder
        settings.last_selected_unified_type = unified_type
        self.settings_manager.save()

    def _on_sash_position_changed(self, event):
        """EVT_SPLITTER_SASH_POS_CHANGED fires once when a drag
        finishes (unlike EVT_SIZE/EVT_MOVE during a window resize),
        so -- unlike window geometry -- there's no reason to defer
        this to _on_close: one settings.json write per drag is
        cheap."""
        settings = self.settings_manager.settings
        position = event.GetSashPosition()
        if event.GetEventObject() is self.mail_panel.outer_splitter:
            if settings.outer_sash_position == position:
                return
            settings.outer_sash_position = position
        elif event.GetEventObject() is self.mail_panel.inner_splitter:
            if settings.inner_sash_position == position:
                return
            settings.inner_sash_position = position
        else:
            return
        self.settings_manager.save()
        event.Skip()

    def _set_folder_pane_mode(self, mode):
        if self.settings_manager.settings.folder_pane_mode == mode:
            return
        self.settings_manager.settings.folder_pane_mode = mode
        self.settings_manager.save()
        self._refresh_tree()
        self.GetStatusBar().SetStatusText(
            lang.t("main_ui", "folder_pane_all", default="Folder pane: All Folders.")
            if mode == "all"
            else lang.t(
                "main_ui", "folder_pane_unified",
                default="Folder pane: Unified Folders.",
            )
        )

    def _on_folders_all(self, event):
        self._set_folder_pane_mode("all")

    def _on_folders_unified(self, event):
        self._set_folder_pane_mode("unified")

    def _on_sort_by(self, event):
        key = {
            ID_SORT_DATE: "date",
            ID_SORT_SUBJECT: "subject",
            ID_SORT_FROM: "from",
        }.get(event.GetId(), "date")
        self._sort_key = key
        self._apply_sort_choice()

    def _on_sort_order(self, event):
        self._sort_ascending = event.GetId() == ID_SORT_ASCENDING
        self._apply_sort_choice()

    def _on_thread_filter(self, event):
        mode = {
            ID_THREADS_ALL: "all",
            ID_THREADS_WATCHED: "watched",
            ID_THREADS_IGNORED: "ignored",
        }.get(event.GetId(), "all")
        self.mail_panel.envelope_panel.set_thread_filter(mode)

    def _on_expand_all_threads(self, event):
        self.mail_panel.envelope_panel.expand_all_threads()

    def _on_collapse_all_threads(self, event):
        self.mail_panel.envelope_panel.collapse_all_threads()

    def _thread_state_store(self, account_id):
        """
        Returns (creating and caching on first use) the
        ThreadStateStore for this account -- one small JSON file per
        account (see thread_state.py), same lazy-cache shape as
        _tree_folder_lists above for the account tree's own
        per-account server folder lists.
        """
        store = self._thread_state_stores.get(account_id)
        if store is None:
            store = ThreadStateStore(self.paths, account_id)
            self._thread_state_stores[account_id] = store
        return store

    def _junk_origin_store(self, account_id):
        """
        Returns (creating and caching on first use) the
        JunkOriginStore for this account -- one small JSON file per
        account (see junk_origin.py), same lazy-cache shape as
        _thread_state_store above.
        """
        store = self._junk_origin_stores.get(account_id)
        if store is None:
            store = JunkOriginStore(self.paths, account_id)
            self._junk_origin_stores[account_id] = store
        return store

    def _get_phishing_list(self):
        """The PhishingList, loaded on first use. Called only on the
        sync worker, which runs one job at a time, so the lazy load
        cannot race itself; a few hundred thousand domains are not
        read on the UI thread."""
        if self._phishing_list is None:
            self._phishing_list = PhishingList(self.paths)
        return self._phishing_list

    def _own_addresses(self):
        """Every address the configured accounts send as, so Mark as
        Junk on a message from yourself never blocks you."""
        addresses = set()
        for account in self.account_manager.accounts:
            for value in (getattr(account, "identity_email", None), getattr(account, "login_email", None)):
                address = junk_rules_module.normalize_address(value)
                if address:
                    addresses.add(address)
        return addresses

    def _expect_arrivals(self, account, folder, message_id_headers):
        """Records messages ZBox is about to move into `folder`, before
        the move starts, so a listing that sees them land -- a poll or
        an IDLE refresh, whichever comes first -- does not treat them as
        new mail. Rescuing a message from Junk used to set off the
        new-mail sound and the filters, and the old spam filter then
        sent it straight back."""
        self._expected_arrivals.add(account.account_id, folder, message_id_headers)

    def _current_thread_flags(self, account_id=None):
        """
        (watched_keys, ignored_keys) for `account_id`'s currently
        selected folder, or for whichever account/folder the tree
        has selected when account_id is None -- the callable
        EnvelopeListPanel reads fresh on every render (see its own
        thread_flags_provider), passing its own account_id explicitly
        for a Unified folder (each thread there can belong to a
        different account) and leaving it out for an ordinary
        single-account folder, where the tree selection already names
        the one account that matters. Either way, toggling Watch/
        Ignore on a thread shows up immediately without that panel
        needing to track account/folder state of its own.

        Empty sets (never an error) whenever nothing resolves --
        nothing selected, or an account that's since been removed --
        so a missing selection just means every thread's filter
        membership is "no", not a crash.
        """
        target = self.mail_panel.account_panel.selected_fetch_target()
        if not target:
            return set(), set()
        resolved_account_id = account_id if account_id is not None else target.get("account_id")
        account = self._find_account(resolved_account_id)
        if account is None:
            return set(), set()
        folder = target.get("folder")
        if folder is None:
            folder = _folder_display_to_himalaya(target.get("unified"), account)
        store = self._thread_state_store(account.account_id)
        return store.watched_keys(folder), store.ignored_keys(folder)

    def _current_thread_selection(self):
        """
        (thread_key, account, folder) for whatever is currently
        selected in the main message list, or None if there is
        nothing usable to act on -- the shared starting point for
        Watch Thread / Ignore Thread, whichever of the menu, the
        right-click context menu or the bare W/K keys triggered it.

        thread_key comes from EnvelopeListPanel.selected_thread_key,
        which already resolves to the thread's real root Message-ID
        even when the selection is a reply row inside an EXPANDED
        thread, not that reply's own id -- using the reply's own id
        here would silently create a second, unrelated watch/ignore
        entry for what is actually the same thread the collapsed row
        and the View > Threads filters already key off of.

        account comes from the selected envelope's own
        _zbox_account_id when set (Unified folders mix accounts, so
        the tree's own selection has none of its own) and falls back
        to the tree selection's account otherwise -- same resolution
        _resolve_envelope_context already uses for every other
        per-message action.
        """
        thread_key = self.mail_panel.envelope_panel.selected_thread_key()
        if thread_key is None:
            return None
        envelope = self.mail_panel.envelope_panel.selected_envelope()
        target = self.mail_panel.account_panel.selected_fetch_target()
        if envelope is None or not target:
            return None
        account_id = envelope.get("_zbox_account_id") or target.get("account_id")
        account = self._find_account(account_id)
        if account is None:
            return None
        folder = target.get("folder")
        if folder is None:
            folder = _folder_display_to_himalaya(target.get("unified"), account)
        return thread_key, account, folder

    def _current_thread_watch_ignore_state(self):
        """(is_watched, is_ignored) for the currently selected
        thread, or None with nothing usable selected -- used to set
        the Watch Thread / Ignore Thread checkmarks wherever they
        appear (Message menu, right-click) without duplicating
        _current_thread_selection's own resolution in each place."""
        selection = self._current_thread_selection()
        if selection is None:
            return None
        thread_key, account, folder = selection
        store = self._thread_state_store(account.account_id)
        return store.is_watched(folder, thread_key), store.is_ignored(folder, thread_key)

    def _toggle_watch_thread(self):
        """Message > Watch Thread, its right-click twin, and the
        bare W key: flips Watched for whichever thread is currently
        selected. Display-only, like every other thread_state.py
        write (see that module's own docstring) -- this never moves
        or touches the underlying messages, only what the View >
        Threads > Watched Threads filter shows."""
        selection = self._current_thread_selection()
        if selection is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "actions_announcements", "no_thread_selected",
                default="No thread selected.",
            ))
            self.announce_action(
                lang.t("actions_announcements", "no_thread_selected",
                       default="No thread selected."),
                title=lang.t(
                    "dialogs", "title_watch_thread", default="Watch Thread",
                ),
            )
            return
        thread_key, account, folder = selection
        store = self._thread_state_store(account.account_id)
        new_state = not store.is_watched(folder, thread_key)
        store.set_watched(folder, thread_key, new_state)
        self.mail_panel.envelope_panel.refresh_thread_flags()
        spoken = lang.t(
            "actions_announcements",
            "thread_watched" if new_state else "thread_not_watched",
            default="Thread watched." if new_state else "Thread no longer watched.",
        )
        self.GetStatusBar().SetStatusText(spoken)
        self.announce_action(
            spoken,
            title=lang.t("dialogs", "title_watch_thread", default="Watch Thread"),
        )

    def _toggle_ignore_thread(self):
        """Message > Ignore Thread, its right-click twin, and the
        bare K key (matching Thunderbird's own "kill" mnemonic).
        Same display-only guarantee as _toggle_watch_thread above --
        an ignored thread is only ever hidden by the Ignored Threads
        filter, never deleted, moved, or otherwise touched."""
        selection = self._current_thread_selection()
        if selection is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "actions_announcements", "no_thread_selected",
                default="No thread selected.",
            ))
            self.announce_action(
                lang.t("actions_announcements", "no_thread_selected",
                       default="No thread selected."),
                title=lang.t(
                    "dialogs", "title_ignore_thread", default="Ignore Thread",
                ),
            )
            return
        thread_key, account, folder = selection
        store = self._thread_state_store(account.account_id)
        new_state = not store.is_ignored(folder, thread_key)
        store.set_ignored(folder, thread_key, new_state)
        self.mail_panel.envelope_panel.refresh_thread_flags()
        spoken = lang.t(
            "actions_announcements",
            "thread_ignored" if new_state else "thread_not_ignored",
            default="Thread ignored." if new_state else "Thread no longer ignored.",
        )
        self.GetStatusBar().SetStatusText(spoken)
        self.announce_action(
            spoken,
            title=lang.t(
                "dialogs", "title_ignore_thread", default="Ignore Thread",
            ),
        )

    def _refresh_thread_menu_items(self):
        """Keeps Message > Watch Thread / Ignore Thread's checkmarks
        and enabled state in sync with whatever is currently
        selected in the message list -- disabled with nothing
        selected, same reasoning as Accounts > Mute Account having
        no target of its own to act on. Called from _on_menu_open as
        the Message menu is about to open, same pattern as
        _refresh_mute_account_menu_item for the Accounts menu."""
        watch_item = self._message_menu.FindItemById(ID_WATCH_THREAD)
        ignore_item = self._message_menu.FindItemById(ID_IGNORE_THREAD)
        if watch_item is None or ignore_item is None:
            return
        state = self._current_thread_watch_ignore_state()
        watch_item.Enable(state is not None)
        ignore_item.Enable(state is not None)
        watch_item.Check(bool(state and state[0]))
        ignore_item.Check(bool(state and state[1]))

    def _apply_sort_choice(self):
        """Redraws the list in the chosen order and remembers it.
        Saved to settings rather than kept for the session: re-picking
        a sort order at every launch is a chore, and a screen reader
        user has no column header to glance at to see where the list
        currently stands."""
        self.mail_panel.envelope_panel.sort_by(self._sort_key, self._sort_ascending)
        settings = self.settings_manager.settings
        if (getattr(settings, "sort_key", None) == self._sort_key
                and getattr(settings, "sort_ascending", None) == self._sort_ascending):
            return
        settings.sort_key = self._sort_key
        settings.sort_ascending = self._sort_ascending
        try:
            self.settings_manager.save()
        except Exception:
            logging.getLogger("zbox.main").warning(
                "Could not save the sort order.", exc_info=True
            )
        label = {"date": "Date", "subject": "Subject", "from": "From"}.get(
            self._sort_key, "Date"
        )
        direction = "ascending" if self._sort_ascending else "descending"
        self.GetStatusBar().SetStatusText(
            lang.t(
                "main_ui", "sorted_ascending",
                default="Sorted by %s, ascending." % label, field=label,
            )
            if self._sort_ascending
            else lang.t(
                "main_ui", "sorted_descending",
                default="Sorted by %s, descending." % label, field=label,
            )
        )

    def _on_unified_folders(self, event):
        dialog = UnifiedFoldersDialog(self, self.settings_manager.settings)
        if dialog.ShowModal() == wx.ID_OK:
            self.settings_manager.settings = dialog.apply_to_settings()
            self.settings_manager.save()
            self._refresh_tree()
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "unified_folders_updated",
                default="Unified folders updated.",
            ))
        dialog.Destroy()

    def _no_account_message(self, title):
        """Says why there is no account to act on -- open_issues V2.

        "Select an account in the tree first" is true when the tree is
        populated and nothing is highlighted. It is a lie when the
        account list could not be opened at all, which is exactly the
        state a folder carried to a new computer starts in: the tree
        is empty because the file is locked, not because the user
        forgot to click. Being told to select something that cannot
        exist invites adding the accounts again, over a list that was
        only unreadable.

        The startup path already words this correctly (see the
        load_error branch in __init__). This carries the same
        explanation into every later gate.
        """
        if self.account_manager.is_locked():
            wx.MessageBox(
                lang.t(
                    "errors", "account_list_no_accounts",
                    default="ZBox could not open your account list this session "
                    "(%s), so there are no accounts to select. Nothing has "
                    "been lost and nothing has been changed. Close ZBox and "
                    "start it again, and enter your master password when "
                    "you are asked for it." % self.account_manager.load_error,
                    error=self.account_manager.load_error,
                ),
                title, wx.OK | wx.ICON_WARNING,
            )
            return
        wx.MessageBox(
            lang.t(
                "dialogs", "select_account_first",
                default="Select an account in the tree first.",
            ),
            title, wx.OK | wx.ICON_INFORMATION,
        )

    def _on_new_account(self, event):
        # First line, not only the last one. add_account refuses a
        # locked list as well, but only after the whole wizard has
        # been filled in -- display name, both addresses, four server
        # fields and a password -- which is a long way to walk to be
        # told nothing will be saved (open_issues V4). Checked before
        # _ensure_master_password_set too, so a locked list does not
        # also get asked to invent a master password it cannot use.
        if self.account_manager.refuse_if_locked("add an account"):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "account_not_added", default="Account not added."
            ))
            return
        # Asked before the account's password is stored rather than
        # after, so the very first secret is already covered and the
        # user hears about the master password at the moment its
        # purpose is obvious. Setting one later still works and
        # re-wraps the existing key.
        self._ensure_master_password_set(
            lang.t('dialogs', 'mp_prompt_new_account', default="ZBox is about to save this account's password. Set a "
            "master password first so the account can still be opened "
            "if you move ZBox to another computer.")
        )
        wizard = AccountWizard(self, self.account_manager)
        account = wizard.run()
        if account is None:
            return  # cancelled
        if not self.account_manager.add_account(account):
            # Refused. _report_locked has already said why through
            # notify_error, and nothing was written, so none of the
            # refreshes below have an account to refresh -- and the
            # status bar must not claim one was added.
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "account_not_added", default="Account not added."
            ))
            return
        self._refresh_tree()
        self.idle_manager.sync(self.account_manager.accounts)
        self._refresh_real_folders_for_account(account)
        if self._search_tab is not None:
            self._search_tab.refresh_accounts()
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "account_added",
            default=f"Account added: {account.identity_email}",
            account=account.identity_email,
        ))

    def _on_account_settings(self, event):
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_account_settings', default="Account Settings"))
            return

        folder_choices = _resolve_search_folders(
            self._folders_for_account(account), account,
        )
        is_default = bool(self.account_manager.accounts) and self.account_manager.accounts[0].account_id == account.account_id
        dialog = AccountSettingsDialog(self, account, folder_choices, is_default=is_default)
        if dialog.ShowModal() == wx.ID_OK:
            updated = dialog.apply_to_account()
            if not self.account_manager.update_account(updated):
                # Refused, and already reported. Nothing reached
                # disk, so the refreshes below would be describing a
                # save that did not happen.
                #
                # apply_to_account writes onto the live Account
                # before anything is saved, so the edits are taken
                # back off it -- including the typed password, which
                # update_account only blanks on the path that stores
                # it and would otherwise sit on the object in the
                # clear for the rest of the session.
                dialog.restore_account()
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "account_settings_not_saved",
                    default="Account settings not saved.",
                ))
                dialog.Destroy()
                return
            # Host may have changed, which changes which real names
            # the special folders resolve to (Gmail vs. everyone
            # else) -- drop the stale list rather than show folders
            # from the account's previous host.
            self._tree_folder_lists.pop(updated.account_id, None)
            self._refresh_tree()
            # Password, host or port may have changed -- restart this
            # account's watcher with the new details rather than
            # leaving it running against the old ones. sync() only
            # touches accounts whose id isn't already running, so
            # force this one account's restart explicitly.
            self.idle_manager.stop_one(updated.account_id)
            self.idle_manager.sync(self.account_manager.accounts)
            # V18: a disabled account (via the checkbox in this same
            # dialog, or set earlier through Accounts > Disable
            # Account) must not be contacted here either -- this call
            # was unconditional before, which meant saving any other
            # settings change on an already-disabled account still
            # fetched its folder list.
            if updated.enabled:
                self._refresh_real_folders_for_account(updated)
            if dialog.wants_default():
                self.account_manager.set_default_account(updated.account_id)
                self._refresh_tree()
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "account_updated",
                default=f"Account updated: {updated.identity_email}",
                account=updated.identity_email,
            ))
        dialog.Destroy()

    def _on_edit_signature(self, event):
        """Compose > Edit Signature: the formatted signature editor,
        for the account an open compose tab is sending as, or for the
        account selected in the tree when there is no compose tab.

        Saved through the same update_account path Account Settings
        uses, and put back the same way when that save is refused --
        the edit lands on the live Account before anything reaches
        disk, and every open tab is holding that same object.
        """
        account = None
        index = self.notebook.GetSelection()
        page = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        selected = getattr(page, "_selected_account", None)
        if selected is not None:
            account = selected()
        if account is None:
            account = self._find_account(
                self.mail_panel.account_panel.selected_account_id()
            )
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_edit_signature', default="Edit Signature"))
            return

        dialog = SignatureEditDialog(self, account.signature_html, account.signature)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            previous = (account.signature, account.signature_html)
            account.signature = dialog.signature_text
            account.signature_html = dialog.signature_html
            if not self.account_manager.update_account(account):
                account.signature, account.signature_html = previous
                self.GetStatusBar().SetStatusText(lang.t(
                    "actions_announcements", "signature_not_saved",
                    default="Signature not saved.",
                ))
                self.announce_action(
                    lang.t("actions_announcements", "signature_not_saved",
                           default="Signature not saved."),
                    title=lang.t(
                        "dialogs", "title_edit_signature",
                        default="Edit Signature",
                    ),
                )
                return
            self.GetStatusBar().SetStatusText(lang.t(
                "actions_announcements", "signature_saved",
                default="Signature saved.",
            ))
            self.announce_action(
                lang.t("actions_announcements", "signature_saved",
                       default="Signature saved."),
                title=lang.t(
                    "dialogs", "title_edit_signature", default="Edit Signature",
                ),
            )
        finally:
            dialog.Destroy()

    def _on_set_default_account(self, event):
        """Account tree context menu's 'Set as Default Account':
        promotes whichever account the context menu was opened on
        (see _show_tree_context_menu, which sets _tree_context_target
        right before popping the menu up) to the front of the account
        list. See AccountManager.set_default_account for what that
        actually changes."""
        data = self._tree_context_target
        account = self._find_account((data or {}).get("account_id"))
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_set_default', default="Set as Default Account"))
            return
        if not self.account_manager.set_default_account(account.account_id):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "default_account_failed",
                default="Could not set the default account.",
            ))
            return
        self._refresh_tree()
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "default_account_set",
            default=f"{account.identity_email} is now the default account.",
            account=account.identity_email,
        ))

    def _on_toggle_pause_auto_check(self, event):
        """Tools menu / account context menu's "Pause Automatic
        Checking" quick toggle: the same auto_check flag Account
        Settings' "Check for new messages automatically" edits, for
        the account selected in the tree. Checked means paused. The
        watcher is stopped and re-synced the way Account Settings
        does it, so pausing ends IDLE at once and resuming starts it."""
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_pause_auto_check', default="Pause Automatic Checking"))
            return
        previous = getattr(account, "auto_check", True)
        account.auto_check = not event.IsChecked()
        if not self.account_manager.update_account(account):
            # Nothing was written: put the flag back. The check mark
            # is re-synced from it whenever a menu opens.
            account.auto_check = previous
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "auto_check_not_saved",
                default="Automatic checking setting not saved.",
            ))
            return
        self.idle_manager.stop_one(account.account_id)
        self.idle_manager.sync(self.account_manager.accounts)
        name = account.identity_email
        if account.auto_check:
            spoken = lang.t(
                "actions_announcements", "auto_check_resumed",
                default=f"Automatic checking on for {name}.", account=name,
            )
        else:
            spoken = lang.t(
                "actions_announcements", "auto_check_paused",
                default=f"Automatic checking paused for {name}.", account=name,
            )
        logging.getLogger("zbox.main").info(
            "Account %s auto_check=%s.", account.account_id, account.auto_check,
        )
        self.GetStatusBar().SetStatusText(spoken)
        self.announce_action(
            spoken, title=lang.t("dialogs", "title_account", default="Account")
        )

    def _on_toggle_mute_account(self, event):
        """Accounts menu / account context menu's "Mute Account"
        quick toggle -- the same notifications_muted flag Account
        Settings' own checkbox edits, for whichever account is
        currently selected in the tree, without opening the full
        dialog. See mail_fetch.py's new-mail gating for what muting
        actually suppresses."""
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_mute_account', default="Mute Account"))
            return
        previous = account.notifications_muted
        account.notifications_muted = event.IsChecked()
        if not self.account_manager.update_account(account):
            # Nothing was written, so the flag goes back rather than
            # spending the rest of the session disagreeing with the
            # file. The menu checkmark is re-synced from this flag
            # whenever the menu opens (_on_menu_open), so it corrects
            # itself without being set here.
            account.notifications_muted = previous
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "mute_not_saved", default="Mute setting not saved."
            ))
            return
        self.GetStatusBar().SetStatusText(
            lang.t(
                "main_ui", "account_muted",
                default=f"{account.identity_email} muted.",
                account=account.identity_email,
            )
            if account.notifications_muted
            else lang.t(
                "main_ui", "account_unmuted",
                default=f"{account.identity_email} unmuted.",
                account=account.identity_email,
            )
        )

    def _on_message_filters(self, event):
        """
        Tools > Message Filters (audit finding 45). Scoped to
        whichever account is selected in the tree, the same as
        Account Settings -- see AccountSettingsDialog's own handler.
        """
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_message_filters', default="Message Filters"))
            return

        folder_choices = _resolve_search_folders(
            self._folders_for_account(account), account,
        )
        dialog = FilterRulesDialog(
            self, account.display_name or account.identity_email,
            account.filters, folder_choices,
        )
        result = dialog.ShowModal()
        if result in (wx.ID_OK, FilterRulesDialog.RUN_NOW):
            previous_filters = account.filters
            account.filters = dialog.rules
            if not self.account_manager.save_changes("save these filters"):
                # Refused, and already reported. The rules were put
                # on the live account before the save, so they go
                # back -- and Run Now does not run, because applying
                # rules that reached no disk would move real messages
                # on the strength of a change the user has just been
                # told did not happen.
                account.filters = previous_filters
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "filters_not_saved", default="Filters not saved."
                ))
                dialog.Destroy()
                return
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "filters_saved",
                default=f"Filters saved for {account.identity_email}.",
                account=account.identity_email,
            ))
        if result == FilterRulesDialog.RUN_NOW:
            self._run_filters_on_folder(account, dialog.run_folder)
        dialog.Destroy()

    def _apply_filter_rule_actions(self, account, folder, message_id, rule, moves=None, message_id_header=None):
        """
        Executes one matched filter_rules.FilterRule's actions
        against one message via real himalaya_client calls. Mark
        read and flag first (both act on the message wherever it
        currently is), move last (changes which folder it's in) --
        a rule with no move_to simply skips that step, and a move
        into the folder the message is already in is skipped too.
        There is no delete action here (see filter_rules.py):
        Delete/Archive/Junk already exist as their own commands, and
        "move to Trash by filter" is just move_to with Trash as the
        target.

        With a `moves` list, the move is not made here: it is
        recorded as (message_id, message_id_header, destination) for
        _run_filter_moves to batch, once per message -- the first
        matching rule's destination wins, as it did when the second
        move simply failed.
        """
        if rule.mark_read:
            himalaya_client.mark_as_read(self.paths, account, message_id, folder=folder)
        if rule.flag:
            himalaya_client.set_flagged(self.paths, account, message_id, folder, True)
        if rule.move_to and rule.move_to != folder:
            if moves is None:
                himalaya_client.move_message(self.paths, account, message_id, folder, rule.move_to)
            elif all(move[0] != message_id for move in moves):
                moves.append((message_id, message_id_header, rule.move_to))

    def _run_filters_on_folder(self, account, folder):
        """
        Finding 45's explicitly named manual half: applies this
        account's enabled rules, in order, to every message already
        in `folder` (not just new arrivals). Lists the folder on the
        interactive worker (one quick call), then hands whichever
        messages actually matched something to the same bulk-action
        machinery every multi-select action already uses --
        continue-on-error, a Cancel button, a spoken summary if
        anything failed -- rather than a bespoke loop.
        """
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "filters_checking",
            default=f"Checking {folder} against your filters...", folder=folder,
        ))

        def fetch_work():
            return himalaya_client.list_envelopes(self.paths, account, folder, backend="imap")

        def on_fetch_success(envelopes):
            contexts = []
            for envelope in envelopes:
                message_id = envelope.get("id")
                if message_id is None:
                    continue
                if not filter_rules.matching_rules(account.filters, envelope):
                    continue
                contexts.append((envelope, (account, folder, message_id)))

            if not contexts:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "filters_none_matched",
                    default=f"Run Now: no messages in {folder} matched any "
                            f"enabled rule.",
                    folder=folder,
                ))
                return

            # Filled on the worker while the bulk action runs, read on
            # the UI thread once it has finished.
            moves = []

            def action(envelope, account, folder, message_id):
                for rule in filter_rules.matching_rules(account.filters, envelope):
                    self._apply_filter_rule_actions(
                        account, folder, message_id, rule,
                        moves=moves, message_id_header=envelope.get("message-id"),
                    )

            self._run_bulk_action(
                contexts,
                lang.t('main_ui', 'filters_running', default='Running filters on {folder}', folder=folder),
                lang.t('main_ui', 'filters_applied', default='Filters applied to {count} message(s) in {folder}.', count=len(contexts), folder=folder),
                lang.t('main_ui', 'filters_failed', default="Could not run filters."),
                lang.t('dialogs', 'filters_title', default="Run Filters"),
                lang.t('main_ui', 'filters_failed', default="Could not run filters."),
                action,
                on_success_extra=lambda _result=None: self._run_filter_moves(account, folder, moves),
            )

        def on_fetch_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "filters_run_failed", default="Could not run filters."
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "filters_list_failed",
                    default=f"Could not list messages in {folder}.\n\n{exc}",
                    folder=folder, error=exc,
                ),
                lang.t("dialogs", "title_run_filters", default="Run Filters"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(
            fetch_work, on_fetch_success, on_fetch_error,
        )

    def _run_filter_moves(self, account, folder, moves):
        """
        Run Now's moves, made after every mark-read and flag action
        and batched: one move per destination through
        move_messages_batch, rather than one subprocess per message.
        That also brings the batch path's recheck before counting a
        failure, and records the arrivals so they are not new mail.
        """
        if not moves:
            self._auto_refresh_current_folder()
            return
        by_destination = {}
        for message_id, message_id_header, destination in moves:
            by_destination.setdefault(destination, []).append(
                (message_id, folder, message_id_header)
            )
        for destination, items in by_destination.items():
            self._expect_arrivals(account, destination, [item[2] for item in items if item[2]])
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "filters_moving",
            default=f"Moving {len(moves)} filtered message(s)...",
            count=len(moves),
        ))

        def work():
            failed = []
            for destination, items in by_destination.items():
                _moved, group_failed = himalaya_client.move_messages_batch(
                    self.paths, account, items, destination,
                )
                failed.extend(group_failed)
            return failed

        def on_success(failed):
            if failed:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "filters_move_failures",
                    default=f"Filters: {len(failed)} message(s) could not be "
                            f"moved.",
                    count=len(failed),
                ))
                wx.MessageBox(
                    lang.t(
                        "dialogs", "filters_left_behind",
                        default=f"{len(failed)} message(s) matched a filter but are still in {folder}.",
                        count=len(failed), folder=folder,
                    ),
                    lang.t("dialogs", "title_run_filters", default="Run Filters"),
                    wx.OK | wx.ICON_WARNING,
                )
            else:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "filters_moved",
                    default=f"Filters moved {len(moves)} message(s) from {folder}.",
                    count=len(moves), folder=folder,
                ))
            self._refresh_after_removal()

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "filters_move_failed",
                default="Could not move filtered messages.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "filters_move_failed",
                    default=f"Could not move filtered messages.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_run_filters", default="Run Filters"),
                wx.OK | wx.ICON_ERROR,
            )
            self._refresh_after_removal()

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _apply_automatic_filters(self, account, folder, new_envelopes):
        """
        Finding 45's automatic half: runs this account's enabled
        filters against envelopes _prefetch_message_bodies just
        identified as genuinely new (mail_fetch.py), matching
        Thunderbird's own default "Getting New Mail" trigger.

        Scoped to the account's real Inbox folder only -- filters
        test incoming mail, and a "new" entry anywhere else is either
        this account's own sent copy or a message that arrived by
        some other means (a manual move, another client), neither of
        which is "incoming mail" in the sense a filter author means.

        Runs on sync_worker, the same background/multi-message lane
        the prefetch this rides alongside already uses, since acting
        on a batch means one subprocess call per mark or flag action
        per message, plus one batched move per destination folder.
        A failed action for one message (a
        transient server hiccup) is logged and skipped, never allowed
        to abort the rest of the batch -- same reasoning as
        prefetch_messages and related_messages_panel's own per-
        candidate guards.
        """
        if not account.filters or not new_envelopes:
            return
        if folder != _folder_display_to_himalaya("Inbox", account):
            return

        matched = [
            (envelope, filter_rules.matching_rules(account.filters, envelope))
            for envelope in new_envelopes
        ]
        matched = [(envelope, rules) for envelope, rules in matched if rules]
        if not matched:
            return

        # Recorded here, on the UI thread and before any move starts,
        # for the same reason every other move does it: a filtered
        # message landing in a watched folder is not new mail.
        for envelope, rules in matched:
            header = envelope.get("message-id")
            destination = next(
                (rule.move_to for rule in rules if rule.move_to and rule.move_to != folder),
                None,
            )
            if header and destination:
                self._expect_arrivals(account, destination, [header])

        def work():
            changed = False
            moves = []
            for envelope, rules in matched:
                message_id = envelope.get("id")
                if message_id is None:
                    continue
                for rule in rules:
                    try:
                        self._apply_filter_rule_actions(
                            account, folder, message_id, rule,
                            moves=moves, message_id_header=envelope.get("message-id"),
                        )
                        changed = True
                    except himalaya_client.HimalayaError as exc:
                        logging.getLogger("zbox.main").warning(
                            "Automatic filter '%s' failed for a message in %s/%s: %s",
                            rule.name, account.account_id, folder, exc,
                        )
            by_destination = {}
            for message_id, message_id_header, destination in moves:
                by_destination.setdefault(destination, []).append(
                    (message_id, folder, message_id_header)
                )
            for destination, items in by_destination.items():
                _moved, failed = himalaya_client.move_messages_batch(
                    self.paths, account, items, destination,
                )
                if failed:
                    logging.getLogger("zbox.main").warning(
                        "Automatic filters could not move %d of %d message(s) from %s to %s on %s.",
                        len(failed), len(items), folder, destination, account.account_id,
                    )
                changed = True
            return changed

        def on_success(changed):
            if changed:
                self._request_auto_refresh()

        def on_error(exc):
            logging.getLogger("zbox.main").warning(
                "Automatic filters failed for %s/%s: %s", account.account_id, folder, exc
            )

        self.sync_worker.submit(work, on_success, on_error)

    def _on_clear_message_cache(self, event):
        """Clear Offline Message Cache: Ctrl+Shift+Delete, or the button in
        Tools > Cache Clean Up Configuration.

        The key was Alt+Shift+C until 10 September 2026. It never
        reached ZBox: Windows treats Alt+Shift as the keyboard layout
        switch, and a live log showed the keystrokes arriving as a
        run of shift+alt events with keycode 0 while the layout
        changed underneath. Ctrl+Shift+Delete is free in the
        accelerator table, is what browsers use for the same action,
        and nothing intercepts it.

        Deletes every profile's local Maildir copy and cached message
        bodies, including any left behind by an account that was
        removed. Nothing is deleted from any mail server: the cache
        is a copy, and what goes comes back on demand or on the next
        sync.

        It exists because the cache is the only thing that can hold
        stale or duplicated entries. The message list shows what is on
        disk, so when the list looks wrong this is the thing to reset.
        """
        confirm = wx.MessageBox(
            lang.t(
                "dialogs", "cache_clear_confirm",
                default="Clear the offline message cache for every account?\n\n"
                "This deletes ZBox's local copies only. Nothing is removed "
                "from any mail server, and messages are downloaded again as "
                "you need them.",
            ),
            lang.t(
                "dialogs", "title_clear_cache",
                default="Clear Offline Message Cache",
            ),
            wx.YES_NO | wx.ICON_WARNING,
        )
        if confirm != wx.YES:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "cache_not_cleared", default="Cache not cleared."
            ))
            return

        try:
            files, size = himalaya_client.clear_offline_caches(self.paths)
        except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
            logging.getLogger("zbox.main").exception(
                "Clearing the offline message cache failed."
            )
            wx.MessageBox(
                lang.t(
                    "errors", "cache_clear_failed",
                    default="Could not clear the cache.\n\n%s" % exc, error=exc,
                ),
                lang.t(
                    "dialogs", "title_clear_cache",
                    default="Clear Offline Message Cache",
                ),
                wx.OK | wx.ICON_ERROR,
            )
            return

        message = lang.t(
            "actions_announcements", "cache_cleared",
            default="Cleared {files} cached file(s), {size} MB.",
            files=files, size="%.1f" % (size / 1048576.0),
        )
        self.GetStatusBar().SetStatusText(message)
        self.announce(
            message,
            title=lang.t(
                "dialogs", "title_clear_cache",
                default="Clear Offline Message Cache",
            ),
        )

    def _on_disable_account(self, event):
        """Accounts menu / account context menu's "Disable Account".

        Toggles whether ZBox contacts this account at all. Disabled,
        it gets no folder listing, no unified fetch, no Refresh All,
        no IDLE watcher and no search, and its IDLE connection is
        dropped on the spot. Nothing is deleted: the account, its
        stored password and its Himalaya config section all stay, and
        it remains in the tree marked "(disabled)" so it can be
        switched back on. That is the whole difference between this
        and Remove Account.

        The toggle reads the account's own current state rather than
        the menu item's label, so it is right whichever menu it was
        invoked from and regardless of whether either menu's label
        happened to be stale.
        """
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_disable_account', default="Disable Account"))
            return

        account.enabled = not account.enabled
        if not self.account_manager.save_changes("change this account"):
            # Refused and already reported. Put the flag back, so the
            # menus and the tree keep telling the truth about what is
            # actually stored.
            account.enabled = not account.enabled
            return

        name = account.display_name or account.identity_email
        state = "enabled" if account.enabled else "disabled"
        spoken = lang.t(
            "actions_announcements",
            "account_enabled" if account.enabled else "account_disabled",
            default="{name} enabled." if account.enabled else "{name} disabled.",
            name=name,
        )
        logging.getLogger("zbox.main").info(
            "Account %s %s.", account.account_id, state,
        )
        self._tree_folder_lists.pop(account.account_id, None)
        self._refresh_tree()
        self.idle_manager.sync(self.account_manager.accounts)
        if self._search_tab is not None:
            self._search_tab.refresh_accounts()
        self.GetStatusBar().SetStatusText(spoken)
        self.announce(
            spoken, title=lang.t("dialogs", "title_account", default="Account")
        )

    def _on_remove_account(self, event):
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        if account is None:
            self._no_account_message(lang.t('dialogs', 'na_remove_account', default="Remove Account"))
            return

        confirm = wx.MessageBox(
            lang.t(
                "dialogs", "account_remove_confirm",
                default=f"Remove {account.display_name} ({account.identity_email})?\n\n"
                "Mail on the server is not deleted.\n"
                "The copies downloaded to this computer are deleted.\n"
                "The saved password is deleted.",
                name=account.display_name, address=account.identity_email,
            ),
            lang.t("dialogs", "title_remove_account", default="Remove Account"),
            wx.YES_NO | wx.ICON_WARNING,
        )
        if confirm == wx.YES:
            if not self.account_manager.remove_account(account.account_id):
                # Refused, and already reported. The account is still
                # in the list and its password is still stored, so
                # nothing below should run.
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "account_not_removed", default="Account not removed."
                ))
                return
            # Only after the account itself is gone: the two on-disk
            # directories are per-account and meaningless without it.
            # Cache first, then userdata, and each reported separately
            # so a failure names which root it was.
            removed_files = 0
            try:
                cache_files, _cache_bytes = himalaya_client.remove_account_cache(
                    self.paths, account.account_id
                )
                removed_files += cache_files
            except OSError as exc:
                logging.getLogger("zbox.main").warning(
                    "Could not remove the cache directory for %s: %s",
                    account.account_id, exc,
                )
            try:
                profile_files, _profile_bytes = himalaya_client.remove_account_profile(
                    self.paths, account.account_id
                )
                removed_files += profile_files
            except OSError as exc:
                logging.getLogger("zbox.main").warning(
                    "Could not remove the userdata directory for %s: %s",
                    account.account_id, exc,
                )
            self._tree_folder_lists.pop(account.account_id, None)
            self._refresh_tree()
            self.idle_manager.sync(self.account_manager.accounts)
            if self._search_tab is not None:
                self._search_tab.refresh_accounts()
            if removed_files:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "account_removed_with_files",
                    default=f"Account removed. {removed_files} local file(s) "
                            f"deleted.",
                    count=removed_files,
                ))
            else:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "account_removed", default="Account removed."
                ))

    # --- Folder management (audit finding 42) --------------------------

    def _on_new_folder(self, event):
        target = self._tree_context_target or {}
        account = self._find_account(target.get("account_id"))
        if account is None:
            wx.MessageBox(
                lang.t(
                    "dialogs", "select_account_or_folder",
                    default="Select an account or folder first.",
                ),
                lang.t("dialogs", "title_new_folder", default="New Folder"),
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        dialog = wx.TextEntryDialog(
            self,
            lang.t("dialogs", "mf_folder_name_prompt", default="Folder name:"),
            lang.t("dialogs", "title_new_folder", default="New Folder"),
        )
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        name = dialog.GetValue().strip()
        dialog.Destroy()
        if not name:
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "folder_creating",
            default=f'Creating folder "{name}"...', folder=name,
        ))

        def work():
            himalaya_client.create_mailbox(self.paths, account, name)

        def on_success(_result):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "folder_created",
                default=f'Folder "{name}" created.', folder=name,
            ))
            self._refresh_real_folders_for_account(account)

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "folder_create_failed",
                default="Could not create folder.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "folder_create_failed",
                    default=f"Could not create folder.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_new_folder", default="New Folder"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _on_rename_folder(self, event):
        target = self._tree_context_target or {}
        account = self._find_account(target.get("account_id"))
        old_name = target.get("folder")
        if account is None or not old_name or target.get("is_special_folder"):
            return

        dialog = wx.TextEntryDialog(
            self,
            lang.t("dialogs", "mf_new_name_prompt", default="New name:"),
            lang.t("dialogs", "title_rename_folder", default="Rename Folder"),
            value=target.get("folder_label", old_name),
        )
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        new_name = dialog.GetValue().strip()
        dialog.Destroy()
        if not new_name or new_name == old_name:
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "folder_renaming",
            default=f'Renaming "{old_name}" to "{new_name}"...',
            folder=old_name, new_name=new_name,
        ))

        def work():
            himalaya_client.rename_mailbox(self.paths, account, old_name, new_name)

        def on_success(_result):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "folder_renamed",
                default=f'Folder renamed to "{new_name}".', new_name=new_name,
            ))
            self._refresh_real_folders_for_account(account)

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "folder_rename_failed",
                default="Could not rename folder.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "folder_rename_failed",
                    default=f"Could not rename folder.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_rename_folder", default="Rename Folder"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _on_delete_folder(self, event):
        target = self._tree_context_target or {}
        account = self._find_account(target.get("account_id"))
        name = target.get("folder")
        if account is None or not name or target.get("is_special_folder"):
            return

        confirm = wx.MessageBox(
            lang.t(
                "dialogs", "folder_delete_confirm",
                default=f'Permanently delete the folder "{target.get("folder_label", name)}" '
                f"and everything in it for {account.identity_email}?\n\nThis cannot be undone.",
                folder=target.get("folder_label", name),
                address=account.identity_email,
            ),
            lang.t("dialogs", "title_delete_folder", default="Delete Folder"),
            wx.YES_NO | wx.ICON_WARNING | wx.NO_DEFAULT,
        )
        if confirm != wx.YES:
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "folder_deleting",
            default=f'Deleting folder "{name}"...', folder=name,
        ))

        def work():
            himalaya_client.delete_mailbox(self.paths, account, name)

        def on_success(_result):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "folder_deleted", default="Folder deleted."
            ))
            self._refresh_real_folders_for_account(account)

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "folder_delete_failed",
                default="Could not delete folder.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "folder_delete_failed",
                    default=f"Could not delete folder.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_delete_folder", default="Delete Folder"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _on_subscribe_folder(self, event):
        self._run_subscribe_action(subscribe=True)

    def _on_unsubscribe_folder(self, event):
        self._run_subscribe_action(subscribe=False)

    def _run_subscribe_action(self, subscribe):
        target = self._tree_context_target or {}
        account = self._find_account(target.get("account_id"))
        name = target.get("folder")
        if account is None or not name or target.get("is_special_folder"):
            return

        verb = lang.t('main_ui', 'folder_verb_subscribing', default="Subscribing to") if subscribe else lang.t('main_ui', 'folder_verb_unsubscribing', default="Unsubscribing from")
        self.GetStatusBar().SetStatusText(
            lang.t(
                "main_ui", "folder_subscribing",
                default=f'Subscribing to "{name}"...', folder=name,
            )
            if subscribe
            else lang.t(
                "main_ui", "folder_unsubscribing",
                default=f'Unsubscribing from "{name}"...', folder=name,
            )
        )

        def work():
            if subscribe:
                himalaya_client.subscribe_mailbox(self.paths, account, name)
            else:
                himalaya_client.unsubscribe_mailbox(self.paths, account, name)

        def on_success(_result):
            self.GetStatusBar().SetStatusText(
                lang.t("main_ui", "folder_subscribed", default="Subscribed.")
                if subscribe
                else lang.t(
                    "main_ui", "folder_unsubscribed", default="Unsubscribed."
                )
            )
            self._refresh_real_folders_for_account(account)

        def on_error(exc):
            action = "subscribe to" if subscribe else "unsubscribe from"
            self.GetStatusBar().SetStatusText(
                lang.t(
                    "main_ui", "folder_subscribe_failed",
                    default="Could not subscribe to folder.",
                )
                if subscribe
                else lang.t(
                    "main_ui", "folder_unsubscribe_failed",
                    default="Could not unsubscribe from folder.",
                )
            )
            wx.MessageBox(
                lang.t(
                    "errors", "folder_subscribe_failed",
                    default=f"Could not {action} folder.\n\n{exc}", error=exc,
                ) if subscribe else lang.t(
                    "errors", "folder_unsubscribe_failed",
                    default=f"Could not {action} folder.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_subscribe", default="Subscribe")
                if subscribe else
                lang.t("dialogs", "title_unsubscribe", default="Unsubscribe"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _lane_for_accounts(self, account_ids):
        """The worker for a job touching these accounts: that
        account's own lane when there is exactly one, the shared
        worker when a job spans several."""
        ids = {account_id for account_id in account_ids if account_id}
        if len(ids) == 1:
            return self.lanes.for_account(next(iter(ids)))
        return self.worker

    def _find_account(self, account_id):
        if not account_id:
            return None
        for account in self.account_manager.accounts:
            if account.account_id == account_id:
                return account
        return None

    def _on_refresh_folder(self, event):
        self._on_tree_selection_changed(event)

    def _on_refresh_all_accounts(self, event):
        """
        Shift+F5 / Accounts > Refresh All Accounts: re-fetches every
        account's Inbox in the background (on sync_worker, so this
        never blocks the interactive worker), independent of which
        account or folder is currently on screen. This is the
        counterpart to the 20-second auto-refresh timer, which only
        ever re-checks whichever folder is currently selected -- an
        account sitting in the background (not the active tree
        selection) otherwise never gets polled until the user clicks
        over to it. Whichever account/folder happens to already be on
        screen still gets repainted via the normal changed-only path.
        """
        accounts = list(self.account_manager.enabled_accounts())
        if not accounts:
            return

        current_target = self.mail_panel.account_panel.selected_fetch_target()
        current_account_id = current_target.get("account_id") if current_target else None
        current_folder = current_target.get("folder") if current_target else None

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "refreshing_accounts",
            default=f"Refreshing {len(accounts)} account(s)...",
            count=len(accounts),
        ))
        backend = "maildir" if self.work_offline else "imap"

        # The finish used to be a 1500ms timer that said "Refreshed
        # all accounts" whether or not anything had come back, and it
        # said it only to the status bar -- which no screen reader
        # speaks (see announce.py). One report now goes out when the
        # last account has actually returned, through announce_action
        # like every other finished action. The tally is kept on the
        # main thread via CallAfter, so it needs no lock wherever the
        # sync worker happens to run its callbacks.
        total = len(accounts)
        tally = {"done": 0, "failed": 0}

        def account_finished(failed):
            tally["done"] += 1
            if failed:
                tally["failed"] += 1
            if tally["done"] < total:
                return
            noun = "account" if total == 1 else "accounts"
            if tally["failed"]:
                text = lang.t(
                    "actions_announcements",
                    "refresh_all_done_failures_one" if total == 1
                    else "refresh_all_done_failures",
                    default="Refreshed %d %s, %d could not be refreshed." % (
                        total - tally["failed"], noun, tally["failed"],
                    ),
                    count=total - tally["failed"], failed=tally["failed"],
                )
            elif total == 1:
                text = lang.t(
                    "actions_announcements", "refresh_all_done_one",
                    default="Refreshed %d %s." % (total, noun),
                )
            else:
                text = lang.t(
                    "actions_announcements", "refresh_all_done_many",
                    default="Refreshed %d %s." % (total, noun), count=total,
                )
            try:
                self.GetStatusBar().SetStatusText(text)
            except RuntimeError:
                pass
            self.announce_action(
                text,
                title=lang.t(
                    "dialogs", "title_refresh_all_accounts",
                    default="Refresh All Accounts",
                ),
            )

        def work(account=None):
            return himalaya_client.list_envelopes(self.paths, account, folder="INBOX", backend=backend)

        def make_on_success(account):
            def on_success(envelopes):
                for envelope in envelopes:
                    envelope["_zbox_backend"] = backend
                if backend == "imap":
                    self._refresh_offline_cache_quietly(account, "INBOX")
                if account.account_id == current_account_id and current_folder == "INBOX":
                    wx.CallAfter(self.mail_panel.envelope_panel.populate_if_changed, envelopes)
                wx.CallAfter(account_finished, False)
            return on_success

        def make_on_error(account):
            def on_error(exc):
                logging.getLogger("zbox.main").warning(
                    "Refresh All Accounts: could not refresh %s: %s", account.identity_email, exc
                )
                wx.CallAfter(account_finished, True)
            return on_error

        for account in accounts:
            self.sync_worker.submit(
                lambda account=account: work(account),
                make_on_success(account),
                make_on_error(account),
            )

    def _on_open_saved_message(self, event):
        """File > Open > Saved Message...: opens a .eml file from disk
        (not something ZBox synced) and shows it read-only in its own
        tab. Parsed locally with Python's stdlib email module rather
        than round-tripped through Himalaya, since there's no account
        or live folder behind a file on disk."""
        with wx.FileDialog(
            self,
            lang.t(
                "dialogs", "title_open_saved_message",
                default="Open Saved Message",
            ),
            wildcard=lang.t('dialogs', 'wildcard_eml', default="Email files (*.eml)|*.eml|All files (*.*)|*.*"),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()

        from message_view_panel import read_and_format_eml_file, SavedMessageViewPanel
        try:
            text, subject = read_and_format_eml_file(path)
        except OSError as exc:
            wx.MessageBox(
                lang.t(
                    "errors", "saved_open_failed",
                    default=f"Could not open the file.\n\n{exc}", error=exc,
                ),
                lang.t(
                    "dialogs", "title_open_saved_message",
                    default="Open Saved Message",
                ),
                wx.OK | wx.ICON_ERROR,
            )
            return
        except Exception as exc:  # malformed .eml content
            wx.MessageBox(
                lang.t(
                    "errors", "saved_parse_failed",
                    default=f"Could not parse this file as an email message.\n\n{exc}",
                    error=exc,
                ),
                lang.t(
                    "dialogs", "title_open_saved_message",
                    default="Open Saved Message",
                ),
                wx.OK | wx.ICON_ERROR,
            )
            return

        panel = SavedMessageViewPanel(self.notebook, self, text)
        title = subject if len(subject) <= 30 else subject[:27] + "..."
        self.open_tab(panel, title or lang.t('dialogs', 'tab_saved_message', default="Saved Message"))

    def _on_save_message_as(self, event):
        """File > Save As > File...: saves the message open in the
        current tab to disk as a raw .eml file (full RFC822 source,
        headers included), fetched fresh via Himalaya's --raw flag --
        the same mechanism already used for offline sync."""
        account, folder, envelope, _message = self._current_message_context()
        if envelope is None or account is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "open_message_first_save",
                default="Open a message first, then use Save As.",
            ))
            return

        message_id = envelope.get("id")
        backend = _envelope_backend(envelope)
        subject = _envelope_subject(envelope) or "message"
        safe_name = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in subject).strip() or "message"

        with wx.FileDialog(
            self,
            lang.t(
                "dialogs", "title_save_message_as", default="Save Message As",
            ),
            defaultFile=f"{safe_name}.eml",
            wildcard=lang.t('dialogs', 'wildcard_eml', default="Email files (*.eml)|*.eml|All files (*.*)|*.*"),
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            dest_path = dialog.GetPath()

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "saving_message", default="Saving message..."
        ))

        def work():
            return himalaya_client.read_message_raw(self.paths, account, message_id, folder=folder, backend=backend)

        def on_success(raw_text):
            try:
                with open(dest_path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(raw_text)
            except OSError as exc:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "save_file_failed",
                    default="Could not save the file.",
                ))
                wx.MessageBox(
                    lang.t(
                        "errors", "save_file_failed",
                        default=f"Could not save the file.\n\n{exc}", error=exc,
                    ),
                    lang.t(
                        "dialogs", "title_save_message_as",
                        default="Save Message As",
                    ),
                    wx.OK | wx.ICON_ERROR,
                )
                return
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "saved_to",
                default=f"Saved to {dest_path}", folder=dest_path,
            ))

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "save_fetch_failed",
                default="Could not fetch the message to save.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "save_fetch_failed",
                    default=f"Could not fetch the message to save.\n\n{exc}",
                    error=exc,
                ),
                lang.t("dialogs", "title_save_message_as", default="Save Message As"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_read(account.account_id, backend).submit(work, on_success, on_error)

    def _on_empty_trash(self, event):
        """Tree context menu, on the Trash folder row: permanently
        purges every message in that account's Trash. Reads
        _tree_context_target (the row actually right-clicked), not
        the currently selected/displayed folder -- the same target
        every other context-menu folder action here already uses."""
        target = self._tree_context_target or {}
        account = self._find_account(target.get("account_id"))
        trash_folder = target.get("folder")
        if account is None or not trash_folder:
            return

        confirm = wx.MessageBox(
            lang.t(
                "dialogs", "empty_trash_confirm",
                default=f"Permanently delete all messages in Trash for {account.identity_email}?\n\nThis cannot be undone.",
                address=account.identity_email,
            ),
            lang.t("dialogs", "title_empty_trash", default="Empty Trash"),
            wx.YES_NO | wx.ICON_WARNING | wx.NO_DEFAULT,
        )
        if confirm != wx.YES:
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "emptying_trash",
            default=f"Emptying Trash for {account.identity_email}...",
            account=account.identity_email,
        ))

        def work():
            envelopes = himalaya_client.list_envelopes(
                self.paths, account, folder=trash_folder, page_size=2000, backend="imap"
            )
            himalaya_client.purge_folder(self.paths, account, trash_folder, trash_folder, envelopes)
            return len(envelopes)

        def on_success(purged):
            self.GetStatusBar().SetStatusText(
                lang.t(
                    "main_ui", "trash_already_empty",
                    default="Trash was already empty.",
                )
                if purged == 0
                else lang.t(
                    "main_ui", "trash_emptied",
                    default=f"Emptied Trash: {purged} message(s) purged.",
                    count=purged,
                )
            )
            self._auto_refresh_current_folder()

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "empty_trash_failed",
                default="Could not empty Trash.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "empty_trash_failed",
                    default=f"Could not empty Trash.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_empty_trash", default="Empty Trash"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _on_empty_junk(self, event):
        """Tree context menu, on the Junk folder row: permanently
        purges every message in that account's Junk folder -- moved
        to Trash and purged there in one pass (purge_folder), since
        Junk is never already Trash itself."""
        target = self._tree_context_target or {}
        account = self._find_account(target.get("account_id"))
        junk_folder = target.get("folder")
        if account is None or not junk_folder:
            return

        trash_folder = _folder_display_to_himalaya("Trash", account)
        confirm = wx.MessageBox(
            lang.t(
                "dialogs", "empty_junk_confirm",
                default=f"Permanently delete all messages in Junk for {account.identity_email}?\n\nThis cannot be undone.",
                address=account.identity_email,
            ),
            lang.t("dialogs", "title_empty_junk", default="Empty Junk"),
            wx.YES_NO | wx.ICON_WARNING | wx.NO_DEFAULT,
        )
        if confirm != wx.YES:
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "emptying_junk",
            default=f"Emptying Junk for {account.identity_email}...",
            account=account.identity_email,
        ))

        def work():
            envelopes = himalaya_client.list_envelopes(
                self.paths, account, folder=junk_folder, page_size=2000, backend="imap"
            )
            unresolved = himalaya_client.purge_folder(
                self.paths, account, junk_folder, trash_folder, envelopes
            )
            return len(envelopes), unresolved

        def on_success(result):
            total, unresolved = result
            purged = total - len(unresolved)
            not_moved = sum(1 for _message_id, reason in unresolved if reason == "move_failed")
            left_in_trash = len(unresolved) - not_moved
            if total == 0:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "junk_already_empty",
                    default="Junk was already empty.",
                ))
            elif unresolved:
                parts = [lang.t(
                    "main_ui", "junk_part_purged",
                    default=f"{purged} purged", count=purged,
                )]
                if left_in_trash:
                    parts.append(lang.t(
                        "main_ui", "junk_part_in_trash",
                        default=f"{left_in_trash} moved to Trash but not "
                                f"confirmed removed, check Trash",
                        count=left_in_trash,
                    ))
                if not_moved:
                    parts.append(lang.t(
                        "main_ui", "junk_part_not_moved",
                        default=f"{not_moved} not moved to Trash, still in Junk",
                        count=not_moved,
                    ))
                self.GetStatusBar().SetStatusText(
                    lang.t(
                        "main_ui", "junk_emptied_prefix",
                        default="Emptied Junk:",
                    ) + " " + "; ".join(parts) + "."
                )
            else:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "junk_emptied",
                    default=f"Emptied Junk: {purged} message(s) purged.",
                    count=purged,
                ))
            self._auto_refresh_current_folder()

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "empty_junk_failed",
                default="Could not empty Junk.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "empty_junk_failed",
                    default=f"Could not empty Junk.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_empty_junk", default="Empty Junk"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _on_print_message(self, event):
        """File > Print...: prints the message open in the current
        tab as plain text (the same accessible text ZBox's Text view
        shows), regardless of whether that tab is in Text or HTML
        view -- printing rendered HTML layout is out of scope."""
        page = self._current_message_tab()
        if page is None or not hasattr(page, "printable_text"):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "open_message_first_print",
                default="Open a message first, then use Print.",
            ))
            return
        from message_view_panel import print_text
        print_text(self, page.printable_text(), page.printable_title())

    def _on_print_preview_message(self, event):
        """File > Print Preview...: same content as File > Print
        (audit finding 51), opening wx's on-screen preview frame
        first instead of going straight to the OS print dialog."""
        page = self._current_message_tab()
        if page is None or not hasattr(page, "printable_text"):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "open_message_first_preview",
                default="Open a message first, then use Print Preview.",
            ))
            return
        from message_view_panel import print_preview
        print_preview(self, page.printable_text(), page.printable_title())

    def _on_toggle_work_offline(self, event):
        self.work_offline = event.IsChecked()
        self.GetStatusBar().SetStatusText(
            lang.t(
                "main_ui", "working_offline",
                default="Working offline: showing the last synced copy of "
                        "Inbox/Sent/Drafts.",
            )
            if self.work_offline
            else lang.t("main_ui", "back_online", default="Back online.")
        )
        # Re-run whatever's currently open against the newly selected
        # backend so the switch takes effect immediately rather than
        # waiting for the next selection change.
        self._on_refresh_folder(event)

    def _on_sync_offline(self, event):
        """
        Downloads the most recent messages in Inbox, Sent and Drafts
        for the currently selected account into the local Maildir
        copy, so Work Offline has something to show without having to
        visit anything first.

        Every other folder now gets a local copy the first time it is
        opened while online, which is what this docstring used to
        claim and nothing implemented -- the reason Work Offline
        showed an empty list for Junk, Trash and Archive.
        """
        target = self.mail_panel.account_panel.selected_fetch_target()
        account = self._find_account(target.get("account_id")) if target else None
        if account is None:
            wx.MessageBox(
                lang.t(
                    "dialogs", "sync_select_account",
                    default="Select an account (not a Unified folder) to synchronize for offline use.",
                ),
                lang.t(
                    "dialogs", "title_synchronize_offline",
                    default="Synchronize for Offline",
                ),
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        folders = _OFFLINE_SYNCED_FOLDERS
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "offline_sync_running",
            default=f"Synchronizing {account.identity_email} for offline use...",
            account=account.identity_email,
        ))

        def work():
            total_copied = 0
            for folder in folders:
                total_copied += himalaya_client.sync_folder_offline(
                    self.paths, account, folder, limit=100,
                    max_age_days=self.settings_manager.settings.cache_max_message_age_days,
                )
            return total_copied

        def on_success(total_copied):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "offline_sync_done",
                default=f"Synchronized {total_copied} message(s) across Inbox, "
                        f"Sent and Drafts for offline use.",
                count=total_copied,
            ))

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "offline_sync_failed",
                default=f"Offline sync failed: {exc}", error=exc,
            ))

        self.sync_worker.submit(work, on_success, on_error)

    def _on_announce_sender(self, event):
        """Ctrl+U: say who sent the message under the cursor.

        Works from the message list and from inside an open message,
        because _current_message_context resolves both -- from inside
        a message tab there is otherwise no way to hear the sender
        without closing the tab and going back to the list, which is
        audit finding 16 in miniature.
        """
        self._announce_sender(copy_address=False)

    def _on_announce_copy_sender(self, event):
        """Ctrl+Shift+U: the same, and put the bare address on the
        clipboard -- the address only, no display name, so it can be
        pasted straight into a To field or a filter rule."""
        self._announce_sender(copy_address=True)

    def _on_add_sender_to_address_book(self, event):
        """
        Audit finding 50: adds the sender of whichever message Reply/
        Announce Sender would act on (list selection or an open tab,
        via _current_message_context) to the address book, without
        opening the message or waiting for a reply -- a deliberate,
        user-visible counterpart to the automatic capture that
        already happens on read/reply/send (see contacts.
        add_from_envelope_from's other call sites). Uses the same
        soft-merge add() automatic capture does, not upsert(), so
        this never blanks out a name or phone/notes already on file
        for that address; the status line says which happened
        (added vs. already known) since add() alone gives no other
        feedback.
        """
        _account, _folder, envelope, _message = self._current_message_context()
        if envelope is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "select_or_open_message",
                default="Select or open a message first.",
            ))
            return

        sender_list = envelope.get("from")
        sender = sender_list[0] if isinstance(sender_list, list) and sender_list else None
        email = (sender.get("email") or "").strip() if isinstance(sender, dict) else ""
        if not email:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "no_sender_address",
                default="This message has no sender address to add.",
            ))
            return
        name = (sender.get("name") or "").strip() if isinstance(sender, dict) else ""

        label = f"{name} <{email}>" if name else email
        if self.contacts.add(name, email):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "contact_added",
                default=f"Added {label} to your address book.", contact=label,
            ))
        else:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "contact_already_there",
                default=f"{label} is already in your address book.",
                contact=label,
            ))

    def _announce_sender(self, copy_address):
        _account, _folder, envelope, _message = self._current_message_context()
        if envelope is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "select_or_open_message_ctrl_u",
                default="Select or open a message first, then press Ctrl+U.",
            ))
            return

        spoken, address = _envelope_sender_spoken(envelope)

        if not copy_address:
            # 150 ms later, not at once. Ctrl+U spoke at the instant the
            # key went down, while the screen reader was still handling
            # the key press, and the reader's own Ctrl ("stop talking")
            # swallowed it -- live on 25 September 2026, never heard.
            # Ctrl+Shift+U always was: copying the address first delays
            # its speech by about that much.
            text = spoken
            title = lang.t("dialogs", "title_sender", default="Sender")
            wx.CallLater(150, self.announce, text, title=title)
            return

        if not address:
            self.announce(
                lang.t("actions_announcements", "sender_no_address",
                       default="{sender}. No address to copy.", sender=spoken),
                title=lang.t("dialogs", "title_sender", default="Sender"),
            )
            return

        if self._copy_to_clipboard(address):
            self.announce(
                lang.t("actions_announcements", "sender_address_copied",
                       default="{sender}. Address copied.", sender=spoken),
                title=lang.t("dialogs", "title_sender", default="Sender"),
            )
        else:
            self.announce(
                lang.t(
                    "actions_announcements", "clipboard_not_opened",
                    default="{sender}. The clipboard could not be opened.",
                    sender=spoken,
                ),
                title=lang.t("dialogs", "title_sender", default="Sender"),
            )

    def announce(self, text, title="ZBox"):
        """Says something to the screen reader and puts it in the
        status bar.

        Both, deliberately. The status bar is the quiet channel for
        anyone who has status-bar reporting switched on and does not
        want to be interrupted; the dialog is the one that works with
        no configuration at all, which is what an announcement whose
        whole purpose is to be heard actually needs.
        """
        try:
            self.GetStatusBar().SetStatusText(text)
        except Exception:
            pass
        hold_ms = getattr(
            self.settings_manager.settings, "announcement_hold_ms", 1800
        )
        # Without focus first. A dialog is read by every reader in
        # every configuration, which is why it was chosen, but it
        # takes focus, moves the system caret and is visible to a
        # sighted user -- wrong for something that is only telling
        # you what just happened. The dialog stays as the fallback
        # for when the event cannot be raised at all.
        if speak(self, text):
            return
        announce(self, text, title=title, hold_ms=hold_ms)

    def announce_action(self, text, title="ZBox"):
        """Says what an action just did, when Settings' Announce
        message actions is on.

        Separate from announce() on purpose. announce() is for things
        whose whole reason to exist is to be heard -- who sent this,
        new mail, a message that would not open -- and must never be
        switchable off. This one is the per-action running commentary
        (archived, marked as junk, deleted), which is genuinely a
        matter of taste. Off still leaves the status bar written, as
        every action already did."""
        if not getattr(self.settings_manager.settings, "announce_actions", True):
            return
        self.announce(text, title=title)

    def _check_raise_request(self):
        """Brings the window back when a second launch asked for it.

        See app/single_instance.py: clicking the desktop shortcut
        while ZBox is already running -- minimized, in the tray, or
        just behind something else -- drops a request file instead of
        starting a second copy. Show before Iconize, because a window
        hidden into the tray is not iconized, and both are true after
        a minimize-to-tray.
        """
        if not single_instance.consume_raise_request(self.paths.config):
            return
        if not self.IsShown():
            self.Show()
        if self.IsIconized():
            self.Iconize(False)
        self.Raise()
        # Raise alone only flashes the taskbar button: Windows will
        # not move the foreground to a process nobody just clicked.
        # The second launch granted that right before asking (see
        # single_instance.grant_foreground); this takes it. Then the
        # message list gets focus, as restore_from_tray gives it, so
        # the screen reader lands somewhere it can speak.
        single_instance.force_foreground(self.GetHandle())
        if self.notebook.GetSelection() != 0:
            self.notebook.SetSelection(0)
        self.mail_panel.envelope_panel.list_ctrl.SetFocus()

    def _play_sound(self, name):
        """Plays one of ZBox's notification sounds (Settings > Sound
        & Notifications) -- see sound_manager.py for the theme/
        fallback resolution rules. Always safe to call: a disabled
        master toggle or a missing file is a silent no-op, never a
        dialog or an exception, the same as announce() is never worth
        crashing anything over."""
        sound_manager.play(self.paths.sounds_dir, self.settings_manager.settings, name)

    def _announce_new_mail(self, account, new_envelopes):
        """
        Audit finding 47's other missing half: speaks new mail rather
        than only playing a sound for it, for whoever opted in via
        this account's own "Announce new mail aloud" checkbox
        (Account Settings) -- off by default, since interrupting with
        speech is a bigger ask than a sound and shouldn't become the
        default the moment sounds are already on. Batches the same
        way the sound does: one announcement per genuinely-new batch,
        not one per message.
        """
        count = len(new_envelopes)
        name = account.display_name or account.identity_email
        self.announce(
            lang.t(
                "actions_announcements",
                "new_mail_one" if count == 1 else "new_mail_many",
                default=("1 new message for {name}." if count == 1
                         else "{count} new messages for {name}."),
                count=count, name=name,
            ),
            title=lang.t("dialogs", "title_new_mail", default="New Mail"),
        )

    def _show_new_mail_toast(self, account, new_envelopes):
        """Windows desktop notification for a genuinely-new arrival
        batch -- see desktop_notifications.py for the gating rules
        (Settings' master switch, this account's own mute state) and
        why failures here are always silent, same as _play_sound and
        announce()."""
        desktop_notifications.show_new_mail(
            self.settings_manager.settings, account, new_envelopes
        )

    def _play_progress_tick(self):
        """Throttled separately from other sounds: a bulk action's
        items can complete far faster than progress-tick.wav takes to
        play, and firing one per item would overlap into noise rather
        than a rhythm. At most one tick per 80ms, however fast items
        underneath it actually finish."""
        now = time.monotonic()
        if now - self._last_progress_tick_at < 0.08:
            return
        self._last_progress_tick_at = now
        self._play_sound("progress-tick")

    @staticmethod
    def _copy_to_clipboard(text):
        """Flush() matters: without it the clipboard contents vanish
        when ZBox exits, which is exactly when someone goes to paste
        the address somewhere else."""
        if not wx.TheClipboard.Open():
            return False
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Flush()
            return True
        except Exception:
            logging.getLogger("zbox.main").exception("Clipboard copy failed.")
            return False
        finally:
            wx.TheClipboard.Close()

    def _on_privacy(self, event):
        """Tools > Privacy and Content Blocking. Kept separate from
        Settings so neither dialog outgrows the screen; see
        privacy_dialog."""
        from privacy_dialog import PrivacyDialog

        dialog = PrivacyDialog(
            self,
            self.settings_manager.settings,
            blocklist=self.blocklist,
            on_update_now=lambda: self._schedule_blocklist_update(force=True),
            junk_rules=self.junk_rules,
            phishing_list=self._phishing_list,
            on_update_phishing=lambda: self._schedule_phishing_update(force=True),
        )
        if dialog.ShowModal() == wx.ID_OK:
            self.settings_manager.settings = dialog.apply_to_settings()
            self.settings_manager.save()
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "privacy_saved",
                default="Privacy settings saved. Open a message again to "
                        "apply them.",
            ))
            # Turning the junk rules on should not wait for the hourly
            # tick to fetch a phishing list that was never downloaded.
            self._schedule_phishing_update()
        dialog.Destroy()

    def _on_settings(self, event):
        old_language = getattr(self.settings_manager.settings, "language", "en")
        old_theme = getattr(self.settings_manager.settings, "ui_theme", "system")
        dialog = SettingsDialog(self, self.settings_manager.settings)
        language_changed = False
        theme_changed = False
        if dialog.ShowModal() == wx.ID_OK:
            self.settings_manager.settings = dialog.apply_to_settings()
            self.settings_manager.save()
            # Message font size/reading font (audit finding 53) is
            # the one Settings change worth applying immediately --
            # everything else in this dialog is documented as
            # "takes effect the next time...", but a font change the
            # reader just made and can't see take effect would look
            # broken, not deferred.
            self._apply_message_zoom_to_open_tabs()
            # Run at Windows startup (like message zoom above) is worth
            # applying immediately rather than "next launch" -- toggling
            # a checkbox that silently does nothing until you restart
            # the app looks broken, not deferred.
            ok = startup_registration.set_registered(
                self.settings_manager.settings.run_at_windows_startup,
                self.paths.base,
            )
            status_text = lang.t('main_ui', 'settings_saved_status', default="Settings saved.")
            if not ok:
                status_text = lang.t('main_ui', 'settings_saved_no_startup', default="Settings saved. Could not update the Windows startup entry.")
            self.GetStatusBar().SetStatusText(status_text)
            new_language = getattr(self.settings_manager.settings, "language", "en")
            language_changed = new_language != old_language
            new_theme = getattr(self.settings_manager.settings, "ui_theme", "system")
            theme_changed = new_theme != old_theme
        dialog.Destroy()
        if language_changed:
            self._offer_language_restart()
        elif theme_changed:
            # Same restart path: the theme, like the language, is
            # only read once at startup (theme.apply_theme).
            self._offer_language_restart(
                lang.t(
                    "dialogs", "set_theme_restart_confirm",
                    default="Restart ZBox to apply the theme change?",
                )
            )

    def _offer_language_restart(self, question=None):
        """Interface language is only read once, at startup (see
        ZBoxApp.__init__ in this file), so changing it in Settings
        does nothing visible until ZBox restarts. Offers to do that
        restart immediately: relaunch a new ZBox process the same way
        a desktop shortcut or Himalaya's --get-secret path already
        resolve it (see desktop_shortcut.shortcut_target and
        account_manager._get_secret_argv), then close this instance
        through the normal shutdown path (_on_close) so workers, the
        Himalaya subprocess, window state and the tray icon are still
        torn down properly. The new process carries --relaunch, and
        main.py skips its single-instance check for that flag, so it
        does not mistake this closing one for a second,
        already-running copy of ZBox. The close comes first and the
        launch only follows a close that went through, so a cancelled
        unsent-draft prompt never leaves two copies running.
        """
        answer = wx.MessageBox(
            question or lang.t(
                "dialogs", "set_language_restart_confirm",
                default="Restart ZBox to apply language change?",
            ),
            lang.t("dialogs", "title_settings", default="Settings"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        )
        if answer != wx.YES:
            return
        if getattr(sys, "frozen", False):
            argv = [sys.executable, "--relaunch"]
        else:
            argv = [sys.executable, self.paths.main_script, "--relaunch"]
        logger = logging.getLogger("zbox.main")
        # Close first. _on_close vetoes when an unsent draft's discard
        # prompt is cancelled, and a copy launched before that would
        # keep running beside this one, because --relaunch skips
        # main.py's single-instance check. Close() is False on a veto.
        self._quitting = True
        if not self.Close():
            self._quitting = False
            logger.info("Language restart: close was cancelled; not relaunching.")
            return
        logger.info(
            "Language restart: launching %s (cwd=%s)", argv, self.paths.base,
        )
        try:
            try:
                proc = subprocess.Popen(
                    argv, cwd=self.paths.base,
                    creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,
                )
            except OSError:
                # A job object that forbids breakaway (some launchers
                # and terminals run ZBox inside one) refuses the flag
                # with access denied. Retried once without it.
                logger.warning(
                    "Language restart: breakaway refused; retrying "
                    "without it.", exc_info=True,
                )
                proc = subprocess.Popen(argv, cwd=self.paths.base)
        except OSError as exc:
            logger.exception("Language restart: relaunch failed to start.")
            wx.MessageBox(
                lang.t(
                    "errors", "language_restart_failed",
                    default="ZBox could not restart itself:\n\n%s" % exc,
                    error=exc,
                ),
                lang.t("dialogs", "title_settings", default="Settings"),
                wx.OK | wx.ICON_ERROR,
            )
            return
        logger.info("Language restart: new process pid %s.", proc.pid)

    # --- Message font size/zoom (audit finding 53) ---------------------

    def _apply_message_zoom_to_open_tabs(self):
        for index in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(index)
            if hasattr(page, "apply_reading_settings"):
                page.apply_reading_settings()

    def _adjust_message_zoom(self, delta):
        from message_view_panel import (
            DEFAULT_READING_FONT_POINT_SIZE,
            clamp_reading_font_size,
        )

        settings = self.settings_manager.settings
        current = settings.reading_font_size
        if current is None:
            current = DEFAULT_READING_FONT_POINT_SIZE
        settings.reading_font_size = clamp_reading_font_size(current + delta)
        self.settings_manager.save()
        self._apply_message_zoom_to_open_tabs()
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "message_font_size",
            default=f"Message font size: {settings.reading_font_size} pt.",
            size=settings.reading_font_size,
        ))

    def _on_increase_message_zoom(self, event):
        self._adjust_message_zoom(1)

    def _on_decrease_message_zoom(self, event):
        self._adjust_message_zoom(-1)

    def _on_reset_message_zoom(self, event):
        self.settings_manager.settings.reading_font_size = None
        self.settings_manager.save()
        self._apply_message_zoom_to_open_tabs()
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "zoom_reset",
            default="Message font size reset to default.",
        ))

    # --- Import/export of accounts and settings (audit finding 54) -----

    def _on_export_accounts(self, event):
        import datetime

        from import_export import write_export_file

        accounts = self.account_manager.accounts
        with wx.FileDialog(
            self,
            lang.t(
                "dialogs", "mf_export_dialog_title",
                default=f"Export All {len(accounts)} Account(s) and Settings",
                count=len(accounts),
            ),
            defaultFile=f"zbox_export_{datetime.date.today().isoformat()}.json",
            wildcard=lang.t('dialogs', 'wildcard_export', default="ZBox export (*.json)|*.json"),
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            write_export_file(
                path, accounts, self.settings_manager.settings
            )
        except OSError as exc:
            wx.MessageBox(
                lang.t(
                    "errors", "export_write_failed",
                    default=f"Could not write the export file.\n\n{exc}", error=exc,
                ),
                lang.t(
                    "dialogs", "title_export_accounts",
                    default="Export Accounts and Settings",
                ),
                wx.OK | wx.ICON_ERROR,
            )
            return
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "export_done",
            default=f"Exported {len(accounts)} account(s) and settings to {path}.",
            count=len(accounts), path=path,
        ))

    def _on_import_accounts(self, event):
        from import_export import (
            accounts_from_export_data,
            apply_portable_settings,
            read_export_file,
        )

        with wx.FileDialog(
            self,
            lang.t(
                "dialogs", "title_import_accounts",
                default="Import Accounts and Settings",
            ),
            wildcard=lang.t('dialogs', 'wildcard_export', default="ZBox export (*.json)|*.json"),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()

        try:
            data = read_export_file(path)
        except (OSError, ValueError) as exc:
            wx.MessageBox(
                lang.t(
                    "errors", "import_read_failed",
                    default=f"Could not read this file as a ZBox export.\n\n{exc}",
                    error=exc,
                ),
                lang.t(
                    "dialogs", "title_import_accounts",
                    default="Import Accounts and Settings",
                ),
                wx.OK | wx.ICON_ERROR,
            )
            return

        try:
            new_accounts = accounts_from_export_data(data)
        except Exception as exc:
            wx.MessageBox(
                lang.t(
                    "errors", "import_not_export",
                    default=f"This file does not look like a ZBox export.\n\n{exc}",
                    error=exc,
                ),
                lang.t(
                    "dialogs", "title_import_accounts",
                    default="Import Accounts and Settings",
                ),
                wx.OK | wx.ICON_ERROR,
            )
            return

        saved = [
            account for account in new_accounts
            if self.account_manager.add_account(account)
        ]
        if new_accounts and not saved:
            # Every add was refused: the account list is locked for
            # this session, and add_account has already said so.
            # Stopping here keeps "nothing has been changed" true, by
            # leaving the settings half of the import unapplied too,
            # and avoids falling through to the message below, which
            # would blame the file for holding no accounts.
            return
        new_accounts = saved
        apply_portable_settings(self.settings_manager.settings, data)
        self.settings_manager.save()

        if new_accounts:
            self._refresh_tree()
            self.idle_manager.sync(self.account_manager.accounts)
            for account in new_accounts:
                if getattr(account, "enabled", True):
                    self._refresh_real_folders_for_account(account)
            if self._search_tab is not None:
                self._search_tab.refresh_accounts()
            names = "\n".join(
                f"  {a.display_name or a.login_email}" for a in new_accounts
            )
            wx.MessageBox(
                lang.t(
                    "dialogs", "accounts_imported",
                    default="Imported %d account(s):\n\n%s\n\n"
                    "Passwords are never exported, and the encrypted "
                    "password store is specific to this Windows user "
                    "account, so each imported account needs its password "
                    "entered once via Accounts > Account Settings before "
                    "it can connect."
                    % (len(new_accounts), names),
                    count=len(new_accounts), names=names,
                ),
                lang.t(
                    "dialogs", "title_import_accounts",
                    default="Import Accounts and Settings",
                ),
                wx.OK | wx.ICON_INFORMATION,
            )
        else:
            wx.MessageBox(
                lang.t(
                    "dialogs", "accounts_import_none",
                    default="No accounts were found in this file. Settings were "
                    "still imported, if the file had any.",
                ),
                lang.t(
                    "dialogs", "title_import_accounts",
                    default="Import Accounts and Settings",
                ),
                wx.OK | wx.ICON_INFORMATION,
            )
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "import_done",
            default=f"Imported {len(new_accounts)} account(s) from {path}.",
            count=len(new_accounts), path=path,
        ))

    def _on_address_book(self, event):
        from address_book_dialog import AddressBookDialog

        dialog = AddressBookDialog(self, self.contacts)
        dialog.ShowModal()
        dialog.Destroy()

    def _on_search(self, event):
        """
        Ctrl+Shift+F / Message > Search Messages: opens the Search tab
        (finding 39), creating it the first time and simply
        re-selecting and focusing it on later invocations, so
        repeated use doesn't pile up duplicate tabs.

        _search_tab can go stale two different ways, and both have to
        be handled or this hotkey/menu item goes dead forever after
        one use: the ordinary way is the tab being closed through the
        UI (Ctrl+W etc.), which removes it from the notebook cleanly
        -- GetPageIndex then returns wx.NOT_FOUND, no exception, and
        the code below already handled that (reset to None, fall
        through to recreate). What a live zbox_debug.log caught
        instead, confirmed with temporary logging at each branch here:
        three separate presses in a row logged this method's entry
        with the SAME _search_tab object each time and then produced
        NO further log output at all -- not even the very next debug
        line -- while the rest of the app kept running normally
        (background refresh polls continued unaffected). That is a
        RuntimeError ("wrapped C/C++ object ... has been deleted")
        raised by GetPageIndex/SetSelection/focus_search_field against
        an ALREADY-DESTROYED SearchTabPanel whose Python reference was
        never cleared -- unlike the clean-close case, nothing ever
        reaches the `self._search_tab = None` line, wx's own exception
        handling for a plain EVT_MENU handler prints the traceback to
        stderr only (this app installs no sys.excepthook), so it never
        appears in zbox_debug.log, and the SAME dead reference then
        fails the exact same way on every future attempt -- forever,
        which matches the user's report exactly. What actually
        destroys the panel without going through the normal close path
        is still unconfirmed; this makes the method self-heal from
        that state regardless, the same defensive shape already used
        for this exact class of bug elsewhere (compose_panel,
        bulk_progress_dialog, message_view_panel).
        """
        if self._search_tab is not None:
            try:
                index = self.notebook.GetPageIndex(self._search_tab)
                if index != wx.NOT_FOUND:
                    self.notebook.SetSelection(index)
                    self._search_tab.focus_search_field()
                    return
            except RuntimeError as exc:
                logging.getLogger("zbox.main").warning(
                    "_on_search: existing Search tab was already "
                    "destroyed (%s); recreating it.", exc,
                )
            self._search_tab = None  # closed, or just destroyed above

        from search_panel import SearchTabPanel

        self._search_tab = SearchTabPanel(self.notebook, self)
        self.open_tab(self._search_tab, "Search")

    def _open_search_result(self, envelope):
        """
        Opens a search result (Enter or double-click in the Search
        tab) in a normal message tab. Search results can come from
        any account/folder combination the search scope covered --
        there's no tree selection behind them the way
        _resolve_envelope_context relies on -- so account and folder
        are read straight off the envelope's own _zbox_account_id and
        _zbox_folder (stamped on every result by SearchTabPanel)
        instead. Once opened, the message tab is a normal message tab
        -- every single-message action keys off the tab's own stored
        (account, folder, envelope), not off this resolution.
        """
        account = self._find_account(envelope.get("_zbox_account_id"))
        folder = envelope.get("_zbox_folder")
        message_id = envelope.get("id")
        if account is None or folder is None or message_id is None:
            wx.MessageBox(
                lang.t(
                    "errors", "search_open_failed",
                    default="Could not open this message.",
                ),
                lang.t("dialogs", "title_search", default="Search"),
                wx.OK | wx.ICON_ERROR,
            )
            return
        backend = _envelope_backend(envelope)

        opening_key = (account.account_id, folder, message_id)
        if self._opening_message_key == opening_key:
            logging.getLogger("zbox.main").debug(
                "Open search result: %s is already being opened; "
                "ignoring the repeat.", opening_key,
            )
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "opening_message_already",
                default="Opening message... (already loading)",
            ))
            return
        logging.getLogger("zbox.main").debug(
            "Open search result: %s backend=%s", opening_key, backend,
        )

        # A new open supersedes any older one, exactly as in
        # _on_envelope_activated_for.
        self._cancel_open_watchdog()
        self._open_request_id += 1
        request_id = self._open_request_id

        # Instant path, same idea as _on_envelope_activated: a result
        # already warmed by the background prefetch opens with no
        # subprocess call at all.
        cached_message = himalaya_client.read_message_cached_only(
            self.paths, account, message_id, folder=folder, backend=backend,
        )
        if cached_message is not None:
            self._open_message_tab(account, folder, envelope, cached_message)
            return

        self._opening_message_key = opening_key
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "opening_message", default="Opening message..."
        ))

        def work():
            return himalaya_client.read_message(
                self.paths, account, message_id, folder=folder,
                backend=backend,
            )

        def clear_key():
            # Only if this request is still the one being tracked.
            # A slow read that fails after the user has already
            # started opening a different message used to clear the
            # newer message's key too, which quietly disabled the
            # duplicate-activation guard for it.
            if self._opening_message_key == opening_key:
                self._opening_message_key = None
            if request_id == self._open_request_id:
                self._cancel_open_watchdog()
            if self._open_timed_out_key == opening_key:
                self._open_timed_out_key = None

        def on_success(message):
            if request_id != self._open_request_id:
                logging.getLogger("zbox.main").debug(
                    "Open search result: %s arrived too late; dropped.",
                    opening_key,
                )
                return
            self._open_message_tab(account, folder, envelope, message)
            clear_key()

        def on_error(exc):
            if request_id != self._open_request_id:
                logging.getLogger("zbox.main").debug(
                    "Open search result: %s failed after being "
                    "superseded; not reported.", opening_key,
                )
                return
            clear_key()
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "could_not_open_message",
                default="Could not open message.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "open_message_failed",
                    default=f"Could not open message.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_open_message", default="Open Message"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_read(account.account_id, backend).submit(
            work, on_success, on_error,
            is_cancelled=lambda: request_id != self._open_request_id,
        )
        self._arm_open_watchdog(opening_key, request_id)

    def _on_open_conversation(self, event):
        """
        Message > Open Message in Conversation (Ctrl+Shift+O) --
        deliberately the same name and key as Thunderbird's own
        built-in command, and the same behaviour: the whole thread in
        ONE tab, as a list of its messages with the selected one's
        body underneath.

        This is what replaced opening a tab per message. See
        conversation_panel.py for why that had to go.
        """
        envelopes = self.mail_panel.envelope_panel.selected_thread_envelopes()
        if not envelopes:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "select_message_first",
                default="Select a message first.",
            ))
            return
        live = [e for e in envelopes if self._is_live_envelope(e)]
        if not live:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "folder_still_loading",
                default="Still loading this folder -- try again in a moment.",
            ))
            return
        context = self._resolve_envelope_context(live[0])
        if context is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "conversation_failed",
                default="Could not open this conversation.",
            ))
            return
        account, folder, _message_id = context

        from conversation_panel import ConversationTabPanel

        panel = ConversationTabPanel(self.notebook, self, account, folder, live)
        subject = live[0].get("subject") or "(no subject)"
        title = subject if len(subject) <= 26 else subject[:23] + "..."
        self.open_tab(panel, title)
        panel.focus_default()

    def _open_conversation_message(self, account, folder, envelope, thread_envelopes=None):
        """
        Enter on a row inside a conversation tab: open that ONE
        message in a full message tab, HTML view and all. One at a
        time is the entire point.
        """
        self._on_envelope_activated_for(
            account, folder, envelope, thread_envelopes=thread_envelopes,
        )

    def _on_show_related_messages(self, event):
        """
        Ctrl+Shift+T / Message > Show Related Messages (audit finding
        44's minimum bar). Needs the same (account, folder, envelope,
        message) as Reply/Forward, and gets it the same way -- an
        open message tab's own stored context if one is focused,
        otherwise the mail list's current selection, fetching the
        body (cache first, then a live read) if it isn't loaded yet.
        """
        account, folder, envelope, message = self._current_message_context()
        if envelope is None or account is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "select_message_for_related",
                default="Select a message to see related messages for first.",
            ))
            return

        if message is not None:
            self._show_related_messages(account, folder, envelope, message)
            return

        backend = _envelope_backend(envelope)
        message_id = envelope.get("id")
        cached_message = himalaya_client.read_message_cached_only(
            self.paths, account, message_id, folder=folder, backend=backend,
        )
        if cached_message is not None:
            self._show_related_messages(account, folder, envelope, cached_message)
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "looking_for_related",
            default="Looking for related messages...",
        ))

        def work():
            return himalaya_client.read_message(
                self.paths, account, message_id, folder=folder, backend=backend,
            )

        def on_success(fetched_message):
            self.GetStatusBar().SetStatusText(
                lang.t("main_ui", "ready", default="Ready.")
            )
            self._show_related_messages(account, folder, envelope, fetched_message)

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "could_not_load_message",
                default="Could not load this message.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "related_load_failed",
                    default=f"Could not load this message.\n\n{exc}", error=exc,
                ),
                lang.t(
                    "dialogs", "title_show_related",
                    default="Show Related Messages",
                ),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_read(account.account_id, backend).submit(work, on_success, on_error)

    def _show_related_messages(self, account, folder, envelope, message):
        """
        Opens the Related Messages tab once the open message's own
        body (and therefore its Message-ID/References/In-Reply-To
        headers) is available. See message_body.thread_ids for what
        "related" means and related_messages_panel for the search
        itself.
        """
        ids = _message_thread_ids(message)
        if not ids:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "no_threading_headers",
                default="This message has no Message-ID or threading headers "
                        "to search on.",
            ))
            return

        from related_messages_panel import RelatedMessagesTabPanel

        panel = RelatedMessagesTabPanel(self.notebook, self, account, folder, envelope, ids)
        subject = envelope.get("subject") or "(no subject)"
        short_subject = subject if len(subject) <= 24 else subject[:21] + "..."
        title = lang.t(
            "dialogs", "mf_related_tab",
            default="Related: " + short_subject, subject=short_subject,
        )
        self.open_tab(panel, title)

    def _current_message_context(self):
        """
        Returns (account, folder, envelope, message) for whichever
        message Reply/Reply All should act on: the message currently
        open in its own tab if one is focused (its body is already
        loaded, no fetch needed), otherwise the message selected in
        the mail list (message is None in that case -- the caller
        fetches the body separately, since the list view only ever
        holds envelopes). Returns (None, None, None, None) when
        nothing is resolvable at all.
        """
        tab = self._current_message_tab()
        if tab is not None and hasattr(tab, "envelope"):
            # Excludes tabs like SavedMessageViewPanel that qualify as
            # a "message tab" (they have close_tab) but have no live
            # account/folder/envelope behind them -- a file opened
            # from disk isn't something Reply/Forward/Save As can
            # act on the same way.
            return tab.account, tab.folder, tab.envelope, tab.message

        envelope = self.mail_panel.envelope_panel.selected_envelope()
        if envelope is None:
            return None, None, None, None
        context = self._resolve_envelope_context(envelope)
        if context is None:
            return None, None, envelope, None
        account, folder, _message_id = context
        return account, folder, envelope, None

    def _on_reply(self, event):
        """
        Reply to the selected message (Q1 answer: block multi-select).
        The multi-select block only applies when Reply's target would
        come from the mail list -- if a message is open in its own
        tab and that tab is focused, _current_message_context() always
        resolves to that single message regardless of how many rows
        happen to still be selected in the background list, so the
        block is skipped in that case.
        """
        if self._current_message_tab() is None:
            count = self.mail_panel.envelope_panel.get_selected_item_count()
            if count > 1:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "reply_one_at_a_time",
                    default="Reply works on one message at a time. Please "
                            "select a single message.",
                ))
                return
        self._start_reply(all_recipients=False)

    def _on_reply_all(self, event):
        """Reply to all recipients of the selected message (Q1 answer: block multi-select).
        See _on_reply for why the block is skipped when a message tab is focused."""
        if self._current_message_tab() is None:
            count = self.mail_panel.envelope_panel.get_selected_item_count()
            if count > 1:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "reply_one_at_a_time",
                    default="Reply works on one message at a time. Please "
                            "select a single message.",
                ))
                return
        self._start_reply(all_recipients=True)

    def _start_reply(self, all_recipients):
        account, folder, envelope, message = self._current_message_context()
        if envelope is None or account is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "select_message_to_reply",
                default="Select a message to reply to first.",
            ))
            return

        if message is not None:
            self._open_reply_compose(account, envelope, message, all_recipients)
            return

        # Selected from the list rather than open in a tab: no body
        # loaded yet. Try the message cache first (likely already
        # warmed by the background prefetch) before falling back to
        # a live read on the interactive worker.
        backend = _envelope_backend(envelope)
        message_id = envelope.get("id")
        cached_message = himalaya_client.read_message_cached_only(
            self.paths, account, message_id, folder=folder, backend=backend,
        )
        if cached_message is not None:
            self._open_reply_compose(account, envelope, cached_message, all_recipients)
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "preparing_reply", default="Preparing reply..."
        ))

        def work():
            return himalaya_client.read_message(
                self.paths, account, message_id, folder=folder, backend=backend,
            )

        def on_success(fetched_message):
            self.GetStatusBar().SetStatusText(
                lang.t("main_ui", "ready", default="Ready.")
            )
            self._open_reply_compose(account, envelope, fetched_message, all_recipients)

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "reply_load_failed",
                default="Could not load the original message for reply.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "reply_load_failed",
                    default=f"Could not load the message to reply to.\n\n{exc}",
                    error=exc,
                ),
                lang.t("dialogs", "title_reply", default="Reply"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_read(account.account_id, backend).submit(work, on_success, on_error)

    def _open_reply_compose(self, account, envelope, message, all_recipients):
        to_field, covered = _reply_recipient(envelope, message)
        # Reply All must not Cc anyone already in To -- which, with
        # Reply-To honoured, is no longer always just the From address.
        cc_field = _reply_all_cc_field(account, envelope, covered) if all_recipients else ""
        subject = _reply_subject(envelope.get("subject"))
        body = _quoted_reply_body(envelope, message)
        in_reply_to, references = threading_headers(message)

        panel = ComposePanel(
            self.notebook, self,
            from_identity=_reply_from_identity(account, envelope),
            to=to_field,
            cc=cc_field,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )
        title = subject if len(subject) <= 30 else subject[:27] + "..."
        self.open_tab(panel, title)

    def _on_forward(self, event):
        account, folder, envelope, message = self._current_message_context()
        if envelope is None or account is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "select_message_to_forward",
                default="Select a message to forward first.",
            ))
            return

        if message is not None:
            self._open_forward_compose(account, envelope, message)
            return

        backend = _envelope_backend(envelope)
        message_id = envelope.get("id")
        cached_message = himalaya_client.read_message_cached_only(
            self.paths, account, message_id, folder=folder, backend=backend,
        )
        if cached_message is not None:
            self._open_forward_compose(account, envelope, cached_message)
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "preparing_forward",
            default="Preparing message to forward...",
        ))

        def work():
            return himalaya_client.read_message(
                self.paths, account, message_id, folder=folder, backend=backend,
            )

        def on_success(fetched_message):
            self.GetStatusBar().SetStatusText(
                lang.t("main_ui", "ready", default="Ready.")
            )
            self._open_forward_compose(account, envelope, fetched_message)

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "forward_load_failed",
                default="Could not load the message to forward.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "forward_load_failed",
                    default=f"Could not load the message to forward.\n\n{exc}",
                    error=exc,
                ),
                lang.t("dialogs", "title_forward", default="Forward"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_read(account.account_id, backend).submit(work, on_success, on_error)

    def _open_forward_compose(self, account, envelope, message):
        subject = _forward_subject(envelope.get("subject"))
        body = _forwarded_body(envelope, message)
        # References but not In-Reply-To, which is what Thunderbird
        # does on a forward: the new message belongs to the same
        # conversation history, but it is not a reply to anything and
        # claiming otherwise makes threads knot together.
        _in_reply_to, references = threading_headers(message)

        panel = ComposePanel(
            self.notebook, self,
            from_identity=account.identity_email,
            subject=subject,
            body=body,
            references=references,
        )
        title = subject if len(subject) <= 30 else subject[:27] + "..."
        self.open_tab(panel, title)

    def _on_list_delete_key(self, action, shift):
        if action == "delete":
            self._delete_from_selection(shift)
        elif action == "flag":
            self._toggle_flag_from_selection()
        elif action == "archive":
            self._archive_from_selection()
        elif action == "junk":
            self._mark_junk_from_selection()
        elif action == "not_junk":
            self._mark_not_junk_from_selection()
        elif action == "watch_thread":
            self._toggle_watch_thread()
        elif action == "ignore_thread":
            self._toggle_ignore_thread()

    def _on_selection_announce(self, message):
        """
        Speaks a multi-select count (e.g. "3 messages selected.") via
        the status bar -- the same channel every other action in ZBox
        already uses for spoken feedback ("Deleted 3 messages.", etc.),
        so no new announcement mechanism is introduced. Called by
        EnvelopeListPanel once a Shift+Up/Down sweep or Ctrl+A settles.
        """
        self.GetStatusBar().SetStatusText(message)

    def _on_delete_message(self, event):
        # Delete key / menu: move to Trash. Shift+Delete handles the
        # permanent variant (see _on_delete_permanent).
        self._delete_from_selection(permanent=False)

    def _on_delete_permanent(self, event):
        self._delete_from_selection(permanent=True)

    def _gather_bulk_contexts(self, none_selected_text, still_loading_text):
        """
        The three steps every bulk action (Delete, Archive, Move To,
        Copy To, flag toggle, Mark Read/Unread) starts with: get
        every selected envelope, drop cache-preview rows that are
        still loading (they can't be acted on until the live fetch
        replaces them, which takes about a second in the background),
        and resolve what's left to (account, folder, message_id).

        Sets the status bar and returns [] on either empty case, so
        callers just check for an empty result and return -- see
        audit item 24: this and _run_bulk_action below are what the
        six near-identical bulk actions were collapsed onto.
        """
        envelopes = self.mail_panel.envelope_panel.get_all_selected_envelopes()
        if not envelopes:
            self.GetStatusBar().SetStatusText(none_selected_text)
            return []

        live_envelopes = list(envelopes)
        # Rows still showing the offline copy are acted on too, not
        # dropped as "still loading": every job resolves their server
        # ID by Message-ID first (himalaya_client.resolve_live_items,
        # and _resolving_action for the per-message actions). Dropping
        # them made two selected rows act as one.

        contexts = []
        for envelope in live_envelopes:
            context = self._resolve_envelope_context(envelope)
            if context is not None:
                contexts.append((envelope, context))
        return contexts

    def _run_bulk_action(
        self, contexts, running_text, done_text,
        error_status_text, error_title, error_prefix,
        action, on_success_extra=None,
    ):
        """
        Runs action(envelope, account, folder, message_id) for every
        context on the worker thread, then reports the result -- the
        submit/status/error plumbing that used to be copy-pasted once
        per bulk action. running_text and done_text are shown as-is
        (running_text gets "..." appended); on_success_extra, when
        given, runs after done_text is set, for whatever a particular
        action still needs done on the UI thread (a folder refresh
        for Delete/Archive, per-row updates for flag/read state).

        A single selected message keeps the plain all-or-nothing path
        below: submit, then one status-bar line or one error dialog.
        More than one goes through _run_bulk_action_multi instead --
        audit finding 34: the old version ran every context inside
        one try, so the first failure raised out of the whole batch
        and silently abandoned everything after it, with no way to
        cancel a long run either.
        """
        if not contexts:
            return

        action = self._resolving_action(action)

        if len(contexts) > 1:
            self._run_bulk_action_multi(
                contexts, done_text, error_title, action, on_success_extra,
            )
            return

        self.GetStatusBar().SetStatusText(f"{running_text}...")

        def work():
            for envelope, (account, folder, message_id) in contexts:
                action(envelope, account, folder, message_id)

        def on_success(_result):
            self.GetStatusBar().SetStatusText(done_text)
            if on_success_extra is not None:
                on_success_extra(None)

        def on_error(exc):
            self.GetStatusBar().SetStatusText(error_status_text)
            wx.MessageBox(f"{error_prefix}\n\n{exc}", error_title, wx.OK | wx.ICON_ERROR)

        self._lane_for_accounts(
            account.account_id for _envelope, (account, _folder, _id) in contexts
        ).submit(work, on_success, on_error)

    def _run_bulk_action_multi(self, contexts, done_text, title, action, on_success_extra):
        """
        The multi-message path: a progress dialog with a Cancel
        button, continue-on-error (bulk_execution.run_bulk) instead of
        aborting the whole batch on the first failure, and a spoken
        summary at the end when anything other than complete success
        happened -- cancelled, or some messages failed. Complete
        success stays a quiet status-bar line, matching every other
        bulk action and announce.py's own rule: interrupt only for
        something that exists to be heard.
        """
        total = len(contexts)
        dialog = BulkProgressDialog(self, title, total)

        def _on_progress(completed, done):
            dialog.update(completed, done)
            self._play_progress_tick()

        def work():
            return run_bulk(
                contexts, action,
                is_cancelled=dialog.is_cancelled,
                on_progress=lambda completed, done: wx.CallAfter(_on_progress, completed, done),
            )

        def on_success(result):
            dialog.finish()
            status_text = summarize(result, done_text)
            self.GetStatusBar().SetStatusText(status_text)
            spoken_title = _action_name(title)
            if result.cancelled or result.failures:
                announce(self, status_text, title=spoken_title)
            else:
                self.announce_action(status_text, title=spoken_title)
            if on_success_extra is not None:
                on_success_extra(result)

        def on_error(exc):
            # A failure here means the worker job itself blew up
            # (not one item's action() -- run_bulk already turned
            # those into result.failures), so nothing in contexts is
            # known to have been attempted safely.
            dialog.finish()
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "action_failed",
                default=f"{title} failed.", action=_action_name(title),
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "action_failed",
                    default=f"Could not complete this action.\n\n{exc}", error=exc,
                ),
                title, wx.OK | wx.ICON_ERROR,
            )

        self._lane_for_accounts(
            account.account_id for _envelope, (account, _folder, _id) in contexts
        ).submit(work, on_success, on_error)
        dialog.ShowModal()
        dialog.Destroy()

    def _on_menu_open(self, event):
        # EVT_MENU_OPEN fires for every top-level menu as it opens,
        # not just Edit -- only refresh the Undo label/enabled state
        # when it's actually about to be shown.
        if event.GetMenu() is self._edit_menu:
            self._refresh_undo_menu_item()
        if event.GetMenu() is self._tools_menu:
            self._refresh_mute_account_menu_item()
            self._refresh_pause_auto_check_menu_item()
            self._refresh_disable_account_menu_item()
        if event.GetMenu() is self._message_menu:
            self._refresh_thread_menu_items()
            self._refresh_flag_menu_item()
        if event.GetMenu() is self._compose_menu:
            self._hotkeys_item.Check(self.hotkeys_diagnostic)
        event.Skip()

    def _on_toggle_hotkeys_diagnostic(self, _event):
        self.toggle_hotkeys_diagnostic()

    def toggle_hotkeys_diagnostic(self):
        """Compose > Hotkeys Diagnostic, and Ctrl+Shift+F12 from
        inside a compose tab.

        One switch for the whole application. Every open compose tab
        is armed or disarmed with it, and a tab opened afterwards
        reads it as it is built, so two tabs can never disagree.

        The state is announced, not only ticked in the menu. A key
        pressed inside the composer's WebView never reaches the menu
        bar, so the check mark is not what tells anyone what just
        happened.
        """
        self.hotkeys_diagnostic = not self.hotkeys_diagnostic
        for index in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(index)
            if isinstance(page, ComposePanel):
                page.body.set_hotkey_logging(self.hotkeys_diagnostic)
        item = getattr(self, "_hotkeys_item", None)
        if item is not None:
            item.Check(self.hotkeys_diagnostic)
        self.announce_action(
            lang.t(
                "actions_announcements",
                "hotkeys_diagnostic_on" if self.hotkeys_diagnostic
                else "hotkeys_diagnostic_off",
                default=("Hotkeys diagnostic on, checked" if self.hotkeys_diagnostic
                         else "Hotkeys diagnostic off, unchecked"),
            ),
            title=lang.t(
                "dialogs", "title_hotkeys_diagnostic",
                default="Hotkeys Diagnostic",
            ),
        )

    def _refresh_mute_account_menu_item(self):
        """Keeps Tools > Mute Account's checkmark in sync with
        whichever account is currently selected in the tree -- it has
        no target of its own the way Account Settings/Remove Account
        don't either, so it's disabled with no account selected
        rather than left checked or unchecked for nothing."""
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        item = self._tools_menu.FindItemById(ID_MUTE_ACCOUNT)
        if item is None:
            return
        item.Enable(account is not None)
        item.Check(bool(account and account.notifications_muted))

    def _refresh_pause_auto_check_menu_item(self):
        """Keeps Tools > Pause Automatic Checking's check mark in sync
        with the account selected in the tree, same shape as Mute
        Account above. Checked means auto_check is off."""
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        item = self._tools_menu.FindItemById(ID_PAUSE_AUTO_CHECK)
        if item is None:
            return
        item.Enable(account is not None)
        item.Check(bool(account and not getattr(account, "auto_check", True)))

    def _refresh_disable_account_menu_item(self):
        """Keeps Tools > Disable Account in sync with whichever account
        is currently selected in the tree, same shape as Mute Account
        above -- except this one relabels rather than checks. A
        checked/unchecked 'Disable Account' reads as a description of
        the account's current state; what the item actually does on
        the next click is the opposite of that, which is exactly what
        the label now says: 'Disable Account' when enabled, 'Enable
        Account' once it isn't."""
        account_id = self.mail_panel.account_panel.selected_account_id()
        account = self._find_account(account_id)
        item = self._tools_menu.FindItemById(ID_DISABLE_ACCOUNT)
        if item is None:
            return
        item.Enable(account is not None)
        item.SetItemLabel(
            lang.menu_label('tools_enable_account', "Enable Account") if (account and not account.enabled) else lang.menu_label('tools_disable_account', "Disable Account")
        )

    def _refresh_undo_menu_item(self):
        """Keeps the Edit > Undo item in sync with undo_manager: its
        label names the action it would reverse ("Undo Archive"),
        same as Thunderbird's own dynamic Undo label, and it's
        disabled with nothing to undo so NVDA/JAWS announce
        "unavailable" rather than reading a misleading generic
        "Undo". Called right after every _set_undo_record/_on_undo,
        not only from _on_menu_open, so Ctrl+Z's enabled state is
        always current even if Edit was never opened with the mouse."""
        label = self.undo_manager.describe()
        if label:
            self.undo_menu_item.SetItemLabel(
                lang.t(
                    "menus", "edit_undo_action", default="Undo {action}",
                    action=_action_name(label),
                ) + "\tCtrl+Z"
            )
            self.undo_menu_item.Enable(True)
        else:
            self.undo_menu_item.SetItemLabel(lang.menu_label("edit_undo", "Undo\tCtrl+Z"))
            self.undo_menu_item.Enable(False)

    def _set_undo_record(self, label, moves):
        """Records `moves` as the new single-level undo entry (audit
        finding 48) and refreshes the Edit menu to match. Moves with
        no Message-ID header are dropped by UndoManager itself
        (nothing to re-find them by later); if none are left, this
        clears whatever undo entry existed before rather than leaving
        a stale one an undo could apply to the wrong messages."""
        self.undo_manager.record(label, moves)
        self._refresh_undo_menu_item()

    def _on_undo(self, event):
        """Edit > Undo / Ctrl+Z: reverses the last Delete, Archive,
        Mark as Junk or Mark as Not Junk (audit finding 48). Single-
        level -- calling this again immediately after has nothing to
        undo, by design (see undo_manager.UndoManager)."""
        record = self.undo_manager.take()
        self._refresh_undo_menu_item()
        if record is None:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "nothing_to_undo", default="Nothing to undo."
            ))
            return

        count = len(record.moves)
        running_text = (
            lang.t(
                "main_ui", "undo_running_one", default="Undoing {action}",
                action=_action_name(record.label),
            )
            if count == 1
            else lang.t(
                "main_ui", "undo_running_many", default="Undoing {action} ({count} messages)",
                action=_action_name(record.label), count=count,
            )
        )
        action = _action_name(record.label)
        done_text = (
            lang.t(
                "actions_announcements", "undo_done_one",
                default=f"Undid {action}.", action=action,
            )
            if count == 1
            else lang.t(
                "actions_announcements", "undo_done_many",
                default=f"Undid {action} for {count} messages.",
                action=action, count=count,
            )
        )

        for move in record.moves:
            self._expect_arrivals(move.account, move.from_folder, [move.message_id_header])
        self.GetStatusBar().SetStatusText(f"{running_text}...")

        def work():
            return himalaya_client.undo_moves(self.paths, record.moves)

        def on_success(unresolved):
            if unresolved:
                self.GetStatusBar().SetStatusText(lang.t(
                    "main_ui", "undo_partly_restored",
                    default=f"Undid {_action_name(record.label)}, but "
                            f"{len(unresolved)} could not be restored -- they "
                            f"may have been moved, deleted or expired again "
                            f"since.",
                    action=_action_name(record.label), count=len(unresolved),
                ))
            else:
                self.GetStatusBar().SetStatusText(done_text)
                self.announce_action(done_text, title=_action_name("Undo"))
            restored = [move for move in record.moves if move not in unresolved]
            if record.label == "Mark as Junk":
                self.junk_rules.undo_block([move.message_id_header for move in restored])
            elif record.label == "Mark as Not Junk":
                self.junk_rules.undo_allow([move.message_id_header for move in restored])
            if record.label in ("Mark as Junk", "Mark as Not Junk"):
                # Keep junk_origin.py's record in step with what Undo
                # actually reversed -- only for moves that were really
                # restored, never for ones left in unresolved.
                for move in restored:
                    store = self._junk_origin_store(move.account.account_id)
                    if record.label == "Mark as Not Junk":
                        # Back in Junk now -- re-record where it had
                        # been sent, so a second Mark as Not Junk
                        # still knows where to send it.
                        store.set_origin(move.message_id_header, move.to_folder)
                    else:
                        # Back out of Junk entirely -- nothing left to
                        # restore it from.
                        store.clear_origin(move.message_id_header)
            self._auto_refresh_current_folder()

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "undo_failed",
                default=f"Could not undo {_action_name(record.label)}.",
                action=_action_name(record.label),
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "undo_failed",
                    default=f"Could not undo {record.label}.\n\n{exc}",
                    label=record.label, error=exc,
                ),
                lang.t("dialogs", "title_undo", default="Undo"),
                wx.OK | wx.ICON_ERROR,
            )

        self._lane_for_accounts(
            move.account.account_id for move in record.moves
        ).submit(work, on_success, on_error)

    def _live_message_id(self, envelope, account, folder, message_id):
        """message_id, or the server UID for a row still showing the
        offline copy, matched by Message-ID. Worker thread only: it may
        list the folder live. Unmatched ids come back unchanged."""
        if message_id is None:
            return message_id
        try:
            int(message_id)
            return message_id
        except (TypeError, ValueError):
            pass
        header = envelope.get("message-id") if isinstance(envelope, dict) else None
        resolved = himalaya_client.resolve_live_items(
            self.paths, account, [(message_id, folder, header)],
        )
        return resolved[0][0] if resolved else message_id

    def _resolving_action(self, action):
        """A bulk action that first swaps an offline-copy id for the
        server UID (Flag, Mark Read/Unread, Copy To)."""
        def wrapped(envelope, account, folder, message_id):
            return action(
                envelope, account, folder,
                self._live_message_id(envelope, account, folder, message_id),
            )
        return wrapped

    def _pending_hidden_keys(self):
        """
        The Mail tab list's hidden_keys_provider: every (account_id or
        None, uid) key a batched Delete or Shift+Delete has taken off
        the list, for the folder the tree has selected now. Expired
        entries are dropped as they are read.

        Only Unified rows carry _zbox_account_id (mail_fetch stamps it
        there and nowhere else), so a single-account folder hides
        (None, uid) and a Unified folder hides (account_id, uid), its
        display name resolved per account the same way
        _resolve_envelope_context does.
        """
        pending = getattr(self, "_pending_removals", None)
        if not pending:
            return set()
        now = time.monotonic()
        for location in list(pending):
            uids = pending[location]
            for uid in [uid for uid, expiry in uids.items() if expiry <= now]:
                del uids[uid]
            if not uids:
                del pending[location]
        if not pending:
            return set()
        target = self.mail_panel.account_panel.selected_fetch_target()
        if not target:
            return set()
        keys = set()
        for (account_id, folder), uids in pending.items():
            if "unified" in target:
                account = self._find_account(account_id)
                if account is None:
                    continue
                shown = _folder_display_to_himalaya(target["unified"], account)
                if himalaya_client._same_folder(folder, shown):
                    keys.update((account_id, uid) for uid in uids)
            elif account_id == target.get("account_id") and himalaya_client._same_folder(folder, target.get("folder")):
                keys.update((None, uid) for uid in uids)
        return keys

    def _hold_pending_removals(self, account, items):
        """Hides `items`, (uid, folder, header) for one account, from
        the list until their job reports back. Hidden by Message-ID as
        well as by id: a row from the offline copy and its live twin
        have different ids, and only the header says they are one."""
        from envelope_list_panel import hide_key_for_header

        for uid, folder, header in items:
            location = (account.account_id, folder)
            hidden = self._pending_removals.setdefault(location, {})
            hidden[str(uid)] = float("inf")
            header_key = hide_key_for_header(header)
            if header_key:
                hidden[header_key] = float("inf")

    def _settle_pending_removals(self, account, items, clear):
        """
        A delete job for `account` is back. Entries in `clear` are
        still listed where they were, so they show again at once. The
        rest stay hidden two more minutes, long enough for a listing
        that raced the move to be replaced by one taken after it.
        """
        clear_keys = {(str(entry[0]), entry[1]) for entry in clear}
        expiry = time.monotonic() + 120
        from envelope_list_panel import hide_key_for_header

        for uid, folder, header in items:
            location = (account.account_id, folder)
            uids = self._pending_removals.get(location)
            if uids is None:
                continue
            cleared = (str(uid), folder) in clear_keys
            for key in (str(uid), hide_key_for_header(header)):
                if not key or key not in uids:
                    continue
                if cleared:
                    del uids[key]
                else:
                    uids[key] = expiry
            if not uids:
                del self._pending_removals[location]

    def _refresh_after_removal(self):
        """
        _auto_refresh_current_folder without its has_content() guard.
        drop_hidden_rows can empty the list, which puts the placeholder
        up, and the guard would then skip the refresh every delete has
        to end with (finding b): a message whose move failed would
        never come back. A silent fetch only ever calls
        populate_if_changed, so there is no loading state to fight.
        """
        target = self.mail_panel.account_panel.selected_fetch_target()
        if not target:
            return
        self._envelope_request_id += 1
        request_id = self._envelope_request_id
        if "unified" in target:
            self._fetch_unified_envelopes(target["unified"], request_id, silent=True)
        else:
            self._fetch_account_envelopes(target["account_id"], target["folder"], request_id, silent=True)

    def _delete_from_selection(self, permanent):
        """
        Delete (to Trash) and Shift+Delete (permanent) for the list
        selection, one message or many.

        Both take the selected rows off the list at once
        (drop_hidden_rows), then run one worker job per account:
        trash_messages_batch for Delete, permanently_delete_messages
        for Shift+Delete. A message already in Trash is permanently
        deleted either way, as Thunderbird does. Every job ends with a
        refresh, errors included. One report follows once every job is
        back: a status line, one undo record for Delete, and a modal
        only when something was not done.

        This replaced the per-message BulkProgressDialog path for
        Delete: one move per source folder is fast enough that a
        progress dialog and Cancel button had nothing left to show.
        """
        contexts = self._gather_bulk_contexts(
            lang.t('main_ui', 'bulk_none_delete', default="No message selected to delete."),
            lang.t('main_ui', 'bulk_loading_delete', default="Still loading message(s) -- try deleting again in a moment."),
        )
        if not contexts:
            return

        count = len(contexts)
        if permanent:
            msg = (lang.t('dialogs', 'perm_delete_one', default='Permanently delete {count} message?', count=count) if count == 1
                   else lang.t('dialogs', 'perm_delete_many', default='Permanently delete {count} messages?', count=count))
            confirm = wx.MessageBox(
                lang.t('dialogs', 'perm_delete_confirm', default='{msg} This skips the Trash folder and cannot be undone.', msg=msg),
                lang.t(
                    "dialogs", "title_delete_permanently",
                    default="Delete Permanently",
                ),
                wx.YES_NO | wx.ICON_WARNING,
            )
            if confirm != wx.YES:
                return

        by_account = {}
        for envelope, (account, folder, message_id) in contexts:
            by_account.setdefault(account, []).append(
                (message_id, folder, envelope.get("message-id"))
            )

        for account, items in by_account.items():
            self._hold_pending_removals(account, items)
        self.mail_panel.envelope_panel.drop_hidden_rows()

        if permanent:
            running = (
                lang.t(
                    "main_ui", "deleting_one",
                    default="Permanently deleting message",
                )
                if count == 1
                else lang.t(
                    "main_ui", "deleting_many",
                    default=f"Permanently deleting {count} messages",
                    count=count,
                )
            )
        else:
            running = (
                lang.t(
                    "main_ui", "trashing_one",
                    default="Moving message to Trash",
                )
                if count == 1
                else lang.t(
                    "main_ui", "trashing_many",
                    default=f"Moving {count} messages to Trash", count=count,
                )
            )
        self.GetStatusBar().SetStatusText(f"{running}...")

        # Spoken here rather than in _report_delete, for the same
        # reason moves are: the rows are already off the list, and
        # waiting for the worker means a fresh SELECT of a large
        # Inbox first -- seconds of silence after a keypress. If
        # anything did not delete, _report_delete still raises its
        # modal, which is read aloud, so nothing is claimed that did
        # not happen.
        if permanent:
            spoken = lang.t(
                "actions_announcements",
                "deleted_one" if count == 1 else "deleted_many",
                default=("Message permanently deleted." if count == 1
                         else "Permanently deleted {count} messages."),
                count=count,
            )
        else:
            spoken = lang.t(
                "actions_announcements",
                "trashed_one" if count == 1 else "trashed_many",
                default=("Message moved to Trash." if count == 1
                         else "Moved {count} messages to Trash."),
                count=count,
            )
        self.announce_action(
            spoken,
            title=lang.t(
                "dialogs", "title_delete_permanently",
                default="Delete Permanently",
            )
            if permanent
            else lang.t("dialogs", "title_delete", default="Delete")
        )

        report = {"left": len(by_account), "done": 0, "moves": [], "unresolved": [], "errors": []}

        def finish_one():
            report["left"] -= 1
            self._refresh_after_removal()
            if report["left"] == 0:
                self._report_delete(permanent, count, report)

        for account, items in by_account.items():
            self._submit_delete_job(account, items, permanent, report, finish_one)

    def _delete_queue_account(self, account_id):
        """The live Account for delete_queue, or None once removed.
        Called from its threads; only reads the account list."""
        for account in list(self.account_manager.accounts):
            if account.account_id == account_id:
                return account
        return None

    def _on_delete_batch_done(self, batch_id, account_id, items, moved, unresolved):
        """delete_queue finished a batch; main thread (wx.CallAfter).
        A batch from this session goes to the callback that started
        it. One carried over from an earlier run settles its rows
        here and refreshes, with a status line if anything is left."""
        if self._shutting_down:
            return
        callback = self._delete_callbacks.pop(batch_id, None)
        if callback is not None:
            callback(moved, unresolved)
            return
        account = self._delete_queue_account(account_id)
        if account is None:
            return
        self._settle_pending_removals(account, items, list(unresolved))
        self._refresh_after_removal()
        if unresolved:
            # Resumed on its own from an earlier run, not started now,
            # so it is logged rather than put on the status bar.
            logging.getLogger("zbox.main").info(
                "%d message(s) from an earlier delete could not be deleted.",
                len(unresolved),
            )

    def _resume_pending_deletes(self):
        """Hides the rows of deletes saved by an earlier run, then lets
        delete_queue carry on with them."""
        for batch in self.delete_queue.pending():
            account = self._delete_queue_account(batch["account_id"])
            if account is not None:
                self._hold_pending_removals(account, [tuple(item) for item in batch["items"]])
        self.mail_panel.envelope_panel.drop_hidden_rows()
        self.delete_queue.start()

    def _submit_delete_job(self, account, items, permanent, report, finish_one):
        """One account's half of _delete_from_selection, handed to
        delete_queue. Its own method so each batch's callback closes
        over its own account and items."""
        trash_folder = _folder_display_to_himalaya("Trash", account)
        elsewhere = [item for item in items if not himalaya_client._same_folder(item[1], trash_folder)]

        def on_success(result):
            moved, unresolved = result
            # Still listed where they were: never moved, or failed
            # while already in Trash.
            clear = [
                entry for entry in unresolved
                if entry[2] in ("move_failed", "delete_failed")
                or himalaya_client._same_folder(entry[1], trash_folder)
            ]
            self._settle_pending_removals(account, items, clear)
            report["moves"].extend(
                UndoableMove(account, item[2], item[1], trash_folder) for item in moved
            )
            report["done"] += len(items) - len(unresolved)
            report["unresolved"].extend(unresolved)
            finish_one()

        self._expect_arrivals(account, trash_folder, [item[2] for item in elsewhere])
        batch_id = self.delete_queue.add(account, items, permanent, trash_folder)
        # Registered before the queue can report back: its report comes
        # through wx.CallAfter, which runs after this returns.
        self._delete_callbacks[batch_id] = (
            lambda moved, unresolved: on_success((moved, unresolved))
        )

    def _report_delete(self, permanent, count, report):
        """The single report for a finished Delete or Shift+Delete."""
        if not permanent:
            self._set_undo_record("Delete", report["moves"])
        if report["done"]:
            self._play_sound("delete")
        unresolved = report["unresolved"]
        errors = report["errors"]
        if not unresolved and not errors:
            if permanent:
                text = (
                    lang.t(
                        "actions_announcements", "deleted_one",
                        default="Message permanently deleted.",
                    )
                    if count == 1
                    else lang.t(
                        "actions_announcements", "deleted_many",
                        default=f"Permanently deleted {count} messages.",
                        count=count,
                    )
                )
            else:
                text = (
                    lang.t(
                        "actions_announcements", "trashed_one",
                        default="Message moved to Trash.",
                    )
                    if count == 1
                    else lang.t(
                        "actions_announcements", "trashed_many",
                        default=f"Moved {count} messages to Trash.", count=count,
                    )
                )
            self.GetStatusBar().SetStatusText(text)
            return

        not_moved = sum(1 for entry in unresolved if entry[2] == "move_failed")
        left_in_trash = len(unresolved) - not_moved
        verb = lang.t('main_ui', 'report_verb_permanent', default="Permanently deleted") if permanent else lang.t('main_ui', 'report_verb_deleted', default="Deleted")
        lines = [lang.t('main_ui', 'report_done', default='{verb} {done} of {count} message(s).', verb=verb, done=report['done'], count=count)]
        if not_moved:
            lines.append(
                f"Not moved to Trash, still in the original folder: {not_moved}."
            )
        if left_in_trash:
            lines.append(
                f"In Trash, but the server refused to delete them "
                f"permanently: {left_in_trash}."
            )
        self.GetStatusBar().SetStatusText(" ".join(lines))
        for account, exc in errors:
            lines.append(f"{account.identity_email}: {exc}")
        wx.MessageBox(
            "\n\n".join(lines),
            lang.t(
                "dialogs", "title_delete_permanently",
                default="Delete Permanently",
            ) if permanent else lang.t("dialogs", "title_delete", default="Delete"),
            wx.OK | wx.ICON_WARNING,
        )

    def _archive_from_selection(self):
        """
        Moves selected message(s) to the Archive folder (provider-specific:
        [Gmail]/All Mail for Gmail, Archive for others), through
        _move_selection.
        """
        contexts = self._gather_bulk_contexts(
            lang.t('main_ui', 'bulk_none_archive', default="No message selected to archive."),
            lang.t('main_ui', 'bulk_loading_archive', default="Still loading message(s) -- try archiving again in a moment."),
        )
        if not contexts:
            return
        count = len(contexts)
        self._move_selection(
            contexts, "Archive",
            lambda envelope, account, folder: _folder_display_to_himalaya("Archive", account),
            "Archiving" if count == 1 else f"Archiving {count} messages",
            "Archived." if count == 1 else f"Archived {count} messages.",
        )

    def _mark_junk_from_selection(self):
        """
        Audit finding 46: moves selected message(s) to this account's
        Junk folder (per-account override in Account Settings, else
        the same provider-aware mapping Archive/Trash already use --
        see envelope_format._junk_folder_for_account), through
        _move_selection, and records where each message that actually
        moved came from, for Mark as Not Junk.

        Each sender also goes on the junk rules' blocked list at once,
        whether or not the move succeeds: that is a judgment of the
        sender, not of the move. Mailing-list mail and mail from the
        user's own addresses are skipped (junk_rules.block_senders).
        """
        contexts = self._gather_bulk_contexts(
            lang.t('main_ui', 'bulk_none_junk', default="No message selected to mark as junk."),
            lang.t('main_ui', 'bulk_loading_generic', default="Still loading message(s) -- try again in a moment."),
        )
        if not contexts:
            return
        count = len(contexts)
        newly_blocked = self.junk_rules.block_senders(
            [envelope for envelope, _context in contexts], self._own_addresses(),
        )
        # block_senders skips mailing-list mail and the user's own
        # addresses, and adds nothing for a sender already blocked.
        # Saying nothing about that was indistinguishable from Mark as
        # Junk being broken: the message moved, the blocked list did
        # not change, and nothing explained why.
        done_text = (
            lang.t(
                "actions_announcements", "junk_marked_one",
                default="Marked as junk.",
            )
            if count == 1
            else lang.t(
                "actions_announcements", "junk_marked_many",
                default=f"Marked {count} messages as junk.", count=count,
            )
        )
        if not newly_blocked:
            done_text += " " + lang.t(
                "actions_announcements", "junk_no_new_sender",
                default=(
                    "No new sender blocked: already blocked, a mailing list, "
                    "or one of your own addresses."
                ),
            )

        def after_moves(moves):
            for move in moves:
                self._junk_origin_store(move.account.account_id).set_origin(
                    move.message_id_header, move.from_folder
                )

        self._move_selection(
            contexts, lang.t('dialogs', 'move_title_junk', default="Mark as Junk"),
            lambda envelope, account, folder: _junk_folder_for_account(account),
            lang.t('main_ui', 'running_junk_one', default="Marking as junk") if count == 1 else lang.t('main_ui', 'running_junk_many', default='Marking {count} messages as junk', count=count),
            done_text,
            after_moves=after_moves,
        )

    def _mark_not_junk_from_selection(self):
        """
        Audit finding 46's other half: moves selected message(s) back
        to wherever Mark as Junk moved them out of, when ZBox recorded
        that (see junk_origin.py) -- otherwise back to Inbox, which is
        also Thunderbird's own fallback when it has no prior folder
        recorded. Messages with different origins go in one move per
        origin folder, through _move_selection.

        Each sender goes on the junk rules' never-junk list and each
        message is exempted for good, at once and whatever the move
        does, so nothing ZBox runs can send them back to Junk.
        """
        contexts = self._gather_bulk_contexts(
            lang.t('main_ui', 'bulk_none_not_junk', default="No message selected to mark as not junk."),
            lang.t('main_ui', 'bulk_loading_generic', default="Still loading message(s) -- try again in a moment."),
        )
        if not contexts:
            return
        count = len(contexts)
        self.junk_rules.allow_senders([envelope for envelope, _context in contexts])

        def destination_for(envelope, account, folder):
            origin = self._junk_origin_store(account.account_id).get_origin(envelope.get("message-id"))
            return origin or _folder_display_to_himalaya("Inbox", account)

        def after_moves(moves):
            for move in moves:
                self._junk_origin_store(move.account.account_id).clear_origin(move.message_id_header)

        self._move_selection(
            contexts, lang.t('dialogs', 'move_title_not_junk', default="Mark as Not Junk"), destination_for,
            lang.t('main_ui', 'running_not_junk_one', default="Marking as not junk") if count == 1 else lang.t('main_ui', 'running_not_junk_many', default='Marking {count} messages as not junk', count=count),
            lang.t(
                "actions_announcements", "not_junk_marked_one",
                default="Marked as not junk.",
            )
            if count == 1
            else lang.t(
                "actions_announcements", "not_junk_marked_many",
                default=f"Marked {count} messages as not junk.", count=count,
            ),
            after_moves=after_moves,
        )

    def _move_selection(self, contexts, label, destination_for, running_text, done_text,
                        after_moves=None, undoable=True):
        """
        The shared pipeline for Archive, Mark as Junk, Mark as Not Junk
        and Move To: the move counterpart of _delete_from_selection.

        destination_for(envelope, account, folder) names each message's
        destination. A message already there, or with no destination
        (an account with no Junk folder), is left alone. The rest come
        off the list at once, are recorded as expected arrivals in
        their destination, and move in one worker job per account,
        one batched move per source and destination folder
        (himalaya_client.move_messages_batch). What moved is decided
        by re-reading each source folder, so the undo record, the
        after_moves callback and the report only ever cover messages
        that really moved.

        undoable=False leaves the undo record untouched, as Move To
        always has.
        """
        by_account = {}
        for envelope, (account, folder, message_id) in contexts:
            destination = destination_for(envelope, account, folder)
            if not destination or himalaya_client._same_folder(destination, folder):
                continue
            by_account.setdefault(account, []).append(
                ((message_id, folder, envelope.get("message-id")), destination)
            )
        count = sum(len(plan) for plan in by_account.values())
        if not count:
            if undoable:
                self._set_undo_record(label, [])
            self.GetStatusBar().SetStatusText(done_text)
            self.announce_action(done_text, title=_action_name(label))
            return

        for account, plan in by_account.items():
            self._hold_pending_removals(account, [item for item, _destination in plan])
            for item, destination in plan:
                self._expect_arrivals(account, destination, [item[2]])
        self.mail_panel.envelope_panel.drop_hidden_rows()
        self.GetStatusBar().SetStatusText(f"{running_text}...")
        # Said now, not when the worker comes back. The rows are
        # already off the list, so the action is done as far as the
        # reader is concerned; waiting for the job meant waiting for a
        # fresh SELECT of the source folder, which on a large Gmail
        # Inbox is seconds of silence before anything is spoken.
        # Failures still announce themselves through _report_moves'
        # modal, so nothing is claimed that did not happen.
        self.announce_action(done_text, title=_action_name(label))

        report = {"left": len(by_account), "moves": [], "failed": 0, "errors": []}

        def finish_one():
            report["left"] -= 1
            self._refresh_after_removal()
            if report["left"] == 0:
                self._report_moves(label, count, done_text, report, after_moves, undoable)

        for account, plan in by_account.items():
            self._submit_move_job(account, plan, label, report, finish_one)

    def _submit_move_job(self, account, plan, label, report, finish_one):
        """One account's half of _move_selection. Its own method so
        each job's callbacks close over their own account and plan."""
        items = [item for item, _destination in plan]

        def work():
            by_destination = {}
            for item, destination in plan:
                by_destination.setdefault(destination, []).append(item)
            moved = []
            failed = []
            for destination, group in by_destination.items():
                group_moved, group_failed = himalaya_client.move_messages_batch(
                    self.paths, account, group, destination,
                )
                moved.extend((item, destination) for item in group_moved)
                failed.extend(group_failed)
            return moved, failed

        def on_success(result):
            moved, failed = result
            # A failed move is still listed where it was.
            self._settle_pending_removals(account, items, failed)
            report["moves"].extend(
                UndoableMove(account, item[2], item[1], destination)
                for item, destination in moved
            )
            report["failed"] += len(failed)
            finish_one()

        def on_error(exc):
            logging.getLogger("zbox.main").warning(
                "%s job failed on %s: %s", label, account.account_id, exc,
            )
            self._settle_pending_removals(account, items, items)
            report["errors"].append((account, exc))
            finish_one()

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _report_moves(self, label, count, done_text, report, after_moves, undoable):
        """The single report for a finished _move_selection."""
        moves = report["moves"]
        if undoable:
            self._set_undo_record(label, moves)
        if after_moves is not None:
            after_moves(moves)
        failed = report["failed"]
        errors = report["errors"]
        if not failed and not errors:
            self.GetStatusBar().SetStatusText(done_text)
            return
        lines = [lang.t(
            "main_ui", "moves_report",
            default=f"{_action_name(label)}: moved {len(moves)} of {count} "
                    f"message(s).",
            action=_action_name(label), moved=len(moves), total=count,
        )]
        if failed:
            lines.append(lang.t(
                "main_ui", "moves_report_failed",
                default=f"Not moved, still in the original folder: {failed}.",
                count=failed,
            ))
        self.GetStatusBar().SetStatusText(" ".join(lines))
        for account, exc in errors:
            lines.append(f"{account.identity_email}: {exc}")
        wx.MessageBox("\n\n".join(lines), _action_name(label), wx.OK | wx.ICON_WARNING)

    def _copy_message(self, account, folder, message_id, to_folder):
        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "copying_message", default="Copying message..."
        ))

        def work():
            himalaya_client.copy_message(self.paths, account, message_id, folder, to_folder)

        def on_success(_result):
            copied = lang.t(
                "actions_announcements", "copied_to_folder",
                default="Copied to {to_folder}.", to_folder=to_folder,
            )
            self.GetStatusBar().SetStatusText(copied)
            self.announce_action(
                copied, title=lang.t("dialogs", "title_copy_to", default="Copy To")
            )

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "copy_failed", default="Copy failed."
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "copy_failed",
                    default=f"Could not copy message.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_copy_to", default="Copy To"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _bulk_move_messages(self, contexts, to_folder, label):
        """
        Moves multiple messages (from contexts list) to the specified
        target folder. Each message is moved from its own source folder,
        allowing cross-folder and cross-account moves.

        contexts: list of (envelope, (account, folder, message_id)) tuples
        to_folder: real Himalaya folder name (e.g., "[Gmail]/All Mail")
        label: display name for status bar (e.g., "Archive")
        """
        count = len(contexts)
        # Through _move_selection like Archive, but not undoable: Undo
        # has only ever covered Delete, Archive and the junk actions.
        self._move_selection(
            contexts, lang.t('dialogs', 'move_title_move_to', default="Move To"),
            lambda envelope, account, folder: to_folder,
            lang.t('main_ui', 'running_move_one', default="Moving") if count == 1 else lang.t('main_ui', 'running_move_many', default='Moving {count} messages', count=count),
            lang.t(
                "actions_announcements", "moved_to_one",
                default=f"Moved to {label}.", to_folder=label,
            )
            if count == 1
            else lang.t(
                "actions_announcements", "moved_to_many",
                default=f"Moved {count} messages to {label}.",
                count=count, to_folder=label,
            ),
            undoable=False,
        )

    def _bulk_copy_messages(self, contexts, to_folder):
        """
        Copies multiple messages (from contexts list) to the specified
        target folder. Each message is copied from its own source folder.

        contexts: list of (envelope, (account, folder, message_id)) tuples
        to_folder: real Himalaya folder name (e.g., "[Gmail]/All Mail")
        """
        count = len(contexts)
        running_text = lang.t('main_ui', 'running_copy_one', default="Copying message") if count == 1 else lang.t('main_ui', 'running_copy_many', default='Copying {count} messages', count=count)
        done_text = (
            lang.t(
                "actions_announcements", "copied_to_folder",
                default=f"Copied to {to_folder}.", to_folder=to_folder,
            )
            if count == 1
            else lang.t(
                "actions_announcements", "copied_to_many",
                default=f"Copied {count} messages to {to_folder}.",
                count=count, to_folder=to_folder,
            )
        )

        def action(envelope, account, folder, message_id):
            himalaya_client.copy_message(self.paths, account, message_id, folder, to_folder)

        self._run_bulk_action(
            contexts, running_text, done_text,
            lang.t('main_ui', 'bulk_copy_failed_status', default="Copy failed."), lang.t('dialogs', 'title_copy_to_err', default="Copy To"), lang.t('main_ui', 'bulk_copy_failed', default="Could not copy message(s)."),
            action,
        )

    def _toggle_flag_from_selection(self):
        """
        Toggles flagged state for the selected message(s) -- or,
        for a selected COLLAPSED thread row, every message in that
        thread (get_all_selected_envelopes expands it).

        target_state is False only when every message about to be
        acted on is ALREADY flagged; otherwise True. That makes the
        single-message case (by far the most common, via the bare "S"
        shortcut) a genuine toggle -- flagged becomes unflagged and
        back again -- and normalizes a mixed-state multi-selection to
        fully flagged on one press, fully unflagged on the next,
        rather than picking one arbitrary message's state for the
        rest to copy.

        (Supersedes the audit finding 26 fix from 2026-09-04, which
        matched the FIRST selected message's own state instead. That
        was confirmed with the user at the time, but it makes a
        single already-flagged message a no-op -- exactly the "flag/
        unflag does nothing" the user later ran into -- since nothing
        needs to "match" the one message already choosing its own
        state.)
        """
        contexts = self._gather_bulk_contexts(
            lang.t('main_ui', 'bulk_none', default="No message selected."),
            lang.t('main_ui', 'bulk_loading_flag', default="Still loading message(s) -- try flagging again in a moment."),
        )
        if not contexts:
            return

        target_state = not all(_envelope_is_flagged(envelope) for envelope, _context in contexts)

        count = len(contexts)
        running_text = f"Updating {count} flag(s)"

        def action(envelope, account, folder, message_id):
            himalaya_client.set_flagged(self.paths, account, message_id, folder, target_state)

        def update_rows(result=None):
            failed_ids = _failed_message_ids(contexts, result)
            succeeded_ids = [
                message_id for _envelope, (_account, _folder, message_id) in contexts
                if message_id not in failed_ids
            ]
            self.mail_panel.envelope_panel.mark_ids_flagged(succeeded_ids, target_state)

        self._run_bulk_action(
            contexts, running_text, lang.t('main_ui', 'flag_updated', default="Flag(s) updated."),
            lang.t('main_ui', 'flag_failed', default="Could not update flag(s)."), lang.t('dialogs', 'flag_title', default="Flag Message"), lang.t('main_ui', 'flag_failed', default="Could not update flag(s)."),
            action, on_success_extra=update_rows,
        )

    def _folders_for_account(self, account):
        """
        Folder names to offer in Move To / Copy To, freshest known
        list first. Returns the standard folder set immediately
        (matching the tree, which is itself a fixed list rather than
        server-fetched -- see AccountTreePanel.refresh_accounts) and
        kicks off a background 'mailbox list' to replace the cache
        for next time the menu opens, so custom folders eventually
        show up without ever blocking the popup on a network call.
        """
        cached = self._folder_cache.get(account.account_id)
        if cached is None:
            cached = ["Inbox", "Sent", "Drafts", "Trash", "Junk", "Archive"]

            def work():
                return himalaya_client.list_folders(self.paths, account)

            def on_success(folders):
                names = [f.get("name") for f in folders if isinstance(f, dict) and f.get("name")]
                if names:
                    self._folder_cache[account.account_id] = names

            def on_error(exc):
                logging.getLogger("zbox.main").warning(
                    "Could not list folders for %s: %s", account.account_id, exc
                )

            self.lanes.for_account(account.account_id).submit(work, on_success, on_error)
        return cached

    def _run_delete(self, account, folder, envelope, permanent, after_success=None):
        """
        Shared delete path used by the Delete / Shift+Delete keys in
        both the message list and message-body tabs, the toolbar and
        the menus. Non-permanent deletes move the message to Trash;
        permanent deletes skip Trash and are confirmed first.
        """
        logging.getLogger("zbox.main").debug(
            "_run_delete: account=%r folder=%r message_id=%r permanent=%r",
            account, folder, envelope.get("id"), permanent,
        )
        message_id = envelope.get("id")
        if message_id is None:
            return

        if permanent:
            confirm = wx.MessageBox(
                lang.t(
                    "dialogs", "delete_permanently_confirm_one",
                    default="Permanently delete this message? This skips the Trash "
                    "folder and cannot be undone.",
                ),
                lang.t(
                    "dialogs", "title_delete_permanently",
                    default="Delete Permanently",
                ),
                wx.YES_NO | wx.ICON_WARNING,
            )
            if confirm != wx.YES:
                return
            status_text = lang.t('main_ui', 'running_perm_delete_one', default="Permanently deleting message...")
            done_text = lang.t('main_ui', 'done_perm_delete_one', default="Message permanently deleted.")
        else:
            status_text = lang.t('main_ui', 'running_trash_one', default="Moving message to Trash...")
            done_text = lang.t('main_ui', 'done_trash_one', default="Message moved to Trash.")
        trash_folder = _folder_display_to_himalaya("Trash", account)

        self.GetStatusBar().SetStatusText(status_text)

        # The open message tab closes now, not when the server is
        # done: a permanent delete is a copy to Trash plus a purge,
        # and waiting on it kept the tab up for the whole round trip.
        # The delete carries on in the background; a failure still
        # reports itself below.
        if after_success is not None:
            after_success()

        # Off the list now too, the same way the list's own batched
        # Delete does it, rather than waiting for a refresh that
        # stands down while the reader is still moving through the
        # list. Settled when the job reports back: gone for good on
        # success, shown again on failure.
        removal = [(message_id, folder, envelope.get("message-id"))]
        self._hold_pending_removals(account, removal)
        self.mail_panel.envelope_panel.drop_hidden_rows()

        def on_success(_result):
            self._settle_pending_removals(account, removal, [])
            self.GetStatusBar().SetStatusText(done_text)
            self._auto_refresh_current_folder()

        def on_error(exc):
            self._settle_pending_removals(account, removal, removal)
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "delete_failed", default="Delete failed."
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "delete_failed",
                    default=f"Could not delete message.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_delete", default="Delete"),
                wx.OK | wx.ICON_ERROR,
            )

        def on_done(_moved, unresolved):
            if not unresolved:
                on_success(None)
                return
            reason = unresolved[0][2]
            if reason == "move_failed":
                problem = f"The server did not move it to Trash; it is still in {folder}."
            elif reason == "delete_failed":
                problem = lang.t('main_ui', 'delete_refused_still', default='The server refused to delete it; it is still in {folder}.', folder=folder)
            else:
                problem = lang.t('main_ui', 'delete_refused_trash', default="It is in Trash, but the server refused to delete it permanently.")
            on_error(RuntimeError(problem))

        # delete_queue runs it on this account's own thread and pooled
        # connection, retrying through network trouble, and finds the
        # message on the server by its Message-ID, so a row still
        # showing the offline copy deletes like any other.
        batch_id = self.delete_queue.add(account, removal, permanent, trash_folder)
        self._delete_callbacks[batch_id] = on_done

    def _run_archive(self, account, folder, envelope, after_success=None):
        """
        Shared single-message archive path for Alt+A (see
        _on_char_hook and message_view_panel._dispatch_hotkey's
        ALT_A) -- archives whichever message is open in its own tab,
        independent of the message list's own selection. Same shape
        as _run_delete just above, and deliberately NOT built on
        _archive_from_selection/_gather_bulk_contexts: those read the
        message LIST's current selection, which has nothing to do
        with which message is open and focused in its own tab (they
        could easily be two different messages).
        """
        message_id = envelope.get("id")
        if message_id is None:
            return
        archive_folder = _folder_display_to_himalaya("Archive", account)
        if archive_folder == folder:
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "already_in_archive", default="Already in Archive."
            ))
            return

        self.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "archiving", default="Archiving..."
        ))
        message_id_header = envelope.get("message-id")
        if message_id_header:
            self._expect_arrivals(account, archive_folder, [message_id_header])

        def work():
            # The batch path with one item: its recheck keeps Gmail's
            # late listing from reporting a failure that did not
            # happen, and it clears the offline copy.
            _moved, failed = himalaya_client.move_messages_batch(
                self.paths, account, [(message_id, folder, message_id_header)], archive_folder,
            )
            if failed:
                raise RuntimeError(f"The message is still in {folder}.")

        def on_success(_result):
            self.GetStatusBar().SetStatusText(lang.t(
                "actions_announcements", "archived", default="Archived."
            ))
            # Said here and nowhere earlier. Archive runs on the
            # worker, so the keypress itself knows nothing yet; saying
            # "Archived" before the move is confirmed would be a claim
            # the failure path then has to take back.
            self.announce_action(
                lang.t("actions_announcements", "archived", default="Archived."),
                title=_action_name("Archive"),
            )
            if message_id_header:
                self._set_undo_record(
                    "Archive",
                    [UndoableMove(account, message_id_header, folder, archive_folder)],
                )
            if after_success is not None:
                after_success()
            self._auto_refresh_current_folder()

        def on_error(exc):
            self.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "archive_failed", default="Archive failed."
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "archive_failed",
                    default=f"Could not archive message.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_archive", default="Archive"),
                wx.OK | wx.ICON_ERROR,
            )

        self.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _on_mark_read(self, event):
        """Menu handler for Mark as Read."""
        self._mark_multiple_read(read=True)

    def _on_mark_unread(self, event):
        """Menu handler for Mark as Unread."""
        self._mark_multiple_read(read=False)

    def _mark_multiple_read(self, read):
        """
        Marks all selected message(s) as read or unread. Works on all
        selected messages at once. read=True marks as read, read=False
        marks as unread.
        """
        contexts = self._gather_bulk_contexts(
            lang.t('main_ui', 'bulk_none', default="No message selected."),
            lang.t('main_ui', 'bulk_loading_generic', default="Still loading message(s) -- try again in a moment."),
        )
        if not contexts:
            return

        count = len(contexts)
        if read:
            running_text = lang.t('main_ui', 'running_read_one', default="Marking as read") if count == 1 else lang.t('main_ui', 'running_read_many', default='Marking {count} messages as read', count=count)
            done_text = lang.t(
                "actions_announcements", "marked_read", default="Marked as read."
            )
        else:
            running_text = lang.t('main_ui', 'running_unread_one', default="Marking as unread") if count == 1 else lang.t('main_ui', 'running_unread_many', default='Marking {count} messages as unread', count=count)
            done_text = lang.t(
                "actions_announcements", "marked_unread", default="Marked as unread."
            )
        error_text = (
            lang.t("main_ui", "mark_read_failed", default="Could not mark message(s) as read.")
            if read
            else lang.t("main_ui", "mark_unread_failed", default="Could not mark message(s) as unread.")
        )

        def action(envelope, account, folder, message_id):
            if read:
                himalaya_client.mark_as_read(self.paths, account, message_id, folder=folder)
            else:
                himalaya_client.mark_as_unread(self.paths, account, message_id, folder=folder)

        def update_rows(result=None):
            failed_ids = _failed_message_ids(contexts, result)
            succeeded_ids = [
                message_id for _envelope, (_account, _folder, message_id) in contexts
                if message_id not in failed_ids
            ]
            if read:
                self.mail_panel.envelope_panel.mark_ids_read(succeeded_ids)
            else:
                self.mail_panel.envelope_panel.mark_ids_unread(succeeded_ids)

        self._run_bulk_action(
            contexts, running_text, done_text,
            error_text, lang.t('dialogs', 'title_mark_message', default="Mark Message"), error_text,
            action, on_success_extra=update_rows,
        )

    def _unlock_secret_store(self):
        """Asks for the master password when, and only when, the
        saved passwords cannot be read on this computer.

        Three outcomes, all of them final for this launch: nothing to
        do (the usual case, and silent); unlocked with the master
        password, which also records a DPAPI wrap so this computer
        never asks again; or no way in, in which case the accounts
        are told plainly that they need signing in to their servers
        again. See dpapi_secret_store for the shape of the store.
        """
        import dpapi_secret_store

        try:
            state = dpapi_secret_store.status(self.paths.config)
        except Exception:
            logging.getLogger("zbox.main_frame").exception(
                "Could not read the secret store's state."
            )
            return

        if not state["locked"]:
            return

        if state["master_available"]:
            from master_password_dialog import MasterPasswordPrompt

            while True:
                dialog = MasterPasswordPrompt(self)
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        break
                    entered = dialog.get_password()
                finally:
                    dialog.Destroy()

                try:
                    if dpapi_secret_store.unlock_with_master(
                        self.paths.config, entered
                    ):
                        return
                except dpapi_secret_store.WrongMasterPassword:
                    wx.MessageBox(
                        lang.t(
                            "errors", "unlock_password_wrong",
                            default="That master password is not correct.",
                        ),
                        lang.t(
                            "dialogs", "title_master_password",
                            default="Master Password",
                        ),
                        wx.OK | wx.ICON_ERROR, self,
                    )
                    continue
                except dpapi_secret_store.SecretStoreUnavailable as exc:
                    wx.MessageBox(
                        str(exc),
                        lang.t(
                            "dialogs", "title_master_password",
                            default="Master Password",
                        ),
                        wx.OK | wx.ICON_ERROR, self,
                    )
                    return
                break

        # One explanation, not two. When the account list failed to
        # open as well, __init__ (line 241) already says this and says
        # more with it: what is refused, and that nothing has been
        # lost. Repeating it here is a second modal dialog carrying no
        # new information, and on a screen reader that is a second full
        # announcement to sit through before the window appears
        # (open_issues V3).
        #
        # Not deleted outright. This still fires for the case that
        # message does not cover -- a readable account list with a
        # locked secret store -- where dropping it would replace a
        # warning with silence.
        if self.account_manager.load_error:
            return

        wx.MessageBox(
            lang.t(
                "dialogs", "accounts_locked",
                default="The saved account passwords were written on another "
                "computer, so they cannot be read here.\n\n"
                "Each account will need signing in to its mail server "
                "again. To avoid that next time, set a master password "
                "from Settings, App and Data Security on the computer "
                "where the accounts already work.",
            ),
            lang.t("dialogs", "title_accounts_locked", default="Accounts Locked"),
            wx.OK | wx.ICON_WARNING, self,
        )

    def _ensure_master_password_set(self, note):
        """Makes sure a master password exists before ZBox stores an
        account secret. Thin wrapper around
        master_password_dialog.ensure_master_password_set, which this
        frame's parentage is passed into -- see that function's own
        docstring for the full behaviour, now shared with the
        first-run EULA gate (ZBoxApp._show_eula_if_needed), which
        calls it the same way with parent=None before any frame
        exists.

        Returns True when a master password is in force.
        """
        from master_password_dialog import ensure_master_password_set

        return ensure_master_password_set(self, self.paths.config, note)

    def _on_master_password(self, event):
        """Settings > App and Data Security > Master Password. (The
        Tools > Master Password item it was originally written for is
        gone -- menu audit finding 17 -- but the handler stays, and is
        still reached from Settings.) The master password is what makes
        the folder portable: without one the saved passwords are
        readable only by this Windows account on this machine, which
        is deliberate -- copying the folder somewhere is then not
        enough to read anyone's mail."""
        import dpapi_secret_store
        from master_password_dialog import MasterPasswordDialog

        try:
            is_set = dpapi_secret_store.has_master_password(self.paths.config)
        except Exception:
            logging.getLogger("zbox.main_frame").exception(
                "Could not read the secret store's state."
            )
            is_set = False

        dialog = MasterPasswordDialog(self, is_set)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            current = dialog.get_current_password()
            new_password = dialog.get_new_password()
        finally:
            dialog.Destroy()

        try:
            if new_password:
                dpapi_secret_store.set_master_password(
                    self.paths.config, new_password, current
                )
                message = (
                    lang.t('dialogs', 'mp_set_done', default="Master password set. ZBox will ask for it once on "
                    "each new computer this folder is opened on, and "
                    "unlock normally there afterwards.")
                )
            elif is_set:
                confirm = wx.MessageBox(
                    lang.t(
                        "dialogs", "master_password_remove_confirm",
                        default="Remove the master password? The saved account passwords "
                        "will then only be readable on this computer and this "
                        "Windows account. Copy ZBox anywhere else afterward and "
                        "every account will need to be signed in again.\n\n"
                        "Remove it now?",
                    ),
                    lang.t(
                        "dialogs", "title_master_password",
                        default="Master Password",
                    ),
                    wx.YES_NO | wx.ICON_WARNING, self,
                )
                if confirm != wx.YES:
                    return
                if not dpapi_secret_store.clear_master_password(
                    self.paths.config, current
                ):
                    raise dpapi_secret_store.WrongMasterPassword(
                        "That master password is not correct."
                    )
                message = (
                    lang.t('dialogs', 'mp_removed_done', default="Master password removed. The saved passwords now "
                    "work only on this computer and this Windows "
                    "account.")
                )
            else:
                return
        except dpapi_secret_store.WrongMasterPassword as exc:
            wx.MessageBox(
                str(exc),
                lang.t("dialogs", "title_master_password", default="Master Password"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        except (dpapi_secret_store.SecretStoreUnavailable, ValueError) as exc:
            wx.MessageBox(
                str(exc),
                lang.t("dialogs", "title_master_password", default="Master Password"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        except Exception as exc:
            logging.getLogger("zbox.main_frame").exception(
                "Changing the master password failed."
            )
            wx.MessageBox(
                lang.t(
                    "errors", "master_password_not_changed",
                    default="The master password could not be changed: %s" % exc,
                    error=exc,
                ),
                lang.t("dialogs", "title_master_password", default="Master Password"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return

        wx.MessageBox(
            message,
            lang.t("dialogs", "title_master_password", default="Master Password"),
            wx.OK | wx.ICON_INFORMATION, self,
        )

    def _on_shortcuts(self, event):
        from shortcuts_dialog import ShortcutsDialog

        dialog = ShortcutsDialog(self)
        dialog.ShowModal()
        dialog.Destroy()


class ZBoxApp(wx.App):
    def __init__(self, paths, settings_manager, minimized=False):
        self.paths = paths
        self.settings_manager = settings_manager
        # The interface language, loaded once here rather than in
        # main.py. main.py reaches app modules as app.<name> while
        # every module here imports its siblings as plain <name>, and
        # those are two different modules to Python -- loading into
        # one would leave the other empty and nothing would ever be
        # translated. Before the frame is built, so every string the
        # menus and dialogs look up is already in the chosen language.
        # Returns the language actually in force, which is English
        # when the file is missing or unreadable.
        in_force = lang.load(
            paths.lang_dir, getattr(settings_manager.settings, "language", "en")
        )
        logging.getLogger("zbox.main").info("Interface language: %s.", in_force)
        # True for a Windows-startup launch (main.py's --minimized,
        # see app/startup_registration.py) -- the frame is built
        # (tray icon included) but never shown, so ZBox starts
        # straight into the tray rather than flashing its window open
        # on every sign-in.
        self.minimized = minimized
        # Set by _show_eula_if_needed only on a genuinely fresh
        # install (see its own docstring) -- OnInit reads this once,
        # after the main frame exists, to open the New Account
        # wizard automatically. Never set on a folder moved to
        # another computer with accounts already in it: that case
        # never reaches the branch that sets it.
        self._prompt_new_account = False
        super().__init__(redirect=False)

    def _show_eula_if_needed(self):
        """The first-run EULA gate. True means carry on starting
        ZBox; False means OnInit returns False and ZBox exits before
        MainLoop ever starts, without a window ever having been
        shown.

        Shown once: settings.eula_accepted, once True, is saved to
        settings.json and this returns True immediately on every
        later launch without showing anything.

        Runs before ZBoxMainFrame exists (like _unlock_app below), so
        EulaDialog's parent is None here -- the same pattern
        StartupUnlockDialog already uses for the same reason.

        On a genuinely fresh install -- neither accounts.json nor
        settings.json exists yet, checked directly here rather than
        inferred, since nothing has constructed AccountManager or
        written SettingsManager's own state to disk at this point --
        accepting also runs the master password setup immediately
        (rather than waiting for the first account to be added, as
        before this gate existed) and arms _prompt_new_account so
        OnInit opens the New Account wizard right after the window
        appears. A folder moved to another computer with accounts
        already in it fails this check and skips both: it already
        has accounts and goes through ZBoxMainFrame's own
        _unlock_secret_store prompt instead, unchanged.
        """
        settings = self.settings_manager.settings
        if getattr(settings, "eula_accepted", False):
            return True

        # The language page comes first, so the license page is
        # already in the chosen language. Cancel there is the same
        # choice as I Disagree here. The code is held, not saved:
        # saving now would create settings.json and make the
        # fresh-install check below, and the decline cleanup, both
        # believe this is an install that has run before.
        chosen_language = self._choose_language()
        if chosen_language is None:
            self._cleanup_declined_install()
            return False

        from eula_dialog import EulaDialog

        dialog = EulaDialog(None, self.paths.license_file, interactive=True)
        try:
            agreed = dialog.ShowModal() == wx.ID_OK
        finally:
            dialog.Destroy()

        if not agreed:
            self._cleanup_declined_install()
            return False

        # Captured before settings.eula_accepted is saved below: once
        # saved, settings.json exists, so this check has to reflect
        # what was true walking in, not after this method's own write.
        fresh_install = not (
            os.path.isfile(self.paths.accounts_file)
            or os.path.isfile(self.paths.settings_file)
        )

        settings.eula_accepted = True
        settings.language = chosen_language
        self.settings_manager.save()

        if fresh_install:
            from master_password_dialog import ensure_master_password_set

            ensure_master_password_set(
                None, self.paths.config,
                lang.t('dialogs', 'mp_prompt_first_run', default="ZBox is about to save your account passwords. Set a "
                "master password now so they can still be opened if "
                "you move ZBox to another computer."),
            )
            self._offer_desktop_shortcut()
            self._prompt_new_account = True

        return True

    def _choose_language(self):
        """The first-run language page (language_dialog). Returns the
        chosen code, already loaded, or None when the person cancelled.

        Skipped, returning the language already in force, when only
        one language is installed: a list of one is not a choice.
        Preselects the language matching Windows' display language,
        English when none does.
        """
        languages = lang.available(self.paths.lang_dir)
        current = getattr(self.settings_manager.settings, "language", lang.DEFAULT_CODE)
        if len(languages) <= 1:
            return current

        system_name = lang.system_locale_name()
        preselect = lang.match_system_language(
            [code for code, _name in languages], system_name,
        )
        logging.getLogger("zbox.main").info(
            "First-run language page: Windows display language %r, "
            "preselecting %s.", system_name, preselect,
        )

        from language_dialog import LanguageDialog

        dialog = LanguageDialog(None, self.paths.lang_dir, languages, preselect)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                lang.load(self.paths.lang_dir, current)
                return None
            chosen = dialog.selected_code()
        finally:
            dialog.Destroy()

        in_force = lang.load(self.paths.lang_dir, chosen)
        logging.getLogger("zbox.main").info("Interface language: %s.", in_force)
        return in_force

    def _offer_desktop_shortcut(self):
        """First-run desktop shortcut step, between the master
        password setup above and the New Account wizard OnInit
        opens. Asked here rather than from Settings' own entry so it
        lands once, on a fresh install, with no window yet built --
        hence parent None, like the EULA and master password dialogs
        either side of it.

        Never blocks the launch: declining, or a failure to write the
        file, carries straight on to the account wizard.
        """
        answer = wx.MessageBox(
            lang.t(
                "dialogs", "shortcut_offer",
                default="Put a ZBox shortcut on the Desktop?\n\n"
                "It will start this ZBox folder:\n%s\n\n"
                "A shortcut records that exact location. If you move the "
                "ZBox folder later, open Settings, System tray and "
                "window, and choose Create desktop shortcut again to "
                "point it at the new place." % self.paths.base,
                path=self.paths.base,
            ),
            lang.t("dialogs", "title_desktop_shortcut", default="Desktop Shortcut"),
            wx.YES_NO | wx.ICON_QUESTION,
        )
        if answer != wx.YES:
            return
        from desktop_shortcut import create_desktop_shortcut

        try:
            path = create_desktop_shortcut(self.paths.base)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("zbox.main_frame").exception(
                "Creating the first-run desktop shortcut failed."
            )
            wx.MessageBox(
                lang.t(
                    "errors", "shortcut_failed_startup",
                    default="The desktop shortcut could not be created:\n\n%s\n\n"
                    "ZBox will carry on starting. You can try again later "
                    "from Settings." % exc,
                    error=exc,
                ),
                lang.t("dialogs", "title_desktop_shortcut", default="Desktop Shortcut"),
                wx.OK | wx.ICON_WARNING,
            )
            return
        wx.MessageBox(
            lang.t(
                "dialogs", "shortcut_created",
                default="Shortcut created:\n%s" % path, path=path,
            ),
            lang.t("dialogs", "title_desktop_shortcut", default="Desktop Shortcut"),
            wx.OK | wx.ICON_INFORMATION,
        )

    def _cleanup_declined_install(self):
        """Removes the empty data\\ tree main.py creates on every
        launch before anything else runs, when I Disagree is chosen
        on a genuinely fresh install -- so declining leaves no trace
        of having run ZBox at all.

        Guarded on the same fresh-install check _show_eula_if_needed
        uses: refuses to delete anything if accounts.json or
        settings.json already exist, so this can never fire against
        a real, already-used install under any circumstance,
        including this method somehow being reached a second time.

        Best effort, not guaranteed complete: data\\logs\\zbox_debug.log
        is already open for writing by this same process's own
        logging setup (main.setup_logging, which runs before ZBoxApp
        is even constructed), and Windows will not let an open file
        be deleted out from under its own handle. shutil.rmtree's
        ignore_errors=True means that one file (and only that one)
        can be left behind rather than this raising.
        """
        if os.path.isfile(self.paths.accounts_file) or os.path.isfile(
            self.paths.settings_file
        ):
            logging.getLogger("zbox.main_frame").warning(
                "EULA declined but accounts.json or settings.json "
                "already exists; leaving data\\ untouched."
            )
            return
        import shutil

        try:
            shutil.rmtree(self.paths.data, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    def _unlock_app(self):
        """The app lock, run before the main window is built.

        Deliberately here rather than in ZBoxMainFrame.__init__: this
        is the one place a refusal can simply stop the launch, by
        returning False from OnInit, instead of half-constructing a
        frame and then trying to unwind it. Nothing has touched an
        account at this point.

        Off unless "Lock ZBox when it is fully quit" is on in
        Settings > App and Data Security. Any one of the registered
        methods answers it -- passkey, security key, or master
        password -- see unlock_dialog.unlock_at_startup.

        A failure inside the lock itself starts ZBox unlocked and
        says so in the log. That is the deliberate choice between two
        bad outcomes: a bug in the lock leaving someone permanently
        unable to open their own mail is worse than a launch that
        skipped a gate, and this is a gate, not the encryption -- the
        secret store is protected either way (see dpapi_secret_store).
        """
        settings = getattr(self.settings_manager, "settings", None)
        if not getattr(settings, "lock_on_full_quit", False):
            return True
        try:
            from unlock_dialog import unlock_at_startup

            return unlock_at_startup(self.paths)
        except Exception:  # noqa: BLE001
            logging.getLogger("zbox.main_frame").exception(
                "The app lock could not run; ZBox is starting unlocked."
            )
            return True

    def OnInit(self):
        # First of all, before any window: check boxes, radio buttons
        # and toggle buttons report their checked state to screen
        # readers even when the dark theme owner-draws them. See
        # accessible.install_toggle_states.
        try:
            import accessible as _accessible
            _accessible.install_toggle_states()
        except Exception:  # noqa: BLE001 -- must never stop startup
            logging.getLogger("zbox.main").warning(
                "Could not install toggle-state accessibility.", exc_info=True,
            )
        # OK, Cancel and the other standard buttons take wx's built-in
        # English labels; this relabels them in the chosen language
        # just before each dialog shows. See accessible.install_stock_labels.
        try:
            import accessible as _accessible
            _accessible.install_stock_labels()
        except Exception:  # noqa: BLE001 -- must never stop startup
            logging.getLogger("zbox.main").warning(
                "Could not install translated stock button labels.", exc_info=True,
            )
        # Before any window, including the license and unlock
        # dialogs: Windows only allows dark mode to be switched on
        # before the first window exists. See theme.py.
        try:
            import theme

            theme.apply_theme(
                self, getattr(self.settings_manager.settings, "ui_theme", "system"),
            )
        except Exception:  # noqa: BLE001 -- a theme must never stop ZBox starting
            logging.getLogger("zbox.theme").warning(
                "Interface theme not applied.", exc_info=True,
            )
        if not self._show_eula_if_needed():
            return False
        if not self._unlock_app():
            return False
        frame = ZBoxMainFrame(self.paths, self.settings_manager)
        # Deliberately not Iconize(True): that still puts a button on
        # the taskbar. Simply never calling Show() leaves the frame
        # exactly as invisible-but-alive as close_to_tray's own
        # Hide() -- reachable only through the tray icon, which
        # restore_from_tray() already knows how to un-hide from
        # either state.
        if not self.minimized:
            frame.Show()
        if self._prompt_new_account:
            # Deferred the same way ZBoxMainFrame.__init__ itself
            # defers initial list focus: runs once the window is
            # actually up, opening the exact same New Account wizard
            # Tools > Account Settings would (_on_new_account), so a
            # genuinely fresh install goes EULA -> master password ->
            # add your first account with no separate code path to
            # keep in sync.
            wx.CallAfter(frame._on_new_account, None)
        self.SetTopWindow(frame)
        return True
