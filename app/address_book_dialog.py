"""
Address Book dialog (Ctrl+Shift+B / Tools > Address Book...).

Browses every contact ZBox knows about -- both the ones captured
automatically from mail sent and received, and any entered by hand
here -- with New/Edit/Delete, plus Import and Export in the two most
widely supported address-book interchange formats: vCard (.vcf, used
by Outlook, Apple Contacts, Google Contacts and Thunderbird) and CSV.

Also manages Groups (audit finding 50): named mailing lists of
existing contacts. Typing a group's name into a compose window's To,
Cc or Bcc field expands it to every member's address at once (see
compose_panel._ContactCompleter) -- there is no separate "send to
group" action, expansion through the same autocomplete every address
already goes through is the only compose-side change this needed.
"""

import os

import wx

import lang
from accessible import wrap_text
import contacts as contacts_module


class ContactEditDialog(wx.Dialog):
    """New/Edit form for a single contact: Name, Email, Phone and
    Notes (audit finding 50 added the last two) -- everything this
    address book stores per contact."""

    def __init__(self, parent, name="", email="", phone="", notes="", title="New Contact"):
        super().__init__(
            parent, title=title, size=(420, 320),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)

        sizer = wx.BoxSizer(wx.VERTICAL)

        name_label = wx.StaticText(
            self, label=lang.t("dialogs", "ab_name", default="Name")
        )
        self.name_field = wx.TextCtrl(self, value=name)
        self.name_field.SetName(lang.t("dialogs", "ab_name", default="Name"))

        email_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "form_email_address", default="Email address",
            ),
        )
        self.email_field = wx.TextCtrl(self, value=email)
        self.email_field.SetName(
            lang.t("dialogs", "form_email_address", default="Email address")
        )

        phone_label = wx.StaticText(
            self, label=lang.t("dialogs", "ab_phone", default="Phone")
        )
        self.phone_field = wx.TextCtrl(self, value=phone)
        self.phone_field.SetName(
            lang.t("dialogs", "ab_phone", default="Phone")
        )

        form = wx.FlexGridSizer(3, 2, 8, 8)
        form.AddGrowableCol(1)
        form.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.name_field, 1, wx.EXPAND)
        form.Add(email_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.email_field, 1, wx.EXPAND)
        form.Add(phone_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.phone_field, 1, wx.EXPAND)
        sizer.Add(form, 0, wx.EXPAND | wx.ALL, 12)

        notes_label = wx.StaticText(
            self, label=lang.t("dialogs", "ab_notes", default="Notes")
        )
        self.notes_field = wx.TextCtrl(self, value=notes, style=wx.TE_MULTILINE)
        self.notes_field.SetName(
            lang.t("dialogs", "ab_notes", default="Notes")
        )
        self.notes_field.SetMinSize((-1, self.notes_field.GetCharHeight() * 4))
        sizer.Add(notes_label, 0, wx.LEFT | wx.RIGHT, 12)
        sizer.Add(self.notes_field, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        wx.CallAfter(self.name_field.SetFocus)

    def _on_ok(self, event):
        if not self.email_field.GetValue().strip():
            wx.MessageBox(
                lang.t(
                    "dialogs", "contact_email_required",
                    default="An email address is required.",
                ),
                lang.t("dialogs", "title_new_contact", default="New Contact"),
                wx.OK | wx.ICON_WARNING,
            )
            self.email_field.SetFocus()
            return
        event.Skip()

    def result(self):
        return (
            self.name_field.GetValue().strip(),
            self.email_field.GetValue().strip(),
            self.phone_field.GetValue().strip(),
            self.notes_field.GetValue().strip(),
        )


class AddressBookDialog(wx.Dialog):
    def __init__(self, parent, contact_manager):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_address_book", default="Address Book"),
            size=(560, 480), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.contacts = contact_manager

        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(
            self, label=lang.t("dialogs", "ab_heading", default="Address Book")
        )
        heading.SetFont(heading.GetFont().Bold())
        sizer.Add(heading, 0, wx.ALL, 8)

        self.list_ctrl = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.SetName(
            lang.t("dialogs", "ab_contacts", default="Contacts")
        )
        self.list_ctrl.InsertColumn(0, lang.t('dialogs', 'ab_col_name', default="Name"), width=200)
        self.list_ctrl.InsertColumn(1, lang.t('dialogs', 'ab_col_email', default="Email"), width=220)
        self.list_ctrl.InsertColumn(2, lang.t('dialogs', 'ab_col_phone', default="Phone"), width=120)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_edit)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        row_buttons = wx.BoxSizer(wx.HORIZONTAL)
        new_button = wx.Button(
            self, label=lang.control_label("ab_new", "&New...")
        )
        new_button.Bind(wx.EVT_BUTTON, self._on_new)
        edit_button = wx.Button(
            self, label=lang.control_label("ab_edit", "&Edit...")
        )
        edit_button.Bind(wx.EVT_BUTTON, self._on_edit)
        delete_button = wx.Button(
            self, label=lang.control_label("ab_delete", "&Delete")
        )
        delete_button.Bind(wx.EVT_BUTTON, self._on_delete)
        row_buttons.Add(new_button, 0, wx.RIGHT, 6)
        row_buttons.Add(edit_button, 0, wx.RIGHT, 6)
        row_buttons.Add(delete_button, 0)
        sizer.Add(row_buttons, 0, wx.ALL, 8)

        io_buttons = wx.BoxSizer(wx.HORIZONTAL)
        import_button = wx.Button(
            self, label=lang.control_label("ab_import", "&Import...")
        )
        import_button.Bind(wx.EVT_BUTTON, self._on_import)
        export_button = wx.Button(
            self, label=lang.control_label("ab_export", "Ex&port...")
        )
        export_button.Bind(wx.EVT_BUTTON, self._on_export)
        io_buttons.Add(import_button, 0, wx.RIGHT, 6)
        io_buttons.Add(export_button, 0)
        sizer.Add(io_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        groups_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "ab_groups_label", default="Groups (mailing lists)",
            ),
        )
        groups_label.SetFont(groups_label.GetFont().Bold())
        sizer.Add(groups_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.groups_list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.groups_list.SetName(
            lang.t("dialogs", "ab_groups_name", default="Groups")
        )
        groups_row_height = self.groups_list.GetCharHeight() or 16
        self.groups_list.SetMinSize((-1, groups_row_height * 3))
        self.groups_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_manage_group_members)
        self.groups_list.Bind(wx.EVT_LISTBOX, lambda event: self._sync_group_buttons())
        sizer.Add(self.groups_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        group_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.new_group_button = wx.Button(
            self, label=lang.control_label("ab_new_group", "New &Group...")
        )
        self.rename_group_button = wx.Button(
            self, label=lang.control_label("ab_rename_group", "Rena&me Group...")
        )
        self.delete_group_button = wx.Button(
            self, label=lang.control_label("ab_delete_group", "Delete Gro&up")
        )
        self.manage_members_button = wx.Button(
            self,
            label=lang.control_label(
                "ab_manage_members", "&Manage Members...",
            ),
        )
        for button, handler in (
            (self.new_group_button, self._on_new_group),
            (self.rename_group_button, self._on_rename_group),
            (self.delete_group_button, self._on_delete_group),
            (self.manage_members_button, self._on_manage_group_members),
        ):
            button.Bind(wx.EVT_BUTTON, handler)
            group_buttons.Add(button, 0, wx.RIGHT, 6)
        sizer.Add(group_buttons, 0, wx.ALL, 8)

        groups_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "ab_note_groups",
                default=(
                    "Type a group's name in a compose window's To, Cc or "
                    "Bcc field to fill in every one of its members at once."
                ),
            ),
        )
        wrap_text(groups_note)
        sizer.Add(groups_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        close_sizer = wx.BoxSizer(wx.HORIZONTAL)
        close_button = wx.Button(
            self, wx.ID_CLOSE,
            lang.t("dialogs", "btn_close_plain", default="Close"),
        )
        close_button.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        close_sizer.Add(close_button, 0)
        sizer.Add(close_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)

        self._populate()
        self._populate_groups()
        wx.CallAfter(self.list_ctrl.SetFocus)

    # --- List population -------------------------------------------

    def _populate(self):
        self.list_ctrl.DeleteAllItems()
        self._rows = self.contacts.all_contacts()
        if not self._rows:
            self.list_ctrl.InsertItem(0, "")
            self.list_ctrl.SetItem(0, 1, lang.t('dialogs', 'ab_no_contacts', default="No contacts yet."))
            return
        for row, contact in enumerate(self._rows):
            self.list_ctrl.InsertItem(row, contact.get("name") or "")
            self.list_ctrl.SetItem(row, 1, contact.get("email", ""))
            self.list_ctrl.SetItem(row, 2, contact.get("phone", ""))

    def _selected_contact(self):
        index = self.list_ctrl.GetFirstSelected()
        if index == -1 or index >= len(self._rows):
            return None
        return self._rows[index]

    # --- New / Edit / Delete -----------------------------------------

    def _on_new(self, event):
        dialog = ContactEditDialog(
            self,
            title=lang.t("dialogs", "title_new_contact", default="New Contact"),
        )
        if dialog.ShowModal() == wx.ID_OK:
            name, email, phone, notes = dialog.result()
            self.contacts.upsert(name, email, phone=phone, notes=notes)
            self._populate()
        dialog.Destroy()

    def _on_edit(self, event):
        contact = self._selected_contact()
        if contact is None:
            wx.MessageBox(
                lang.t(
                    "dialogs", "contact_select_to_edit",
                    default="Select a contact to edit.",
                ),
                lang.t("dialogs", "title_edit_contact", default="Edit Contact"),
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        dialog = ContactEditDialog(
            self, name=contact.get("name", ""), email=contact.get("email", ""),
            phone=contact.get("phone", ""), notes=contact.get("notes", ""),
            title=lang.t("dialogs", "title_edit_contact", default="Edit Contact"),
        )
        if dialog.ShowModal() == wx.ID_OK:
            name, email, phone, notes = dialog.result()
            self.contacts.upsert(
                name, email, previous_email=contact.get("email"), phone=phone, notes=notes,
            )
            self._populate()
        dialog.Destroy()

    def _on_delete(self, event):
        contact = self._selected_contact()
        if contact is None:
            wx.MessageBox(
                lang.t(
                    "dialogs", "contact_select_to_delete",
                    default="Select a contact to delete.",
                ),
                lang.t("dialogs", "title_delete_contact", default="Delete Contact"),
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        label = contacts_module.format_contact(contact)
        confirm = wx.MessageBox(
            lang.t(
                "dialogs", "contact_delete_confirm",
                default=f"Delete {label} from the address book?", label=label,
            ),
            lang.t("dialogs", "title_delete_contact", default="Delete Contact"),
            wx.YES_NO | wx.ICON_WARNING,
        )
        if confirm == wx.YES:
            self.contacts.remove(contact.get("email"))
            self._populate()

    # --- Import / Export -----------------------------------------

    def _on_import(self, event):
        wildcard = (
            "vCard and CSV files (*.vcf;*.csv)|*.vcf;*.csv|"
            "vCard files (*.vcf)|*.vcf|"
            "CSV files (*.csv)|*.csv"
        )
        dialog = wx.FileDialog(
            self,
            lang.t(
                "dialogs", "title_import_contacts", default="Import Contacts",
            ),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        path = dialog.GetPath()
        dialog.Destroy()

        extension = os.path.splitext(path)[1].lower()
        try:
            if extension == ".vcf":
                count = self.contacts.import_vcard(path)
            elif extension == ".csv":
                count = self.contacts.import_csv(path)
            else:
                wx.MessageBox(
                    lang.t(
                        "dialogs", "contacts_import_choose_file",
                        default="Choose a .vcf (vCard) or .csv file to import.",
                    ),
                    lang.t(
                        "dialogs", "title_import_contacts",
                        default="Import Contacts",
                    ),
                    wx.OK | wx.ICON_WARNING,
                )
                return
        except OSError as exc:
            wx.MessageBox(
                lang.t(
                    "errors", "contacts_import_unreadable",
                    default=f"Could not read that file.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_import_contacts", default="Import Contacts"),
                wx.OK | wx.ICON_ERROR,
            )
            return

        self._populate()
        wx.MessageBox(
            lang.t(
                "dialogs", "contacts_imported",
                default=f"Imported {count} contact(s).", count=count,
            ) if count else lang.t(
                "dialogs", "contacts_import_empty",
                default="No contacts were found in that file.",
            ),
            lang.t("dialogs", "title_import_contacts", default="Import Contacts"),
            wx.OK | wx.ICON_INFORMATION,
        )

    def _on_export(self, event):
        if not self._rows:
            wx.MessageBox(
                lang.t(
                    "dialogs", "contacts_none_to_export",
                    default="There are no contacts to export.",
                ),
                lang.t("dialogs", "title_export_contacts", default="Export Contacts"),
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        wildcard = "vCard file (*.vcf)|*.vcf|CSV file (*.csv)|*.csv"
        dialog = wx.FileDialog(
            self,
            lang.t(
                "dialogs", "title_export_contacts", default="Export Contacts",
            ),
            wildcard=wildcard,
            defaultFile="contacts.vcf",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        path = dialog.GetPath()
        filter_index = dialog.GetFilterIndex()  # 0 = vCard, 1 = CSV
        dialog.Destroy()

        # A user who typed their own filename (rather than picking a
        # filter first) may not match the extension to the chosen
        # filter; the filter index is the source of truth for which
        # writer to run, and the extension is fixed up to match so
        # the file that lands on disk is never a .csv with vCard
        # content inside or vice versa.
        if filter_index == 1:
            if not path.lower().endswith(".csv"):
                path = os.path.splitext(path)[0] + ".csv"
            writer = self.contacts.export_csv
        else:
            if not path.lower().endswith(".vcf"):
                path = os.path.splitext(path)[0] + ".vcf"
            writer = self.contacts.export_vcard

        try:
            writer(path)
        except OSError as exc:
            wx.MessageBox(
                lang.t(
                    "errors", "contacts_export_unwritable",
                    default=f"Could not write that file.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_export_contacts", default="Export Contacts"),
                wx.OK | wx.ICON_ERROR,
            )
            return

        wx.MessageBox(
            lang.t(
                "dialogs", "contacts_exported",
                default=f"Exported {len(self._rows)} contact(s) to:\n{path}",
                count=len(self._rows), path=path,
            ),
            lang.t("dialogs", "title_export_contacts", default="Export Contacts"),
            wx.OK | wx.ICON_INFORMATION,
        )

    # --- Groups (mailing lists) --------------------------------------

    def _populate_groups(self):
        previous = self.groups_list.GetSelection()
        self._group_rows = self.contacts.all_groups()
        self.groups_list.Set([group["name"] for group in self._group_rows])
        if self._group_rows:
            target = previous if previous != wx.NOT_FOUND else 0
            self.groups_list.SetSelection(min(target, len(self._group_rows) - 1))
        self._sync_group_buttons()

    def _selected_group(self):
        index = self.groups_list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._group_rows):
            return None
        return self._group_rows[index]

    def _sync_group_buttons(self):
        has_selection = self._selected_group() is not None
        self.rename_group_button.Enable(has_selection)
        self.delete_group_button.Enable(has_selection)
        self.manage_members_button.Enable(has_selection)

    def _on_new_group(self, event):
        dialog = wx.TextEntryDialog(
            self,
            lang.t("dialogs", "ab_group_name_prompt", default="Group name:"),
            lang.t("dialogs", "title_new_group", default="New Group"),
        )
        if dialog.ShowModal() == wx.ID_OK:
            name = dialog.GetValue().strip()
            if name:
                self.contacts.add_group(name)
                self._populate_groups()
        dialog.Destroy()

    def _on_rename_group(self, event):
        group = self._selected_group()
        if group is None:
            return
        dialog = wx.TextEntryDialog(
            self,
            lang.t("dialogs", "ab_group_name_prompt", default="Group name:"),
            lang.t("dialogs", "title_rename_group", default="Rename Group"),
            value=group["name"],
        )
        if dialog.ShowModal() == wx.ID_OK:
            new_name = dialog.GetValue().strip()
            if new_name and new_name != group["name"]:
                if self.contacts.rename_group(group["name"], new_name):
                    self._populate_groups()
                else:
                    wx.MessageBox(
                        lang.t(
                            "dialogs", "group_name_exists",
                            default=f'A group named "{new_name}" already exists.',
                            name=new_name,
                        ),
                        lang.t(
                            "dialogs", "title_rename_group", default="Rename Group"
                        ),
                        wx.OK | wx.ICON_WARNING,
                    )
        dialog.Destroy()

    def _on_delete_group(self, event):
        group = self._selected_group()
        if group is None:
            return
        confirm = wx.MessageBox(
            lang.t(
                "dialogs", "group_delete_confirm",
                default=f'Delete the group "{group["name"]}"? Its contacts are not deleted.',
                name=group["name"],
            ),
            lang.t("dialogs", "title_delete_group", default="Delete Group"),
            wx.YES_NO | wx.ICON_WARNING,
        )
        if confirm == wx.YES:
            self.contacts.remove_group(group["name"])
            self._populate_groups()

    def _on_manage_group_members(self, event):
        group = self._selected_group()
        if group is None:
            return
        dialog = GroupMembersDialog(self, self.contacts, group["name"])
        dialog.ShowModal()
        dialog.Destroy()


class GroupMembersDialog(wx.Dialog):
    """
    Manage one group's membership: two lists (every contact not
    already a member, and this group's current members), with
    Add/Remove moving an entry between them -- the same shape most
    mail clients use for mailing-list membership, and simpler and
    more explicit for a screen-reader user than a checklist column
    buried in the main contacts view would be.
    """

    def __init__(self, parent, contact_manager, group_name):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_members",
                default=f"Members: {group_name}", name=group_name,
            ),
            size=(560, 420), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.contacts = contact_manager
        self.group_name = group_name

        sizer = wx.BoxSizer(wx.VERTICAL)
        lists_row = wx.BoxSizer(wx.HORIZONTAL)

        all_column = wx.BoxSizer(wx.VERTICAL)
        all_column.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "ab_all_contacts", default="All contacts",
                ),
            ),
            0, wx.BOTTOM, 4,
        )
        self.all_list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.all_list.SetName(
            lang.t("dialogs", "ab_all_contacts", default="All contacts")
        )
        all_column.Add(self.all_list, 1, wx.EXPAND)

        button_column = wx.BoxSizer(wx.VERTICAL)
        self.add_button = wx.Button(
            self, label=lang.t("dialogs", "ab_add_member", default="Add >>")
        )
        self.remove_button = wx.Button(
            self, label=lang.t("dialogs", "ab_remove_member", default="<< Remove")
        )
        self.add_button.Bind(wx.EVT_BUTTON, self._on_add)
        self.remove_button.Bind(wx.EVT_BUTTON, self._on_remove)
        button_column.AddStretchSpacer()
        button_column.Add(self.add_button, 0, wx.BOTTOM, 8)
        button_column.Add(self.remove_button, 0)
        button_column.AddStretchSpacer()

        members_column = wx.BoxSizer(wx.VERTICAL)
        members_column.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "ab_members_of",
                    default=f"Members of {group_name}", name=group_name,
                ),
            ),
            0, wx.BOTTOM, 4,
        )
        self.members_list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.members_list.SetName(
            lang.t(
                "dialogs", "ab_members_of",
                default=f"Members of {group_name}", name=group_name,
            )
        )
        members_column.Add(self.members_list, 1, wx.EXPAND)

        lists_row.Add(all_column, 1, wx.EXPAND | wx.RIGHT, 8)
        lists_row.Add(button_column, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        lists_row.Add(members_column, 1, wx.EXPAND)
        sizer.Add(lists_row, 1, wx.EXPAND | wx.ALL, 10)

        close_sizer = wx.BoxSizer(wx.HORIZONTAL)
        close_button = wx.Button(
            self, wx.ID_CLOSE,
            lang.t("dialogs", "btn_close_plain", default="Close"),
        )
        close_button.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        close_sizer.Add(close_button, 0)
        sizer.Add(close_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)

        self._refresh()
        wx.CallAfter(self.all_list.SetFocus)

    def _refresh(self):
        members = self.contacts.group_members(self.group_name)
        member_emails = {contact.get("email", "").lower() for contact in members}
        self._available = [
            contact for contact in self.contacts.all_contacts()
            if contact.get("email", "").lower() not in member_emails
        ]
        self._members = members
        self.all_list.Set([contacts_module.format_contact(c) for c in self._available])
        self.members_list.Set([contacts_module.format_contact(c) for c in self._members])

    def _on_add(self, event):
        index = self.all_list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._available):
            return
        email = self._available[index].get("email")
        self.contacts.add_to_group(self.group_name, email)
        self._refresh()

    def _on_remove(self, event):
        index = self.members_list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._members):
            return
        email = self._members[index].get("email")
        self.contacts.remove_from_group(self.group_name, email)
        self._refresh()
