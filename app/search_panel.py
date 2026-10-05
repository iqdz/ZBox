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
The results list has the Mail tab list's context menu, single keys
and bulk actions (main_frame._action_list, _show_list_context_menu,
_on_list_delete_key). Each result carries its own account and folder
(_zbox_account_id and _zbox_folder), which
main_frame._resolve_envelope_context uses instead of the folder tree.
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
import imap_body_fetch
import search_filters

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
                    "Search for words in every account and folder, or narrow "
                    "the search with the choices below. Leave the words empty "
                    "to find messages by date or by the check boxes alone."
                ),
            ),
        )
        wrap_text(intro)
        sizer.Add(intro, 0, wx.ALL, 8)

        # The search form, in tab order: words, where to look, account,
        # folder, date range, three filters, then Search. Each label is
        # made just before its control, and also names it.
        form = wx.FlexGridSizer(0, 2, 6, 8)
        form.AddGrowableCol(1, 1)

        search_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_for", default="Search for")
        )
        self.search_field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.search_field.SetName(
            lang.t("dialogs", "srch_for", default="Search for")
        )
        self.search_field.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        form.Add(search_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.search_field, 1, wx.EXPAND)

        look_in_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_look_in", default="Look in")
        )
        self.look_in_choice = wx.Choice(self, choices=[
            lang.t("dialogs", "srch_in_all", default="All"),
            lang.t("dialogs", "srch_in_subject", default="Subject"),
            lang.t("dialogs", "srch_in_from", default="From"),
            lang.t("dialogs", "srch_in_to", default="To"),
            lang.t("dialogs", "srch_in_body", default="Body"),
        ])
        self.look_in_choice.SetName(
            lang.t("dialogs", "srch_look_in", default="Look in")
        )
        self.look_in_choice.SetSelection(0)
        form.Add(look_in_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.look_in_choice, 0)

        account_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_account", default="Account")
        )
        self.account_choice = wx.Choice(self, choices=self._account_labels())
        self.account_choice.SetName(
            lang.t("dialogs", "srch_account", default="Account")
        )
        if self.account_choice.GetCount() > 0:
            self.account_choice.SetSelection(0)
        self.account_choice.Bind(wx.EVT_CHOICE, self._on_account_choice)
        form.Add(account_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.account_choice, 0)

        folder_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_folder", default="Folder")
        )
        self.folder_choice = wx.Choice(self, choices=self._folder_labels())
        self.folder_choice.SetName(
            lang.t("dialogs", "srch_folder", default="Folder")
        )
        self.folder_choice.SetStringSelection(_all_folders_label())
        form.Add(folder_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.folder_choice, 0)

        date_label = wx.StaticText(
            self, label=lang.t("dialogs", "srch_date", default="Date")
        )
        self.date_choice = wx.Choice(self, choices=[
            lang.t("dialogs", "srch_date_any", default="Any time"),
            lang.t("dialogs", "srch_date_today", default="Today"),
            lang.t("dialogs", "srch_date_yesterday", default="Yesterday"),
            lang.t("dialogs", "srch_date_week", default="Last 7 days"),
            lang.t("dialogs", "srch_date_month", default="Last 30 days"),
            lang.t("dialogs", "srch_date_year", default="This year"),
            lang.t("dialogs", "srch_date_custom", default="Custom range"),
        ])
        self.date_choice.SetName(
            lang.t("dialogs", "srch_date", default="Date")
        )
        self.date_choice.SetSelection(0)
        self.date_choice.Bind(wx.EVT_CHOICE, self._on_date_choice)
        form.Add(date_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.date_choice, 0)

        from_date_label = wx.StaticText(
            self, label=lang.t(
                "dialogs", "srch_from_date",
                default="From date (year-month-day, optional time)",
            )
        )
        self.from_date_field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.from_date_field.SetName(lang.t(
            "dialogs", "srch_from_date",
            default="From date (year-month-day, optional time)",
        ))
        self.from_date_field.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        form.Add(from_date_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.from_date_field, 0)

        to_date_label = wx.StaticText(
            self, label=lang.t(
                "dialogs", "srch_to_date",
                default="To date (year-month-day, optional time)",
            )
        )
        self.to_date_field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.to_date_field.SetName(lang.t(
            "dialogs", "srch_to_date",
            default="To date (year-month-day, optional time)",
        ))
        self.to_date_field.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        form.Add(to_date_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.to_date_field, 0)
        # Only for Custom range. A disabled field is left out of the tab
        # order, so Tab goes from Date straight to Unread only.
        self.from_date_field.Disable()
        self.to_date_field.Disable()

        sizer.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        filters = wx.BoxSizer(wx.HORIZONTAL)
        self.unread_box = wx.CheckBox(
            self, label=lang.t("dialogs", "srch_unread", default="Unread only")
        )
        self.flagged_box = wx.CheckBox(
            self, label=lang.t("dialogs", "srch_flagged", default="Flagged only")
        )
        self.attachment_box = wx.CheckBox(
            self, label=lang.t("dialogs", "srch_attachment", default="Has attachment")
        )
        self.search_button = wx.Button(
            self, label=lang.t("dialogs", "title_search", default="Search")
        )
        self.search_button.Bind(wx.EVT_BUTTON, self._on_search)
        for box in (self.unread_box, self.flagged_box, self.attachment_box):
            filters.Add(box, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        filters.Add(self.search_button, 0)
        sizer.Add(filters, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.status_label = wx.StaticText(self, label="")
        sizer.Add(self.status_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # The Mail tab list's actions: its context menu, its single keys
        # and the spoken count of a multiple selection. Each action finds
        # the result's own account and folder
        # (main_frame._resolve_envelope_context).
        self.results_panel = EnvelopeListPanel(
            self,
            context_menu_handler=main_frame._show_list_context_menu,
            selection_handler=lambda envelope: None,
            activation_handler=self._on_result_activated,
            key_handler=main_frame._on_list_delete_key,
            selection_announce_handler=main_frame._on_selection_announce,
            bare_key_shortcuts_enabled=(
                lambda: main_frame.settings_manager.settings.bare_key_shortcuts_enabled
            ),
        )
        self.results_panel.show_placeholder(lang.t(
            "main_ui", "search_placeholder",
            default="Type something above and press Enter or select Search.",
        ))
        sizer.Add(self.results_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_form_key)
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
        if not (previous and self.account_choice.SetStringSelection(previous)):
            if self.account_choice.GetCount() > 0:
                self.account_choice.SetSelection(0)
        self._refresh_folders()

    def _folder_labels(self):
        """The Folder choice: the folder types and All Folders, then, with
        one account chosen, that account's other folders by their real
        names, as far as ZBox knows them (main_frame._folders_for_account
        fetches the list in the background the first time)."""
        labels = [*FOLDER_TYPES, _all_folders_label()]
        if self.account_choice.GetSelection() <= 0:
            return labels
        accounts = self._selected_accounts()
        if len(accounts) != 1:
            return labels
        known = {name.lower() for name in FOLDER_TYPES}
        for name in self.main_frame._folders_for_account(accounts[0]) or []:
            if name and name.lower() not in known:
                known.add(name.lower())
                labels.append(name)
        return labels

    def _refresh_folders(self):
        """Rebuilds the Folder choice for the chosen account, keeping the
        folder chosen when it is still offered, else All Folders."""
        previous = self.folder_choice.GetStringSelection()
        self.folder_choice.SetItems(self._folder_labels())
        if not (previous and self.folder_choice.SetStringSelection(previous)):
            self.folder_choice.SetStringSelection(_all_folders_label())

    def _on_account_choice(self, event):
        self._refresh_folders()
        event.Skip()

    def _on_date_choice(self, event):
        """From date and To date are usable only for Custom range."""
        index = max(0, self.date_choice.GetSelection())
        custom = search_filters.DATE_RANGES[index] == "custom"
        self.from_date_field.Enable(custom)
        self.to_date_field.Enable(custom)
        event.Skip()

    def _form_controls(self):
        return (
            self.search_field, self.look_in_choice, self.account_choice,
            self.folder_choice, self.date_choice, self.from_date_field,
            self.to_date_field, self.unread_box, self.flagged_box,
            self.attachment_box,
        )

    def _on_form_key(self, event):
        """Enter runs the search from any control of the form, as it does
        in the Search for field. Elsewhere (the results, the Search
        button) Enter does what it always did."""
        if (event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and not event.HasAnyModifiers()
                and wx.Window.FindFocus() in self._form_controls()):
            self._on_search(None)
            return
        event.Skip()

    def _date_problem(self, field, text):
        """A date that cannot be read: said in a notice, which never takes
        focus, and the cursor put in the field to correct it."""
        import notice_toast

        notice_toast.notify(
            self, text, lang.t("dialogs", "title_search", default="Search"),
        )
        field.SetFocus()
        field.SelectAll()

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
        look_in = search_filters.LOOK_IN[max(0, self.look_in_choice.GetSelection())]
        fields = search_filters.FIELDS[look_in]
        unread = self.unread_box.GetValue()
        flagged = self.flagged_box.GetValue()
        attachment = self.attachment_box.GetValue()
        date_key = search_filters.DATE_RANGES[max(0, self.date_choice.GetSelection())]
        if date_key == "custom":
            bad_date = lang.t(
                "main_ui", "search_bad_date",
                default=(
                    "Type the date as year-month-day, for example 2025-12-31, "
                    "with an optional time such as 14:30."
                ),
            )
            try:
                start = search_filters.parse_when(self.from_date_field.GetValue())
            except ValueError:
                self._date_problem(self.from_date_field, bad_date)
                return
            try:
                end = search_filters.parse_when(self.to_date_field.GetValue(), end=True)
            except ValueError:
                self._date_problem(self.to_date_field, bad_date)
                return
            if start is not None and end is not None and end <= start:
                self._date_problem(self.to_date_field, lang.t(
                    "main_ui", "search_bad_range",
                    default="The end of the date range is before its start.",
                ))
                return
        else:
            start, end = search_filters.preset_range(date_key)
        if not term and not (unread or flagged or attachment) and start is None and end is None:
            self.status_label.SetLabel(lang.t(
                "main_ui", "search_no_term",
                default="Type words to search for, or choose a date or a check box.",
            ))
            return
        # The server is asked for whole days, a little wider than the
        # range; the exact range, times included, is checked below.
        after, not_after = search_filters.server_dates(start, end)

        scope = self.folder_choice.GetStringSelection()
        paths = self.main_frame.paths

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
                elif scope in FOLDER_TYPES:
                    folders = [_folder_display_to_himalaya(scope, account)]
                else:
                    # A folder of the one chosen account, offered by its
                    # real name, which is already the server's name.
                    folders = [scope]
                for folder in folders:
                    try:
                        envelopes = himalaya_client.search_envelopes(
                            paths, account, term, folder,
                            fields=fields, unread=unread, flagged=flagged,
                            after=after, not_after=not_after,
                        )
                    except himalaya_client.HimalayaError as exc:
                        if himalaya_client.is_missing_folder_error(exc):
                            # Expected, not a failure: this account's
                            # server never provisioned one of ZBox's
                            # generic folders (e.g. disroot.org has no
                            # Archive mailbox at all). Searching every
                            # account and folder, this one simply
                            # contributes zero results.
                            logger.debug(
                                "Search skipped %s/%s: no such folder for this account.",
                                account.account_id, folder,
                            )
                        else:
                            logger.warning(
                                "Search failed in %s/%s: %s", account.account_id, folder, exc,
                            )
                        continue
                    envelopes = [
                        envelope for envelope in envelopes
                        if search_filters.in_range(envelope, start, end)
                    ]
                    if attachment and envelopes:
                        # No server search exists for attachments: each
                        # result's structure is read over ZBox's own
                        # connection instead.
                        try:
                            hits = set(imap_body_fetch.attachment_uids(
                                paths, account, folder,
                                [envelope.get("id") for envelope in envelopes],
                            ))
                        except imap_body_fetch.ImapBodyFetchUnavailable as exc:
                            logger.warning(
                                "Search could not check attachments in %s/%s: %s",
                                account.account_id, folder, exc,
                            )
                            continue
                        envelopes = [
                            envelope for envelope in envelopes
                            if search_filters.uid_of(envelope) in hits
                        ]
                        for envelope in envelopes:
                            envelope["has-attachment"] = True
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
                title = lang.t("dialogs", "title_search", default="Search")
                if count == 0:
                    # populate([]) already fell back to its own generic
                    # "No messages in this folder." placeholder -- replace
                    # it with one that reflects the search, then announce
                    # it: with nothing in the list to land focus on, a
                    # search that finds nothing needs its own way to be
                    # heard.
                    if term:
                        message = lang.t(
                            "main_ui", "search_none",
                            default=f'No matches for "{term}".', term=term,
                        )
                    else:
                        message = lang.t(
                            "main_ui", "search_none_any", default="No messages match.",
                        )
                    self.status_label.SetLabel(message)
                    self.results_panel.show_placeholder(message)
                    self.main_frame._play_sound("none_found")
                    self.main_frame.announce(
                        lang.t("actions_announcements", "none_found",
                               default="None found."),
                        title=title,
                    )
                else:
                    if term and count == 1:
                        found = lang.t(
                            "main_ui", "search_found_one",
                            default=f'1 match for "{term}".', term=term,
                        )
                    elif term:
                        found = lang.t(
                            "main_ui", "search_found_many",
                            default=f'{count} matches for "{term}".',
                            count=count, term=term,
                        )
                    elif count == 1:
                        found = lang.t(
                            "main_ui", "search_found_one_any", default="1 message found.",
                        )
                    else:
                        found = lang.t(
                            "main_ui", "search_found_many_any",
                            default=f"{count} messages found.", count=count,
                        )
                    self.status_label.SetLabel(found)
                    # Focus on the first result puts the reader where they
                    # act next; the count is said as well.
                    self.results_panel.focus_top_message()
                    self.main_frame._play_sound("searching_done")
                    self.main_frame.announce(found, title=title)
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
