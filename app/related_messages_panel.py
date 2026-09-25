"""
Audit finding 44's minimum bar: "Conversation or threading view, or
at minimum a 'show related messages' action driven by the References
header." A full automatic threading view groups every message in a
mailbox into conversations; this is the manual, per-message
alternative instead -- invoked from an open message (Ctrl+Shift+T /
Message > Show Related Messages), it finds every other message in
the same account that shares a real RFC 5322 threading relationship
with it, across every folder in that account (not just the one it's
open in -- a reply typically lives in Sent while the original sits
in Inbox, so a single-folder search would miss the other half of
almost every conversation; a thread never crosses accounts, so the
scope stops there).

Modeled closely on search_panel.SearchTabPanel: its own tab, reusing
EnvelopeListPanel for the results and main_frame._open_search_result
to open one (same envelope shape -- _zbox_account_id/_zbox_folder
stamped on each result). Differences: there is no query form -- the
"search term" is the open message's own threading headers, already
known when the tab is created -- and the search starts immediately
rather than waiting on Enter/a button.

Relatedness is decided by message_body.thread_ids: a candidate is
related iff its own thread-id set intersects the open message's (see
that function's docstring for why one set intersection covers
parent, child and sibling messages alike). That needs each
candidate's full headers, which costs a Himalaya subprocess per
candidate -- too expensive to pay for every message in every folder
unconditionally. Candidates are pre-filtered by normalized subject
(message_body.normalized_subject) first, so only a same-subject
candidate's headers are ever fetched. This is a real tradeoff, not a
hidden shortcut: a reply whose subject was deliberately changed will
be missed. It also means the ordering matters -- subject narrows the
candidate list, References verifies it -- so this action never
reports two merely same-subject messages as related.

Runs on main_frame.sync_worker, not the interactive worker: a search
here can mean a Himalaya subprocess per folder plus one per
same-subject candidate, the same "background/multi-message loop"
shape as Empty Trash and offline sync, not a single interactive
action.
"""

import logging

import wx

import lang
from accessible import apply_content_background, wrap_text
from envelope_format import _envelope_backend, _resolve_search_folders
from envelope_list_panel import EnvelopeListPanel
import himalaya_client
import message_body

logger = logging.getLogger("zbox.related")


class RelatedMessagesTabPanel(wx.Panel):
    def __init__(self, parent, main_frame, account, folder, envelope, thread_ids):
        super().__init__(parent)
        self.main_frame = main_frame
        self.account = account
        self.folder = folder
        self.envelope = envelope
        self.thread_ids = thread_ids
        apply_content_background(self)

        sizer = wx.BoxSizer(wx.VERTICAL)

        subject = envelope.get("subject") or "(no subject)"
        intro = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "rel_note_intro",
                default=(
                    'Messages related to "%s" by their Message-ID/References '
                    "headers, across every folder in this account." % subject
                ),
                subject=subject,
            ),
        )
        wrap_text(intro)
        sizer.Add(intro, 0, wx.ALL, 8)

        self.status_label = wx.StaticText(
            self,
            label=lang.t("main_ui", "related_running", default="Searching..."),
        )
        sizer.Add(self.status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.results_panel = EnvelopeListPanel(
            self,
            context_menu_handler=lambda *a: None,
            selection_handler=lambda envelope: None,
            activation_handler=self._on_result_activated,
        )
        self.results_panel.show_placeholder(
            lang.t("main_ui", "related_running", default="Searching...")
        )
        sizer.Add(self.results_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)
        self.main_frame._play_sound("searching")
        wx.CallAfter(self._run_search)

    def focus_default(self):
        """Where focus lands when the notebook returns to this tab
        (main_frame._close_tab_at): the related-message list, which is
        what the reader was in before opening one of its rows."""
        self.results_panel.list_ctrl.SetFocus()

    def _run_search(self):
        account = self.account
        base_folder = self.folder
        base_id = self.envelope.get("id")
        base_backend = _envelope_backend(self.envelope)
        thread_ids = self.thread_ids
        base_subject_key = message_body.normalized_subject(self.envelope.get("subject"))
        paths = self.main_frame.paths

        def work():
            matches = []
            folders = _resolve_search_folders(
                self.main_frame._folders_for_account(account), account,
            )
            for folder in folders:
                try:
                    envelopes = himalaya_client.list_envelopes(
                        paths, account, folder, backend=base_backend,
                    )
                except himalaya_client.HimalayaError as exc:
                    if himalaya_client.is_missing_folder_error(exc):
                        # Expected -- this account's server never
                        # provisioned one of ZBox's generic folders.
                        # Same reasoning as search_panel's identical
                        # guard: contributing zero matches for this
                        # one folder is correct, not a failure.
                        logger.debug(
                            "Related messages: no %s folder for %s.",
                            folder, account.account_id,
                        )
                    else:
                        logger.warning(
                            "Related messages: could not list %s/%s: %s",
                            account.account_id, folder, exc,
                        )
                    continue
                for candidate in envelopes:
                    candidate_id = candidate.get("id")
                    if candidate_id is None:
                        continue
                    if folder == base_folder and candidate_id == base_id:
                        continue  # the open message itself
                    if message_body.normalized_subject(candidate.get("subject")) != base_subject_key:
                        continue
                    candidate_backend = _envelope_backend(candidate)
                    candidate_message = himalaya_client.read_message_cached_only(
                        paths, account, candidate_id, folder=folder, backend=candidate_backend,
                    )
                    if candidate_message is None:
                        try:
                            candidate_message = himalaya_client.read_message(
                                paths, account, candidate_id, folder=folder,
                                backend=candidate_backend, timeout=30,
                            )
                        except himalaya_client.HimalayaError as exc:
                            # One unreadable/slow candidate must never
                            # abort the rest of the search -- same
                            # reasoning as prefetch_messages.
                            logger.debug(
                                "Related messages: could not read candidate %s/%s: %s",
                                folder, candidate_id, exc,
                            )
                            continue
                    if not thread_ids.isdisjoint(message_body.thread_ids(candidate_message)):
                        candidate["_zbox_account_id"] = account.account_id
                        candidate["_zbox_folder"] = folder
                        matches.append(candidate)
            return matches

        def on_success(matches):
            # Guarded like search_panel's own on_success: the tab can
            # be closed (Ctrl+W) while this runs in the background,
            # which destroys every control below C++-side.
            try:
                self.results_panel.populate(matches)
                count = len(matches)
                if count == 0:
                    message = lang.t(
                        "main_ui", "related_none",
                        default="No related messages found.",
                    )
                    self.status_label.SetLabel(message)
                    self.results_panel.show_placeholder(message)
                    self.main_frame._play_sound("none_found")
                    self.main_frame.announce(
                        lang.t("actions_announcements", "none_found",
                               default="None found."),
                        title=lang.t(
                            "dialogs", "title_related_messages",
                            default="Related Messages",
                        ),
                    )
                else:
                    self.status_label.SetLabel(
                        lang.t(
                            "main_ui", "related_found_one",
                            default="1 related message found.",
                        )
                        if count == 1
                        else lang.t(
                            "main_ui", "related_found_many",
                            default="%d related messages found." % count,
                            count=count,
                        )
                    )
                    self.results_panel.focus_top_message()
                    self.main_frame._play_sound("searching_done")
            except RuntimeError:
                logger.debug("Related messages: tab closed before results arrived.")

        def on_error(exc):
            try:
                self.status_label.SetLabel(lang.t(
                    "main_ui", "related_failed",
                    default="Could not search for related messages.",
                ))
                wx.MessageBox(
                    lang.t(
                        "errors", "related_search_failed",
                        default="Could not search for related messages.\n\n%s" % exc,
                        error=exc,
                    ),
                    lang.t(
                        "dialogs", "title_related_messages",
                        default="Related Messages",
                    ),
                    wx.OK | wx.ICON_ERROR,
                )
            except RuntimeError:
                logger.debug("Related messages: tab closed before failure was reported.")

        self.main_frame.sync_worker.submit(work, on_success, on_error)

    def _on_result_activated(self, envelope):
        self.main_frame._open_search_result(envelope)
