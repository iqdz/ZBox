"""
Account Settings dialog. This is where the identity/From address is
edited independently of the login email, the same edit Thunderbird
lets you make: keep logging in as login@example.net while sending and
receiving as you@example.org once the provider has that custom
domain linked to the same mailbox.

Also exposes the IMAP/SMTP server settings (host, port, encryption)
that the account wizard pre-fills from a provider preset. This
matters because if a bundled preset was ever wrong, an account
created from it keeps the wrong values it was created with even
after the preset itself gets fixed; this dialog is what lets that be
corrected without removing and re-adding the account.
"""

from email.utils import formataddr

import wx

import lang

from accessible import fit_dialog, wrap_text
from account_manager import _normalize_identities
from signature_dialog import SignatureEditDialog

ENCRYPTION_CHOICES = ["tls", "start-tls", "none"]
AUTOMATIC_JUNK_LABEL = "(automatic)"


class _IdentityEditDialog(wx.Dialog):
    """Audit finding 49: Add/Edit an extra identity (alias) --
    display name plus email address, same OK-button validation
    pattern as FilterRuleEditDialog (block instead of silently
    accepting an entry with no email address)."""

    def __init__(self, parent, display_name="", email=""):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_identity", default="Identity"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)

        self.name_field = wx.TextCtrl(self, value=display_name)
        self.name_field.SetName(
            lang.t("dialogs", "form_display_name", default="Display name")
        )
        self.email_field = wx.TextCtrl(self, value=email)
        self.email_field.SetName(
            lang.t("dialogs", "form_email_address", default="Email address")
        )

        form = wx.FlexGridSizer(2, 2, 8, 8)
        form.AddGrowableCol(1)
        form.Add(
            wx.StaticText(
                self,
                label=lang.t("dialogs", "form_display_name", default="Display name"),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.name_field, 1, wx.EXPAND)
        form.Add(
            wx.StaticText(
                self,
                label=lang.t("dialogs", "form_email_address", default="Email address"),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.email_field, 1, wx.EXPAND)

        note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_display_name",
                default=(
                    "Display name is optional -- leave it blank to send "
                    "as this account's own name."
                ),
            ),
        )
        wrap_text(note)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(form, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        fit_dialog(self, sizer)

    def _on_ok(self, event):
        if not self.email_field.GetValue().strip():
            wx.MessageBox(
                lang.t(
                    "dialogs", "identity_email_required",
                    default="Enter an email address.",
                ),
                lang.t("dialogs", "title_identity", default="Identity"),
                wx.OK | wx.ICON_INFORMATION,
            )
            self.email_field.SetFocus()
            return
        event.Skip()

    def result(self):
        return {
            "display_name": self.name_field.GetValue().strip(),
            "email": self.email_field.GetValue().strip(),
        }


class AccountSettingsDialog(wx.Dialog):
    def __init__(self, parent, account, folder_choices=None, is_default=False):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_account_settings",
                default=f"Account Settings: {account.display_name}",
                name=account.display_name,
            ),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.account = account
        # Filled by apply_to_account with whatever it is about to
        # overwrite, so restore_account can put it back if the save
        # is refused. See both methods at the bottom of this class.
        self._previous = {}
        self.folder_choices = list(folder_choices or [])

        sizer = wx.BoxSizer(wx.VERTICAL)

        name_label = wx.StaticText(
            self, label=lang.t("dialogs", "form_your_name", default="Your name")
        )
        self.name_field = wx.TextCtrl(self, value=account.display_name)
        self.name_field.SetName(
            lang.t("dialogs", "form_your_name", default="Your name")
        )

        login_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_login_email_label",
                default="Login email (used to sign in)",
            ),
        )
        self.login_field = wx.TextCtrl(self, value=account.login_email)
        self.login_field.SetName(
            lang.t("dialogs", "form_login_email", default="Login email")
        )

        password_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_new_password_label",
                default="New password (leave blank to keep current)",
            ),
        )
        self.password_field = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self.password_field.SetName(
            lang.t("dialogs", "acct_new_password_name", default="New password")
        )

        identity_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_identity_email_label",
                default="Email address (used to send and receive)",
            ),
        )
        self.identity_field = wx.TextCtrl(self, value=account.identity_email)
        self.identity_field.SetName(
            lang.t("dialogs", "form_email_address", default="Email address")
        )

        note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_identity",
                default=(
                    "The email address can differ from the login email if your "
                    "provider has a custom domain linked to this mailbox. "
                    "Mail will be sent and received under the address above, "
                    "while sign-in still uses the login email. Leave the "
                    "password field blank to keep the password already stored "
                    "for this account -- for example after rotating a Gmail "
                    "app password, type the new one here; otherwise leave it "
                    "blank."
                ),
            ),
        )
        wrap_text(note)

        identities_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_identities_label",
                default="Additional identities (aliases)",
            ),
        )
        # Audit finding 49: other addresses this same mailbox can
        # send and reply as -- Gmail's own "Send As" aliases, or
        # another domain routed to the same account. Deep-copied so
        # edits here never touch account.extra_identities until OK
        # (apply_to_account) writes them back, same discard-on-
        # Cancel guarantee every other field in this dialog already
        # has by virtue of only being read on OK.
        self.identities = [dict(entry) for entry in account.extra_identities]
        self.identities_list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.identities_list.SetName(
            lang.t(
                "dialogs", "acct_identities_name",
                default="Additional identities",
            )
        )
        identities_row_height = self.identities_list.GetCharHeight() or 16
        self.identities_list.SetMinSize((-1, identities_row_height * 3))
        self.identities_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_edit_identity)
        self.identities_list.Bind(wx.EVT_LISTBOX, lambda event: self._sync_identity_buttons())

        self.new_identity_button = wx.Button(
            self,
            label=lang.control_label("acct_add_identity", "&Add Identity..."),
        )
        self.edit_identity_button = wx.Button(
            self,
            label=lang.control_label("acct_edit_identity", "&Edit Identity..."),
        )
        self.remove_identity_button = wx.Button(
            self,
            label=lang.control_label("acct_remove_identity", "&Remove Identity"),
        )
        for button, handler in (
            (self.new_identity_button, self._on_new_identity),
            (self.edit_identity_button, self._on_edit_identity),
            (self.remove_identity_button, self._on_remove_identity),
        ):
            button.Bind(wx.EVT_BUTTON, handler)

        identities_button_row = wx.BoxSizer(wx.HORIZONTAL)
        for button in (self.new_identity_button, self.edit_identity_button, self.remove_identity_button):
            identities_button_row.Add(button, 0, wx.RIGHT, 8)

        identities_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_identities",
                default=(
                    "Other addresses this account can send and reply as. "
                    "Replying to a message that was sent to one of these "
                    "defaults From to that address instead of the one "
                    "above."
                ),
            ),
        )
        wrap_text(identities_note)

        signature_label = wx.StaticText(
            self, label=lang.t("dialogs", "acct_signature", default="Signature")
        )
        self.signature_field = wx.TextCtrl(
            self, value=account.signature, style=wx.TE_MULTILINE,
        )
        self.signature_field.SetName(
            lang.t("dialogs", "acct_signature", default="Signature")
        )
        self.signature_field.SetMinSize((-1, self.signature_field.GetCharHeight() * 4))
        # The formatted version, edited in the signature editor rather
        # than typed here. Held on the dialog until OK, the same
        # discard-on-Cancel guarantee every other field has.
        self.signature_html = account.signature_html
        self.edit_signature_button = wx.Button(
            self,
            label=lang.control_label("acct_edit_signature", "Edit Si&gnature..."),
        )
        self.edit_signature_button.Bind(wx.EVT_BUTTON, self._on_edit_signature)

        signature_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_signature",
                default=(
                    "Appended to every new message, reply and forward sent from "
                    "this account, below a \"-- \" line. Leave blank for none. "
                    "Edit Signature opens the formatted editor, where bold, "
                    "lists and links are available; the box above then holds "
                    "the plain text version that goes out alongside it."
                ),
            ),
        )
        wrap_text(signature_note)

        junk_label = wx.StaticText(
            self, label=lang.t("dialogs", "acct_junk_folder", default="Junk folder")
        )
        # Editable (not read-only): the account's real folder list
        # may not have been fetched yet (a brand-new account, or one
        # whose background 'mailbox list' hasn't resolved), and audit
        # finding 46 asks for an override precisely for accounts
        # whose provider isn't one of the built-in presets -- typing
        # a name that isn't in the list yet must still work.
        self.junk_folder_field = wx.ComboBox(
            self, choices=[AUTOMATIC_JUNK_LABEL] + self.folder_choices,
        )
        self.junk_folder_field.SetName(
            lang.t("dialogs", "acct_junk_folder", default="Junk folder")
        )
        self.junk_folder_field.SetValue(account.junk_folder or AUTOMATIC_JUNK_LABEL)

        junk_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_junk",
                default=(
                    "Which real folder Mark as Junk moves messages to. "
                    f"\"{AUTOMATIC_JUNK_LABEL}\" uses the same provider-aware "
                    "guess Archive and Trash already use; set this only if "
                    "that guess is wrong or your junk folder has a custom name."
                ),
                automatic=AUTOMATIC_JUNK_LABEL,
            ),
        )
        wrap_text(junk_note)

        status_label = wx.StaticText(
            self,
            label=lang.t("dialogs", "acct_account_status", default="Account status"),
        )
        status_label.SetFont(status_label.GetFont().Bold())

        self.disable_account_checkbox = wx.CheckBox(
            self,
            label=lang.t(
                "dialogs", "acct_disable_account", default="Disable this account"
            ),
        )
        self.disable_account_checkbox.SetValue(
            not getattr(account, "enabled", True)
        )

        status_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_disable",
                default=(
                    "A disabled account is never contacted -- no IDLE, no "
                    "periodic refresh, nothing -- until re-enabled. It still "
                    "shows in the tree, marked (disabled), and its already-"
                    "downloaded mail stays available offline. Same toggle as "
                    "Accounts > Disable Account or the account's own context "
                    "menu; this is just another place to reach it."
                ),
            ),
        )
        wrap_text(status_note)

        self.auto_check_checkbox = wx.CheckBox(
            self,
            label=lang.t(
                "dialogs", "acct_auto_check",
                default="Check for new messages automatically",
            ),
        )
        self.auto_check_checkbox.SetValue(getattr(account, "auto_check", True))

        auto_check_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_auto_check",
                default=(
                    "Off: ZBox never contacts this account on its own -- no "
                    "new-mail watching, no timed refresh, no background "
                    "downloads -- so an unreliable server cannot slow the "
                    "other accounts. Its downloaded mail still opens, and "
                    "Refresh (F5), sending and deleting still reach the "
                    "server. Same as Pause Automatic Checking in the Tools "
                    "menu or the account's context menu."
                ),
            ),
        )
        wrap_text(auto_check_note)

        default_label = wx.StaticText(
            self,
            label=lang.t("dialogs", "acct_default_account", default="Default account"),
        )
        default_label.SetFont(default_label.GetFont().Bold())

        self.default_account_checkbox = wx.CheckBox(
            self,
            label=lang.t(
                "dialogs", "acct_set_default", default="Set as default account"
            ),
        )
        self.default_account_checkbox.SetValue(is_default)

        default_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_default",
                default=(
                    "This account's folders appear first in the tree, and "
                    "a new message defaults to sending from it unless "
                    "changed manually in the composer. Does not affect "
                    "which address a reply or forward uses -- that already "
                    "follows the message being answered (see Additional "
                    "Identities above). There is always exactly one default "
                    "account, so unchecking this on the current default "
                    "without checking it on another one leaves it unchanged "
                    "-- check another account's box instead to hand the "
                    "default to it."
                ),
            ),
        )
        wrap_text(default_note)

        notifications_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_notifications", default="New mail notifications"
            ),
        )
        notifications_label.SetFont(notifications_label.GetFont().Bold())

        self.mute_account_checkbox = wx.CheckBox(
            self,
            label=lang.t(
                "dialogs", "acct_mute_notifications",
                default="Mute notifications for this account",
            ),
        )
        self.mute_account_checkbox.SetValue(
            getattr(account, "notifications_muted", False)
        )

        notifications_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_note_mute",
                default=(
                    "Muting blocks this account's new-mail sound, spoken "
                    "announcement and Windows notification, regardless of "
                    "how those are otherwise configured. It does not affect "
                    "other accounts, and it's still subject to the master "
                    "switches in Settings > Sound & Notifications (sounds "
                    "and desktop notifications overall)."
                ),
            ),
        )
        wrap_text(notifications_note)

        form = wx.FlexGridSizer(4, 2, 8, 8)
        form.AddGrowableCol(1)
        form.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.name_field, 1, wx.EXPAND)
        form.Add(login_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.login_field, 1, wx.EXPAND)
        form.Add(password_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.password_field, 1, wx.EXPAND)
        form.Add(identity_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.identity_field, 1, wx.EXPAND)

        server_label = wx.StaticText(
            self,
            label=lang.t("dialogs", "acct_server_settings", default="Server settings"),
        )
        server_label.SetFont(server_label.GetFont().Bold())

        # Each label is created immediately before its own field, not
        # all six fields then all six labels -- JAWS's own label
        # heuristic follows creation order, not SetName, for these
        # control types, and the previous field-block-then-label-
        # block layout left every field here reading back whatever
        # static text happened to precede the whole block instead of
        # its own label.
        imap_host_label = wx.StaticText(
            self,
            label=lang.t("dialogs", "srv_imap_host", default="Incoming (IMAP) server"),
        )
        self.imap_host_field = wx.TextCtrl(self, value=account.imap_host)
        self.imap_host_field.SetName(
            lang.t("dialogs", "srv_imap_host", default="Incoming (IMAP) server")
        )
        imap_port_label = wx.StaticText(
            self, label=lang.t("dialogs", "srv_imap_port", default="Incoming port")
        )
        self.imap_port_field = wx.SpinCtrl(self, min=1, max=65535, initial=account.imap_port)
        self.imap_port_field.SetName(
            lang.t("dialogs", "srv_imap_port", default="Incoming port")
        )
        imap_encryption_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "srv_imap_encryption", default="Incoming encryption"
            ),
        )
        self.imap_encryption_choice = wx.Choice(self, choices=ENCRYPTION_CHOICES)
        self.imap_encryption_choice.SetName(
            lang.t("dialogs", "srv_imap_encryption", default="Incoming encryption")
        )
        self.imap_encryption_choice.SetStringSelection(account.imap_encryption)

        smtp_host_label = wx.StaticText(
            self,
            label=lang.t("dialogs", "srv_smtp_host", default="Outgoing (SMTP) server"),
        )
        self.smtp_host_field = wx.TextCtrl(self, value=account.smtp_host)
        self.smtp_host_field.SetName(
            lang.t("dialogs", "srv_smtp_host", default="Outgoing (SMTP) server")
        )
        smtp_port_label = wx.StaticText(
            self, label=lang.t("dialogs", "srv_smtp_port", default="Outgoing port")
        )
        self.smtp_port_field = wx.SpinCtrl(self, min=1, max=65535, initial=account.smtp_port)
        self.smtp_port_field.SetName(
            lang.t("dialogs", "srv_smtp_port", default="Outgoing port")
        )
        smtp_encryption_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "srv_smtp_encryption", default="Outgoing encryption"
            ),
        )
        self.smtp_encryption_choice = wx.Choice(self, choices=ENCRYPTION_CHOICES)
        self.smtp_encryption_choice.SetName(
            lang.t("dialogs", "srv_smtp_encryption", default="Outgoing encryption")
        )
        self.smtp_encryption_choice.SetStringSelection(account.smtp_encryption)

        server_form = wx.FlexGridSizer(6, 2, 8, 8)
        server_form.AddGrowableCol(1)
        server_form.Add(imap_host_label, 0, wx.ALIGN_CENTER_VERTICAL)
        server_form.Add(self.imap_host_field, 1, wx.EXPAND)
        server_form.Add(imap_port_label, 0, wx.ALIGN_CENTER_VERTICAL)
        server_form.Add(self.imap_port_field, 1, wx.EXPAND)
        server_form.Add(imap_encryption_label, 0, wx.ALIGN_CENTER_VERTICAL)
        server_form.Add(self.imap_encryption_choice, 1, wx.EXPAND)
        server_form.Add(smtp_host_label, 0, wx.ALIGN_CENTER_VERTICAL)
        server_form.Add(self.smtp_host_field, 1, wx.EXPAND)
        server_form.Add(smtp_port_label, 0, wx.ALIGN_CENTER_VERTICAL)
        server_form.Add(self.smtp_port_field, 1, wx.EXPAND)
        server_form.Add(smtp_encryption_label, 0, wx.ALIGN_CENTER_VERTICAL)
        server_form.Add(self.smtp_encryption_choice, 1, wx.EXPAND)

        sizer.Add(form, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(identities_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.identities_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        sizer.Add(identities_button_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(identities_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(signature_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.signature_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        sizer.Add(self.edit_signature_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(signature_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(junk_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.junk_folder_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        sizer.Add(junk_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(status_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.disable_account_checkbox, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(status_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(self.auto_check_checkbox, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(auto_check_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(default_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.default_account_checkbox, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(default_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(notifications_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.mute_account_checkbox, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(notifications_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(server_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(server_form, 0, wx.EXPAND | wx.ALL, 10)

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)
        self._refresh_identities_list()

    def _refresh_identities_list(self):
        previous = self.identities_list.GetSelection()
        self.identities_list.Set([
            formataddr((entry["display_name"], entry["email"])) if entry["display_name"] else entry["email"]
            for entry in self.identities
        ])
        if self.identities:
            target = previous if previous != wx.NOT_FOUND else 0
            self.identities_list.SetSelection(min(target, len(self.identities) - 1))
        self._sync_identity_buttons()

    def _sync_identity_buttons(self):
        has_selection = self.identities_list.GetSelection() != wx.NOT_FOUND
        self.edit_identity_button.Enable(has_selection)
        self.remove_identity_button.Enable(has_selection)

    def _on_edit_signature(self, event):
        """Opens the formatted signature editor. What comes back is
        held on this dialog, not written to the account: Cancel here
        still discards it, like every other field."""
        dialog = SignatureEditDialog(
            self, self.signature_html, self.signature_field.GetValue()
        )
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.signature_html = dialog.signature_html
                # The plain box always shows the text alternative, so
                # the two can never drift apart without being seen.
                self.signature_field.SetValue(dialog.signature_text)
        finally:
            dialog.Destroy()

    def _on_new_identity(self, event):
        dialog = _IdentityEditDialog(self)
        if dialog.ShowModal() == wx.ID_OK:
            self.identities.append(dialog.result())
            self._refresh_identities_list()
            self.identities_list.SetSelection(len(self.identities) - 1)
            self._sync_identity_buttons()
        dialog.Destroy()

    def _on_edit_identity(self, event):
        index = self.identities_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        entry = self.identities[index]
        dialog = _IdentityEditDialog(self, entry["display_name"], entry["email"])
        if dialog.ShowModal() == wx.ID_OK:
            self.identities[index] = dialog.result()
            self._refresh_identities_list()
            self.identities_list.SetSelection(index)
        dialog.Destroy()

    def _on_remove_identity(self, event):
        index = self.identities_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        del self.identities[index]
        self._refresh_identities_list()

    # Every attribute apply_to_account writes, named once so the
    # snapshot and the restore cannot drift away from the
    # assignments themselves.
    _EDITED_FIELDS = (
        "display_name", "login_email", "password", "identity_email",
        "imap_host", "imap_port", "imap_encryption",
        "smtp_host", "smtp_port", "smtp_encryption",
        "signature", "signature_html", "junk_folder", "notifications_muted",
        "extra_identities", "enabled", "auto_check",
    )

    def wants_default(self):
        """Whether the person checked 'Set as default account' --
        not one of _EDITED_FIELDS/apply_to_account, since promoting
        an account is a reorder of AccountManager.accounts, not a
        field on this one Account. An ordinary toggle: checked simply
        means 'promote this one', unchecked means 'leave it as is' --
        there is no separate action for unchecking the current
        default with nothing else checked, since the account list
        always has exactly one default and this dialog only ever
        edits one account at a time. The caller (main_frame.
        _on_account_settings) reads this after a successful save and
        calls account_manager.set_default_account() itself."""
        return self.default_account_checkbox.GetValue()

    def apply_to_account(self):
        """Writes the edited values onto the live Account.

        Onto the live object deliberately, not a copy: every other
        holder of it -- open tabs, the IDLE watcher, the tree -- sees
        the edit immediately because they are all holding the same
        object. That is also why restore_account exists. The edits
        land before the caller knows whether the account list can be
        saved at all, so a refusal needs them taken back off again.
        """
        self._previous = {
            name: getattr(self.account, name) for name in self._EDITED_FIELDS
        }
        self.account.display_name = self.name_field.GetValue().strip()
        self.account.login_email = self.login_field.GetValue().strip()
        self.account.password = self.password_field.GetValue()
        self.account.identity_email = self.identity_field.GetValue().strip() or self.account.login_email
        self.account.imap_host = self.imap_host_field.GetValue().strip()
        self.account.imap_port = self.imap_port_field.GetValue()
        self.account.imap_encryption = self.imap_encryption_choice.GetStringSelection()
        self.account.smtp_host = self.smtp_host_field.GetValue().strip()
        self.account.smtp_port = self.smtp_port_field.GetValue()
        self.account.smtp_encryption = self.smtp_encryption_choice.GetStringSelection()
        self.account.signature = self.signature_field.GetValue()
        self.account.signature_html = self.signature_html
        junk_value = self.junk_folder_field.GetValue().strip()
        self.account.junk_folder = "" if junk_value in ("", AUTOMATIC_JUNK_LABEL) else junk_value
        self.account.notifications_muted = self.mute_account_checkbox.GetValue()
        self.account.extra_identities = _normalize_identities(self.identities)
        self.account.enabled = not self.disable_account_checkbox.GetValue()
        self.account.auto_check = self.auto_check_checkbox.GetValue()
        return self.account

    def restore_account(self):
        """Puts back what apply_to_account overwrote, for a save the
        account manager refused.

        On the same object rather than a replacement, so every
        reference to this Account stays the one the rest of ZBox is
        holding. Nothing the edits describe reached disk, and without
        this the session would run on settings that exist nowhere --
        a changed IMAP host, a changed login email -- until ZBox is
        restarted. The typed password goes with them: update_account
        only blanks it on the path that stores it, so a refusal
        leaves the raw secret sitting on the object.
        """
        if not self._previous:
            return
        for name, value in self._previous.items():
            setattr(self.account, name, value)
        self._previous = {}
