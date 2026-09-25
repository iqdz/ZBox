"""
Search tab (audit finding 39 -- "the biggest single gap"). Opened
with Ctrl+Shift+F or Message > Search Messages. Own tab rather than a
dialog, so results stay on screen alongside whatever else is open,
matching the mail tab's own model.

Kept deliberately simple for a first version: an account choice
(every configured account, plus "All Accounts", which is the
default), a folder choice (the six generic folder types ZBox already
uses everywhere, plus "All Folders", also the default), a search
field, and a results list that reuses EnvelopeListPanel exactly as
the Mail tab does. Pressing Enter in the search field runs the
search, same as selecting the Search button (wx.TE_PROCESS_ENTER on
the field). A completed search either moves focus onto the first
result (so a screen reader lands somewhere useful and hears it
immediately, the way focus normally announces itself) or, when
nothing matched, announces "None found" instead -- there being
nothing in an empty list for focus to land on and be heard from,
same reasoning as announce.py generally. Opening a
result (Enter or double-click) opens it in a normal message tab via
main_frame._open_search_result -- from there every single-message
action (reply, forward, delete, archive, mark read/unread, flag,
print, save as) already works unchanged, because those all key off
the OPEN TAB's own stored (account, folder, envelope), not off
whichever list or tree selection is currently visible (see
main_frame._current_message_context and _current_message_tab).
Multi-select bulk actions (Archive/Move/Copy/flag/mark-read on
several search results at once) are NOT wired up here -- those all
resolve their targets from the Mail tab's own tree selection
(_resolve_envelope_context), which this tab deliberately never
touches, since a search result can come from any folder and the tree
has no matching selection at all. Open each result and act on it
from its own tab instead; multi-select-from-search is a possible
follow-up, not implemented.
"""

import logging

import wx

import lang
from accessible import apply_content_background, wrap_text
from envelope_format import (
    _accounts_for_search_selection,
    _folder_display_to_himalaya,
    _resolve_search_folders,
)
from envelope_list_panel import EnvelopeListPanel
from settings_manager import FOLDER_TYPES
import himalaya_client

logger = logging.getLogger("zbox.search")

ALL_ACCOUNTS_LABEL = "All Accounts"
ALL_FOLDERS_LABEL = "All Folders"


def _all_accounts_label():
    """The "All Accounts" entry as shown, in the chosen language."""
    return lang.t("dialogs", "srch_all_accounts", default=ALL_ACCOUNTS_LABEL)


def _all_folders_label():
    """The "All Folders" entry as shown and compared."""
    return lang.t("dialogs", "srch_all_folders", default=ALL_FOLDERS_LABEL)


class SearchTabPanel(wx.Panel):
    def __init__(self, parent, main_frame):
        super().__init__(parent)
        self.main_frame = main_frame
        apply_content_background(self)
        self._search_id = 0

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "srch_note_intro",
                default=(
                    "Search From, Subject and Body for text you enter below. "
                    "Searches every account and folder unless you narrow it "
                    "below."
                ),
            ),
        )
        wrap_text(intro)
        sizer.Add(intro, 0, wx.ALL, 8)

        form = wx.BoxSizer(wx.HORIZONTAL)

        account_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_account", default="Account")
        )
        self.account_choice = wx.Choice(self, choices=self._account_labels())
        self.account_choice.SetName(
            lang.t("dialogs", "srch_account", default="Account")
        )
        if self.account_choice.GetCount() > 0:
            self.account_choice.SetSelection(0)

        folder_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_folder", default="Folder")
        )
        self.folder_choice = wx.Choice(self, choices=[*FOLDER_TYPES, _all_folders_label()])
        self.folder_choice.SetName(
            lang.t("dialogs", "srch_folder", default="Folder")
        )
        self.folder_choice.SetStringSelection(_all_folders_label())

        search_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_for", default="Search for")
        )
        self.search_field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.search_field.SetName(
            lang.t("dialogs", "srch_for", default="Search for")
        )
        self.search_field.Bind(wx.EVT_TEXT_ENTER, self._on_search)

        self.search_button = wx.Button(
            self, label=lang.t("dialogs", "title_search", default="Search")
        )
        self.search_button.Bind(wx.EVT_BUTTON, self._on_search)

        form.Add(account_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        form.Add(self.account_choice, 0, wx.RIGHT, 12)
        form.Add(folder_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        form.Add(self.folder_choice, 0, wx.RIGHT, 12)
        form.Add(search_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        form.Add(self.search_field, 1, wx.RIGHT, 8)
        form.Add(self.search_button, 0)

        sizer.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.status_label = wx.StaticText(self, label="")
        sizer.Add(self.status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.results_panel = EnvelopeListPanel(
            self,
            context_menu_handler=lambda *a: None,
            selection_handler=lambda envelope: None,
            activation_handler=self._on_result_activated,
        )
        self.results_panel.show_placeholder(lang.t(
            "main_ui", "search_placeholder",
            default="Type something above and press Enter or select Search.",
        ))
        sizer.Add(self.results_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)
        wx.CallAfter(self.search_field.SetFocus)

    def _account_labels(self):
        return [_all_accounts_label()] + [
            account.display_name for account in self.main_frame.account_manager.accounts
        ]

    def _selected_accounts(self):
        """Every account this search should run against -- see
        envelope_format._accounts_for_search_selection for the index
        arithmetic ("All Accounts" is a synthetic entry at index 0)."""
        return _accounts_for_search_selection(
            self.main_frame.account_manager.accounts, self.account_choice.GetSelection(),
        )

    def refresh_accounts(self):
        """Called by main_frame after accounts change (add/remove) so
        a search tab left open doesn't keep offering a stale list."""
        previous = self.account_choice.GetStringSelection()
        self.account_choice.SetItems(self._account_labels())
        if previous and self.account_choice.SetStringSelection(previous):
            return
        if self.account_choice.GetCount() > 0:
            self.account_choice.SetSelection(0)

    def focus_search_field(self):
        self.search_field.SetFocus()
        self.search_field.SelectAll()

    def focus_default(self):
        """Where keyboard focus should land when the notebook returns
        to this tab (main_frame._close_tab_at) -- the results, not the
        search field, since closing a result means coming back to the
        list you opened it from."""
        if self.results_panel.has_content():
            self.results_panel.list_ctrl.SetFocus()
        else:
            self.focus_search_field()

    def _on_search(self, event):
        accounts = [
            account for account in self._selected_accounts()
            if getattr(account, "enabled", True)
        ]
        if not accounts:
            self.status_label.SetLabel(lang.t(
                "main_ui", "search_no_account", default="Add an account first."
            ))
            return

        term = self.search_field.GetValue().strip()
        if not term:
            self.status_label.SetLabel(lang.t(
                "main_ui", "search_no_term",
                default="Type something to search for.",
            ))
            return

        scope = self.folder_choice.GetStringSelection()

        self._search_id += 1
        search_id = self._search_id
        self.status_label.SetLabel(lang.t(
            "main_ui", "search_running", default="Searching..."
        ))
        self.search_button.Disable()
        self.main_frame._play_sound("searching")

        def work():
            merged = []
            for account in accounts:
                if scope == _all_folders_label():
                    # Resolved through _resolve_search_folders, not
                    # used as-is -- see its docstring for the Gmail
                    # bug this fixes.
                    folders = _resolve_search_folders(
                        self.main_frame._folders_for_account(account), account,
                    )
                else:
                    folders = [_folder_display_to_himalaya(scope, account)]
                for folder in folders:
                    try:
                        envelopes = himalaya_client.search_envelopes(
                            self.main_frame.paths, account, term, folder,
                        )
                    except himalaya_client.HimalayaError as exc:
                        if himalaya_client.is_missing_folder_error(exc):
                            # Expected, not a failure: this account's
                            # server never provisioned one of ZBox's
                            # generic folders (e.g. disroot.org has no
                            # Archive mailbox at all). Search across
                            # every account/folder combination for a
                            # multi-account, multi-folder search, so
                            # simply contributing zero results for
                            # this one is correct -- it matches what
                            # mail_fetch.py now tells the person
                            # directly when they open that folder tab.
                            logger.debug(
                                "Search skipped %s/%s: no such folder for this account.",
                                account.account_id, folder,
                            )
                        else:
                            logger.warning(
                                "Search failed in %s/%s: %s", account.account_id, folder, exc,
                            )
                        continue
                    for envelope in envelopes:
                        envelope["_zbox_account_id"] = account.account_id
                        envelope["_zbox_folder"] = folder
                        merged.append(envelope)
            return merged

        def on_success(envelopes):
            # Guarded like compose_panel/message_view_panel/
            # bulk_progress_dialog already guard their own delayed UI
            # updates: a search can take many seconds across several
            # accounts and folders, and if the user closes this
            # Search tab (Ctrl+W etc.) before it finishes, every
            # control below is destroyed C++-side by the time this
            # callback runs. Touching any of them then raises
            # RuntimeError, not a normal Python error -- confirmed via
            # zbox_debug.log, where this crashed on the very first
            # line (self.search_button.Enable()) and, because that
            # aborted the callback immediately, a search that had in
            # fact found real matches never got to show them: no
            # results, no "None found" announcement, focus left
            # sitting wherever it already was (the search field).
            try:
                self.search_button.Enable()
                if search_id != self._search_id:
                    return  # a newer search superseded this one
                self.results_panel.populate(envelopes)
                count = len(envelopes)
                if count == 0:
                    # populate([]) already fell back to its own generic
                    # "No messages in this folder." placeholder -- replace
                    # it with one that actually reflects a search, then
                    # announce it: with nothing in the list to land focus
                    # on and be read by landing there, a search that finds
                    # nothing needs its own way to be heard, same reason
                    # announce.py exists at all.
                    message = lang.t(
                        "main_ui", "search_none",
                        default=f'No matches for "{term}".', term=term,
                    )
                    self.status_label.SetLabel(message)
                    self.results_panel.show_placeholder(message)
                    self.main_frame._play_sound("none_found")
                    self.main_frame.announce(
                        lang.t("actions_announcements", "none_found",
                               default="None found."),
                        title=lang.t("dialogs", "title_search", default="Search"),
                    )
                else:
                    self.status_label.SetLabel(
                        lang.t(
                            "main_ui", "search_found_one",
                            default=f'1 match for "{term}".', term=term,
                        )
                        if count == 1
                        else lang.t(
                            "main_ui", "search_found_many",
                            default=f'{count} matches for "{term}".',
                            count=count, term=term,
                        )
                    )
                    # Landing focus on the first result both puts the
                    # reader where they'll act next and is itself the
                    # announcement -- a screen reader speaks whatever a
                    # newly focused control says the moment it gets focus.
                    self.results_panel.focus_top_message()
                    self.main_frame._play_sound("searching_done")
            except RuntimeError:
                logger.debug(
                    "Search on_success: tab closed before results arrived."
                )

        def on_error(exc):
            # Same guard as on_success -- see its comment.
            try:
                self.search_button.Enable()
                if search_id != self._search_id:
                    return
                self.status_label.SetLabel(lang.t(
                    "main_ui", "search_failed", default="Search failed."
                ))
                wx.MessageBox(
                    lang.t(
                        "errors", "search_failed",
                        default=f"Search failed.\n\n{exc}", error=exc,
                    ),
                    lang.t("dialogs", "title_search", default="Search"),
                    wx.OK | wx.ICON_ERROR,
                )
            except RuntimeError:
                logger.debug(
                    "Search on_error: tab closed before failure was reported."
                )

        # One account: its own lane. Several: the shared worker.
        self.main_frame._lane_for_accounts(
            account.account_id for account in accounts
        ).submit(work, on_success, on_error)

    def _on_result_activated(self, envelope):
        self.main_frame._open_search_result(envelope)
