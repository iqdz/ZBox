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

import threading
from email.utils import formataddr

import wx

import lang
import notice_toast

from accessible import fit_dialog, wrap_text
from account_manager import _normalize_identities, _normalize_openpgp, check_smtp_login
import ms_oauth
from ms_signin_panel import MicrosoftSignInPanel
from provider_presets import MICROSOFT_SERVERS
from spoken_feedback import speak
from message_build import invalid_addresses
from signature_dialog import SignatureEditDialog

ENCRYPTION_CHOICES = ["tls", "start-tls", "none"]
AUTOMATIC_JUNK_LABEL = "(automatic)"


class _IdentityEditDialog(wx.Dialog):
    """Audit finding 49: Add/Edit an extra identity (alias).

    Display name and email address, then six optional settings. Each
    is a check box followed directly by its own fields, and those
    fields stay disabled, which also keeps them out of the tab order,
    until the box is checked. An unchecked box means this identity
    uses the account's own setting. OK blocks a checked box whose
    field is empty or not valid, says which, and puts focus there,
    the same pattern as FilterRuleEditDialog.

    Every label is created immediately before its own field, because
    JAWS names these controls by creation order (see the server
    settings in AccountSettingsDialog).
    """

    def __init__(self, parent, entry=None, folder_choices=None, account=None,
                 secret_lookup=None, has_stored_password=False):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_identity", default="Identity"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        entry = dict(entry or {})
        self._identity_id = entry.get("identity_id", "")
        self._smtp_auth = entry.get("smtp_auth", "plain")
        self._account = account
        self._secret_lookup = secret_lookup
        self._has_stored_password = has_stored_password
        self._testing = False
        folder_choices = list(folder_choices or [])
        # Each check box and the controls it switches on.
        self._groups = []

        name_label = wx.StaticText(
            self, label=lang.t("dialogs", "form_display_name", default="Display name"),
        )
        self.name_field = wx.TextCtrl(self, value=entry.get("display_name", ""))
        self.name_field.SetName(
            lang.t("dialogs", "form_display_name", default="Display name")
        )
        email_label = wx.StaticText(
            self, label=lang.t("dialogs", "form_email_address", default="Email address"),
        )
        self.email_field = wx.TextCtrl(self, value=entry.get("email", ""))
        self.email_field.SetName(
            lang.t("dialogs", "form_email_address", default="Email address")
        )

        form = wx.FlexGridSizer(2, 2, 8, 8)
        form.AddGrowableCol(1)
        form.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.name_field, 1, wx.EXPAND)
        form.Add(email_label, 0, wx.ALIGN_CENTER_VERTICAL)
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

        # 1. Its own signature. Checked with an empty box means this
        # identity sends with no signature at all.
        self.signature_box = self._check_box(
            sizer, "acct_identity_own_signature", "Use its own signature",
            entry.get("own_signature"),
        )
        signature_label = wx.StaticText(
            self, label=lang.t("dialogs", "acct_signature", default="Signature"),
        )
        self.signature_field = wx.TextCtrl(
            self, value=entry.get("signature", ""), style=wx.TE_MULTILINE,
        )
        self.signature_field.SetName(
            lang.t("dialogs", "acct_signature", default="Signature")
        )
        self.signature_field.SetMinSize((-1, self.signature_field.GetCharHeight() * 4))
        # Same rule as the account's own signature: the formatted
        # version is dropped if the plain box is edited by hand past it.
        self.signature_html = entry.get("signature_html", "")
        self._plain_synced = self.signature_field.GetValue()
        self.edit_signature_button = wx.Button(
            self, label=lang.control_label("acct_edit_signature", "Edit Si&gnature..."),
        )
        self.edit_signature_button.Bind(wx.EVT_BUTTON, self._on_edit_signature)
        sizer.Add(signature_label, 0, wx.LEFT | wx.RIGHT, 28)
        sizer.Add(self.signature_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        sizer.Add(self.edit_signature_button, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)
        self._groups.append((self.signature_box, [
            signature_label, self.signature_field, self.edit_signature_button,
        ]))

        # 2 to 4. Reply-To, Always Cc, Always Bcc.
        self.reply_to_box, self.reply_to_field = self._address_group(
            sizer, "acct_identity_own_reply_to", "Use its own Reply-To address",
            "acct_identity_reply_to", "Reply-To address", entry.get("reply_to", ""),
        )
        self.cc_box, self.cc_field = self._address_group(
            sizer, "acct_identity_always_cc", "Always Cc these addresses",
            "acct_identity_cc", "Cc addresses", entry.get("always_cc", ""),
        )
        self.bcc_box, self.bcc_field = self._address_group(
            sizer, "acct_identity_always_bcc", "Always Bcc these addresses",
            "acct_identity_bcc", "Bcc addresses", entry.get("always_bcc", ""),
        )

        # 5. Its own Drafts and Sent folders. Editable, like the
        # account's Junk folder: the folder list may not be fetched yet.
        self.folders_box = self._check_box(
            sizer, "acct_identity_own_folders", "Use its own Drafts and Sent folders",
            entry.get("own_folders"),
        )
        folders_form = wx.FlexGridSizer(2, 2, 8, 8)
        folders_form.AddGrowableCol(1)
        drafts_label = wx.StaticText(
            self, label=lang.t("dialogs", "acct_identity_drafts", default="Drafts folder"),
        )
        self.drafts_field = wx.ComboBox(
            self, value=entry.get("drafts_folder", ""), choices=folder_choices,
        )
        self.drafts_field.SetName(
            lang.t("dialogs", "acct_identity_drafts", default="Drafts folder")
        )
        sent_label = wx.StaticText(
            self, label=lang.t("dialogs", "acct_identity_sent", default="Sent folder"),
        )
        self.sent_field = wx.ComboBox(
            self, value=entry.get("sent_folder", ""), choices=folder_choices,
        )
        self.sent_field.SetName(
            lang.t("dialogs", "acct_identity_sent", default="Sent folder")
        )
        for label, field in ((drafts_label, self.drafts_field), (sent_label, self.sent_field)):
            folders_form.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
            folders_form.Add(field, 1, wx.EXPAND)
        sizer.Add(folders_form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self._groups.append((self.folders_box, [
            drafts_label, self.drafts_field, sent_label, self.sent_field,
        ]))

        # 6. Its own outgoing server.
        self.smtp_box = self._check_box(
            sizer, "acct_identity_own_smtp", "Use its own outgoing server",
            entry.get("own_smtp"),
        )
        smtp_form = wx.FlexGridSizer(5, 2, 8, 8)
        smtp_form.AddGrowableCol(1)
        host_label = wx.StaticText(
            self, label=lang.t("dialogs", "srv_smtp_host", default="Outgoing (SMTP) server"),
        )
        self.smtp_host_field = wx.TextCtrl(self, value=entry.get("smtp_host", ""))
        self.smtp_host_field.SetName(
            lang.t("dialogs", "srv_smtp_host", default="Outgoing (SMTP) server")
        )
        port_label = wx.StaticText(
            self, label=lang.t("dialogs", "srv_smtp_port", default="Outgoing port"),
        )
        self.smtp_port_field = wx.SpinCtrl(
            self, min=1, max=65535, initial=int(entry.get("smtp_port") or 465),
        )
        self.smtp_port_field.SetName(
            lang.t("dialogs", "srv_smtp_port", default="Outgoing port")
        )
        encryption_label = wx.StaticText(
            self, label=lang.t("dialogs", "srv_smtp_encryption", default="Outgoing encryption"),
        )
        self.smtp_encryption_choice = wx.Choice(self, choices=ENCRYPTION_CHOICES)
        self.smtp_encryption_choice.SetName(
            lang.t("dialogs", "srv_smtp_encryption", default="Outgoing encryption")
        )
        self.smtp_encryption_choice.SetStringSelection(entry.get("smtp_encryption") or "tls")
        login_label = wx.StaticText(
            self, label=lang.t("dialogs", "acct_identity_smtp_login", default="Outgoing login name"),
        )
        self.smtp_login_field = wx.TextCtrl(self, value=entry.get("smtp_login", ""))
        self.smtp_login_field.SetName(
            lang.t("dialogs", "acct_identity_smtp_login", default="Outgoing login name")
        )
        if has_stored_password:
            password_text = lang.t(
                "dialogs", "acct_new_password_label",
                default="New password (leave blank to keep current)",
            )
        else:
            password_text = lang.t(
                "dialogs", "acct_identity_smtp_password", default="Outgoing password",
            )
        password_label = wx.StaticText(self, label=password_text)
        self.smtp_password_field = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self.smtp_password_field.SetName(password_text)
        for label, field in (
            (host_label, self.smtp_host_field),
            (port_label, self.smtp_port_field),
            (encryption_label, self.smtp_encryption_choice),
            (login_label, self.smtp_login_field),
            (password_label, self.smtp_password_field),
        ):
            smtp_form.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
            smtp_form.Add(field, 1, wx.EXPAND)
        self.test_smtp_button = wx.Button(
            self, label=lang.control_label("acct_identity_test_smtp", "&Test Outgoing Server"),
        )
        self.test_smtp_button.Bind(wx.EVT_BUTTON, self._on_test_smtp)
        smtp_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "acct_identity_note_smtp",
                default=(
                    "Leave the login name blank to sign in with this identity's "
                    "email address. Sent copies still go to this account's Sent "
                    "folder, or to this identity's own Sent folder when it has one."
                ),
            ),
        )
        wrap_text(smtp_note)
        sizer.Add(smtp_form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.test_smtp_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(smtp_note, 0, wx.EXPAND | wx.ALL, 10)
        self._groups.append((self.smtp_box, [
            host_label, self.smtp_host_field, port_label, self.smtp_port_field,
            encryption_label, self.smtp_encryption_choice, login_label,
            self.smtp_login_field, password_label, self.smtp_password_field,
            self.test_smtp_button,
        ]))

        # 7. OpenPGP (stage 3): this identity's End-to-End Encryption,
        # held here until OK like every other field.
        self.openpgp = dict(entry.get("openpgp") or {})
        self.e2e_button = wx.Button(
            self, label=lang.control_label("acct_e2e_button", "End-to-End Encr&yption..."),
        )
        self.e2e_button.Bind(wx.EVT_BUTTON, self._on_e2e)
        sizer.Add(self.e2e_button, 0, wx.ALL, 10)

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        self._sync_groups()
        fit_dialog(self, sizer)

    def _check_box(self, sizer, key, default, checked):
        box = wx.CheckBox(self, label=lang.t("dialogs", key, default=default))
        box.SetValue(bool(checked))
        box.Bind(wx.EVT_CHECKBOX, lambda _event: self._sync_groups())
        sizer.Add(box, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        return box

    def _address_group(self, sizer, box_key, box_default, field_key, field_default, value):
        box = self._check_box(sizer, box_key, box_default, bool(value))
        label = wx.StaticText(self, label=lang.t("dialogs", field_key, default=field_default))
        field = wx.TextCtrl(self, value=value)
        field.SetName(lang.t("dialogs", field_key, default=field_default))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row.Add(field, 1, wx.EXPAND)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 10)
        self._groups.append((box, [label, field]))
        return box, field

    def _sync_groups(self):
        for box, controls in self._groups:
            enabled = box.GetValue()
            for control in controls:
                control.Enable(enabled)
        if self._testing:
            self.test_smtp_button.Disable()

    def _refuse(self, key, default, field, **values):
        notice_toast.notify(
            self,
            lang.t("dialogs", key, default=default, **values),
            lang.t("dialogs", "title_identity", default="Identity"),
        )
        field.SetFocus()

    def _on_edit_signature(self, event):
        """The formatted signature editor, as in AccountSettingsDialog.
        What comes back is held here until OK."""
        dialog = SignatureEditDialog(
            self, self.signature_html, self.signature_field.GetValue(),
            account_label=self.email_field.GetValue().strip(),
        )
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.signature_html = dialog.signature_html
                self.signature_field.SetValue(dialog.signature_text)
                self._plain_synced = self.signature_field.GetValue()
        finally:
            dialog.Destroy()

    def _on_e2e(self, event):
        import openpgp_compose

        result = openpgp_compose.edit_settings(self, self.email_field.GetValue().strip(), self.openpgp)
        if result is not None:
            self.openpgp = result

    def _stored_password(self):
        if not (self._identity_id and self._account is not None and self._secret_lookup):
            return ""
        try:
            return self._secret_lookup(f"{self._account.account_id}.{self._identity_id}") or ""
        except Exception:  # noqa: BLE001 - none to test with
            return ""

    def _on_test_smtp(self, event):
        """Signs in to the outgoing server on a background thread, so
        a slow server never freezes the dialog. User-started, so it is
        announced when it starts and reported when it ends."""
        if self._testing:
            return
        host_name = lang.t("dialogs", "srv_smtp_host", default="Outgoing (SMTP) server")
        host = self.smtp_host_field.GetValue().strip()
        if not host:
            self._refuse(
                "identity_field_required",
                f"Fill in {host_name}, or uncheck its box.",
                self.smtp_host_field, field=host_name,
            )
            return
        password = self.smtp_password_field.GetValue() or self._stored_password()
        if not password:
            self._refuse(
                "identity_password_required", "Enter the outgoing password.",
                self.smtp_password_field,
            )
            return
        login = self.smtp_login_field.GetValue().strip() or self.email_field.GetValue().strip()
        port = self.smtp_port_field.GetValue()
        encryption = self.smtp_encryption_choice.GetStringSelection() or "tls"
        self._testing = True
        self.test_smtp_button.Disable()
        speak(lang.t(
            "dialogs", "identity_smtp_testing", default="Testing the outgoing server.",
        ))

        def run():
            try:
                check_smtp_login(host, port, encryption, login, password)
                error = None
            except Exception as exc:  # noqa: BLE001 - reported below
                error = exc
            wx.CallAfter(self._test_finished, error)

        threading.Thread(target=run, daemon=True).start()

    def _test_finished(self, error):
        if not self:
            return  # the dialog closed while the test ran
        self._testing = False
        self.test_smtp_button.Enable(self.smtp_box.GetValue())
        title = lang.t("dialogs", "title_identity", default="Identity")
        if error is None:
            notice_toast.notify(
                self,
                lang.t(
                    "dialogs", "identity_smtp_ok",
                    default="The outgoing server accepted the sign-in.",
                ),
                title,
            )
        else:
            wx.MessageBox(
                lang.t(
                    "dialogs", "identity_smtp_failed",
                    default=f"The outgoing server test failed.\n\n{error}",
                    error=error,
                ),
                title, wx.OK | wx.ICON_ERROR,
            )

    def _on_ok(self, event):
        if not self.email_field.GetValue().strip():
            self._refuse(
                "identity_email_required", "Enter an email address.", self.email_field,
            )
            return
        for box, field, key, default in (
            (self.reply_to_box, self.reply_to_field, "acct_identity_reply_to", "Reply-To address"),
            (self.cc_box, self.cc_field, "acct_identity_cc", "Cc addresses"),
            (self.bcc_box, self.bcc_field, "acct_identity_bcc", "Bcc addresses"),
        ):
            if not box.GetValue():
                continue
            name = lang.t("dialogs", key, default=default)
            value = field.GetValue().strip()
            if not value:
                self._refuse(
                    "identity_field_required", f"Fill in {name}, or uncheck its box.",
                    field, field=name,
                )
                return
            if invalid_addresses(value):
                self._refuse(
                    "identity_field_invalid", f"{name} has an address that is not valid.",
                    field, field=name,
                )
                return
        if self.folders_box.GetValue() and not (
            self.drafts_field.GetValue().strip() or self.sent_field.GetValue().strip()
        ):
            self._refuse(
                "identity_folders_required",
                "Choose a Drafts folder, a Sent folder or both, or uncheck their box.",
                self.drafts_field,
            )
            return
        if self.smtp_box.GetValue():
            host_name = lang.t("dialogs", "srv_smtp_host", default="Outgoing (SMTP) server")
            if not self.smtp_host_field.GetValue().strip():
                self._refuse(
                    "identity_field_required", f"Fill in {host_name}, or uncheck its box.",
                    self.smtp_host_field, field=host_name,
                )
                return
            if not self.smtp_password_field.GetValue() and not self._has_stored_password:
                self._refuse(
                    "identity_password_required", "Enter the outgoing password.",
                    self.smtp_password_field,
                )
                return
        event.Skip()

    def result(self):
        """The identity as a raw entry; the caller runs it through
        _normalize_identities, which drops whatever is switched off."""
        plain = self.signature_field.GetValue()
        formatted = self.signature_html if plain == self._plain_synced else ""
        folders = self.folders_box.GetValue()
        return {
            "display_name": self.name_field.GetValue().strip(),
            "email": self.email_field.GetValue().strip(),
            "own_signature": self.signature_box.GetValue(),
            "signature": plain,
            "signature_html": formatted,
            "reply_to": self.reply_to_field.GetValue().strip() if self.reply_to_box.GetValue() else "",
            "always_cc": self.cc_field.GetValue().strip() if self.cc_box.GetValue() else "",
            "always_bcc": self.bcc_field.GetValue().strip() if self.bcc_box.GetValue() else "",
            "own_folders": folders,
            "drafts_folder": self.drafts_field.GetValue().strip() if folders else "",
            "sent_folder": self.sent_field.GetValue().strip() if folders else "",
            "own_smtp": self.smtp_box.GetValue(),
            "smtp_host": self.smtp_host_field.GetValue().strip(),
            "smtp_port": self.smtp_port_field.GetValue(),
            "smtp_encryption": self.smtp_encryption_choice.GetStringSelection() or "tls",
            "smtp_auth": self._smtp_auth,
            "smtp_login": self.smtp_login_field.GetValue().strip(),
            "identity_id": self._identity_id,
            "openpgp": dict(self.openpgp),
        }

    def password(self):
        """The outgoing password typed here, or empty to keep the stored
        one. Never part of result(), so it cannot reach accounts.json."""
        if not self.smtp_box.GetValue():
            return ""
        return self.smtp_password_field.GetValue()


class AccountSettingsDialog(wx.Dialog):
    def __init__(self, parent, account, folder_choices=None, is_default=False,
                 secret_lookup=None, config_dir=None):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_account_settings",
                default=f"Account and Identities Settings: {account.display_name}",
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
        # Reads a stored identity password for the identity dialog's
        # Test Outgoing Server button (AccountManager.stored_secret).
        self.secret_lookup = secret_lookup
        # Where a Microsoft sign-in made here is stored, and whether
        # one was: main_frame drops tokens nobody saved the dialog for.
        self._config_dir = config_dir
        self.signed_in_here = False
        self._original_method = getattr(account, "auth_method", "password")

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

        # Right after the login email, in tab order and on screen.
        self.method_choice = wx.RadioBox(
            self,
            label=lang.t("dialogs", "wiz_signin_method", default="Sign-in method"),
            choices=[
                lang.t("dialogs", "wiz_method_password", default="Password"),
                lang.t(
                    "dialogs", "wiz_method_microsoft",
                    default="Microsoft account (Outlook.com, Hotmail, Live, Microsoft 365)",
                ),
            ],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        self.method_choice.SetSelection(1 if self._original_method == "microsoft" else 0)
        self.method_choice.Bind(wx.EVT_RADIOBOX, self._on_method)

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
        self._password_label = password_label

        # In place of the password row for Microsoft sign-in.
        self.signin = MicrosoftSignInPanel(self)
        self.signin.set_handlers(
            save_record=self._save_signin,
            login_hint=lambda: self.login_field.GetValue().strip(),
            on_signed_in=self._on_signed_in,
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
        # OpenPGP (stage 3): this address's End-to-End Encryption, right
        # after the address it is for, held here until Save.
        self.openpgp = dict(getattr(account, "openpgp", None) or {})
        self.e2e_button = wx.Button(
            self, label=lang.control_label("acct_e2e_button", "End-to-End Encr&yption..."),
        )
        self.e2e_button.Bind(wx.EVT_BUTTON, self._on_e2e)

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
        # Outgoing passwords typed for identities, {identity_id: password},
        # held until OK like the account password, never in the entries.
        self.identity_passwords = {}
        # Identities that had their own outgoing server when the dialog
        # opened: a stored password exists for each, and any no longer
        # using one on OK has its password deleted (apply_to_account).
        self._original_smtp_ids = {
            entry["identity_id"] for entry in account.own_smtp_identities()
        }
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
        # The plain text the formatted version above was last in step
        # with. When the box is edited by hand past this, the formatted
        # version is stale and is dropped on Save, so the composer uses
        # what was typed; see apply_to_account. Read back from the box
        # itself, so line endings compare like with like.
        self._plain_synced = self.signature_field.GetValue()
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

        form = wx.FlexGridSizer(6, 2, 8, 8)
        form.AddGrowableCol(1)
        self._login_form = form
        form.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.name_field, 1, wx.EXPAND)
        form.Add(login_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.login_field, 1, wx.EXPAND)
        form.Add((0, 0))
        form.Add(self.method_choice, 1, wx.EXPAND)
        form.Add(password_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.password_field, 1, wx.EXPAND)
        form.Add((0, 0))
        form.Add(self.signin, 1, wx.EXPAND)
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
        sizer.Add(self.e2e_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
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
        # Save, not OK: this button is what stores every change made
        # here, the signature included, and should say so.
        save_button = self.FindWindowById(wx.ID_OK, self)
        if save_button is not None:
            save_button.SetLabel(lang.control_label("btn_save", "&Save"))
        # Cancel, Escape and closing the window all arrive here as the
        # Cancel button, so one handler guards all three.
        self.Bind(wx.EVT_BUTTON, self._on_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_BUTTON, self._on_save, id=wx.ID_OK)

        fit_dialog(self, sizer)
        # Sized with every row shown, so either method fits; then the
        # rows the account's method does not use are hidden.
        self._apply_method()
        self._show_signin_state()
        self._refresh_identities_list()
        self._opened_state = self._state()

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

    # Sizer position of the spacer beside the sign-in controls in the
    # login form.
    _SIGNIN_SPACER = 8

    def auth_method(self):
        choice = getattr(self, "method_choice", None)
        if choice is None:  # a dialog built without one, as tests do
            return getattr(self.account, "auth_method", "password")
        return "microsoft" if choice.GetSelection() == 1 else "password"

    def _on_method(self, _event):
        self._apply_method()
        if self.auth_method() == "microsoft":
            # Microsoft sign-in always uses Microsoft's own servers.
            self.imap_host_field.SetValue(MICROSOFT_SERVERS["imap_host"])
            self.imap_port_field.SetValue(MICROSOFT_SERVERS["imap_port"])
            self.imap_encryption_choice.SetStringSelection(MICROSOFT_SERVERS["imap_encryption"])
            self.smtp_host_field.SetValue(MICROSOFT_SERVERS["smtp_host"])
            self.smtp_port_field.SetValue(MICROSOFT_SERVERS["smtp_port"])
            self.smtp_encryption_choice.SetStringSelection(MICROSOFT_SERVERS["smtp_encryption"])

    def _apply_method(self):
        """Hides the rows the chosen method does not use, so Tab never
        steps over them: the password for Microsoft sign-in, the
        sign-in controls for a password."""
        microsoft = self.auth_method() == "microsoft"
        form = self._login_form
        form.Show(self._password_label, not microsoft)
        form.Show(self.password_field, not microsoft)
        form.Show(self._SIGNIN_SPACER, microsoft)
        form.Show(self.signin, microsoft)
        self.Layout()

    def _show_signin_state(self):
        if self._original_method != "microsoft" or not self._config_dir:
            return
        account_id = self.account.account_id
        if ms_oauth.needs_sign_in(self._config_dir, account_id):
            self.signin.show_needs_sign_in()
        else:
            self.signin.show_existing(ms_oauth.signed_in_username(self._config_dir, account_id))

    def _save_signin(self, record):
        if not self._config_dir:
            raise RuntimeError("There is no place to store the sign-in.")
        ms_oauth.save_record(self._config_dir, self.account.account_id, record)
        self.signed_in_here = True

    def _on_signed_in(self, record):
        username = str(record.get("username") or "").strip()
        if username:
            self.login_field.SetValue(username)

    def _can_save(self):
        """Refuses to switch a password account to Microsoft sign-in
        before it has signed in. An account already using Microsoft
        can always be saved, even while its sign-in is needed again."""
        original = getattr(self, "_original_method", "password")
        if (self.auth_method() == "microsoft" and original != "microsoft"
                and not self.signin.is_signed_in()):
            wx.MessageBox(
                lang.t(
                    "errors", "acct_ms_sign_in_first",
                    default=(
                        "Sign in with Microsoft before saving, or choose Password "
                        "as the sign-in method."
                    ),
                ),
                self.GetTitle(),
                wx.OK | wx.ICON_WARNING,
                self,
            )
            return False
        return True

    def _on_save(self, event):
        if self._can_save():
            event.Skip()

    def _state(self):
        """Everything this dialog can change, for telling whether
        anything has been. The password counts: typing one is a
        change even though the field starts empty."""
        return (
            self.name_field.GetValue(),
            self.login_field.GetValue(),
            self.password_field.GetValue(),
            self.auth_method(),
            self.identity_field.GetValue(),
            # Whole entries, so changing only an identity's own
            # settings still counts as a change.
            tuple(tuple(sorted(entry.items())) for entry in self.identities),
            tuple(sorted(self.identity_passwords.items())),
            self.signature_field.GetValue(),
            self.signature_html,
            tuple(sorted(self.openpgp.items())),
            self.junk_folder_field.GetValue(),
            self.disable_account_checkbox.GetValue(),
            self.auto_check_checkbox.GetValue(),
            self.default_account_checkbox.GetValue(),
            self.mute_account_checkbox.GetValue(),
            self.imap_host_field.GetValue(),
            self.imap_port_field.GetValue(),
            self.imap_encryption_choice.GetStringSelection(),
            self.smtp_host_field.GetValue(),
            self.smtp_port_field.GetValue(),
            self.smtp_encryption_choice.GetStringSelection(),
        )

    def _on_cancel(self, event):
        """Leaving with unsaved changes asks first. The signature
        editor's own Save only hands the signature back to this dialog,
        so without this question an edited signature could be thrown
        away by one Escape with nothing said."""
        if self._state() == self._opened_state:
            event.Skip()
            return
        dialog = wx.MessageDialog(
            self,
            lang.t(
                "dialogs", "acct_unsaved_q",
                default="Save the changes to this account?",
            ),
            self.GetTitle(),
            wx.YES_NO | wx.CANCEL | wx.ICON_WARNING,
        )
        dialog.SetYesNoCancelLabels(
            lang.control_label("btn_save", "&Save"),
            lang.control_label("comp_btn_discard", "&Discard"),
            lang.control_label("comp_btn_keep_editing", "&Keep editing"),
        )
        try:
            choice = dialog.ShowModal()
        finally:
            dialog.Destroy()
        if choice == wx.ID_YES:
            if self._can_save():
                self.EndModal(wx.ID_OK)
        elif choice == wx.ID_NO:
            self.EndModal(wx.ID_CANCEL)
        # Keep editing: the dialog stays open.

    def _on_edit_signature(self, event):
        """Opens the formatted signature editor. What comes back is
        held on this dialog, not written to the account: Cancel here
        still discards it, like every other field, after asking."""
        dialog = SignatureEditDialog(
            self, self.signature_html, self.signature_field.GetValue(),
            account_label=self.account.identity_email,
        )
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.signature_html = dialog.signature_html
                # The plain box always shows the text alternative, so
                # the two can never drift apart without being seen.
                self.signature_field.SetValue(dialog.signature_text)
                self._plain_synced = self.signature_field.GetValue()
        finally:
            dialog.Destroy()

    def _on_e2e(self, event):
        import openpgp_compose

        address = self.identity_field.GetValue().strip() or self.login_field.GetValue().strip()
        result = openpgp_compose.edit_settings(self, address, self.openpgp)
        if result is not None:
            self.openpgp = result

    def _identity_dialog(self, entry):
        identity_id = (entry or {}).get("identity_id", "")
        has_stored = bool(identity_id) and (
            identity_id in self._original_smtp_ids or identity_id in self.identity_passwords
        )
        return _IdentityEditDialog(
            self, entry, folder_choices=self.folder_choices, account=self.account,
            secret_lookup=self.secret_lookup, has_stored_password=has_stored,
        )

    def _accepted_identity(self, dialog):
        """The dialog's identity, normalized, with its typed password
        held on this dialog under the identity's id."""
        entries = _normalize_identities([dialog.result()])
        if not entries:
            return None
        entry = entries[0]
        password = dialog.password()
        if password and entry.get("identity_id"):
            self.identity_passwords[entry["identity_id"]] = password
        return entry

    def _on_new_identity(self, event):
        dialog = self._identity_dialog(None)
        if dialog.ShowModal() == wx.ID_OK:
            entry = self._accepted_identity(dialog)
            if entry is not None:
                self.identities.append(entry)
                self._refresh_identities_list()
                self.identities_list.SetSelection(len(self.identities) - 1)
                self._sync_identity_buttons()
        dialog.Destroy()

    def _on_edit_identity(self, event):
        index = self.identities_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        entry = self.identities[index]
        dialog = self._identity_dialog(entry)
        if dialog.ShowModal() == wx.ID_OK:
            updated = self._accepted_identity(dialog)
            if updated is not None:
                self.identities[index] = updated
                self._refresh_identities_list()
                self.identities_list.SetSelection(index)
        dialog.Destroy()

    def _on_remove_identity(self, event):
        index = self.identities_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        removed = self.identities.pop(index)
        self.identity_passwords.pop(removed.get("identity_id", ""), None)
        self._refresh_identities_list()

    # Every attribute apply_to_account writes, named once so the
    # snapshot and the restore cannot drift away from the
    # assignments themselves.
    _EDITED_FIELDS = (
        "display_name", "login_email", "password", "auth_method", "identity_email",
        "imap_host", "imap_port", "imap_encryption",
        "smtp_host", "smtp_port", "smtp_encryption",
        "signature", "signature_html", "junk_folder", "notifications_muted",
        "extra_identities", "enabled", "auto_check",
        "identity_passwords", "identity_secret_removals", "openpgp",
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
        self.account.auth_method = self.auth_method()
        self.account.password = (
            "" if self.account.auth_method == "microsoft" else self.password_field.GetValue()
        )
        self.account.identity_email = self.identity_field.GetValue().strip() or self.account.login_email
        self.account.imap_host = self.imap_host_field.GetValue().strip()
        self.account.imap_port = self.imap_port_field.GetValue()
        self.account.imap_encryption = self.imap_encryption_choice.GetStringSelection()
        self.account.smtp_host = self.smtp_host_field.GetValue().strip()
        self.account.smtp_port = self.smtp_port_field.GetValue()
        self.account.smtp_encryption = self.smtp_encryption_choice.GetStringSelection()
        plain = self.signature_field.GetValue()
        if plain != self._plain_synced:
            # Typed into by hand since the formatted version was last
            # in step with it: that version is stale, and the composer
            # would prefer it over what was typed. Dropped, so the
            # plain text is what goes out. An emptied box means no
            # signature at all.
            self.signature_html = ""
        self.account.signature = plain
        self.account.signature_html = self.signature_html
        junk_value = self.junk_folder_field.GetValue().strip()
        self.account.junk_folder = "" if junk_value in ("", AUTOMATIC_JUNK_LABEL) else junk_value
        self.account.notifications_muted = self.mute_account_checkbox.GetValue()
        self.account.extra_identities = _normalize_identities(self.identities)
        self.account.openpgp = _normalize_openpgp(self.openpgp)
        # Typed identity passwords, and the stored ones of identities
        # that no longer have their own outgoing server. update_account
        # stores or deletes them and empties both.
        kept = {entry["identity_id"] for entry in self.account.own_smtp_identities()}
        self.account.identity_passwords = {
            key: value for key, value in self.identity_passwords.items() if key in kept
        }
        self.account.identity_secret_removals = [
            self.account.identity_secret_key({"identity_id": identity_id})
            for identity_id in sorted(self._original_smtp_ids - kept)
        ]
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
