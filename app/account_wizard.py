"""
Account setup wizard. Collects a display name and login email first,
matches the login email's domain against the bundled provider
presets to pre-fill server settings, lets those be edited, then saves
the account. The From/identity address defaults to the login email
here; changing it separately happens afterward in Account Settings,
matching the Disroot-plus-custom-domain flow.
"""

import uuid

import wx

import lang
import ms_oauth
from accessible import make_protected_field, set_field_protected, wrap_text
import wx.adv

from account_manager import Account
from himalaya_worker import HimalayaWorker
from ms_signin_panel import MicrosoftSignInPanel
from provider_presets import (
    MICROSOFT_SERVERS, preset_for_domain, preset_is_unsupported, preset_oauth,
)

# EM_SETPASSWORDCHAR, and the bullet Windows masks with on a Unicode
# build. Sending it with a character of 0 unmasks the field.
_EM_SETPASSWORDCHAR = 0x00CC
_PASSWORD_BULLET = 0x25CF


def _set_password_masking(field, masked):
    """Shows or hides what is typed in a password field, in place.

    wx offers no way to drop wx.TE_PASSWORD after a control exists --
    SetWindowStyleFlag does not reach the native edit control on
    MSW -- and the usual workaround, two stacked fields swapped
    between, means an extra control in the tab order and a second
    thing for a screen reader to trip over on a page that already had
    a focus problem. One field and the message Windows provides for
    exactly this is the smaller change.

    Returns False if the call could not be made, so the caller can
    say so rather than leaving a checkbox that silently does nothing.
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_ssize_t,
        ]
        user32.SendMessageW(
            ctypes.c_void_p(field.GetHandle()), _EM_SETPASSWORDCHAR,
            _PASSWORD_BULLET if masked else 0, 0,
        )
    except Exception:  # noqa: BLE001
        return False
    field.Refresh()
    return True


_GOOGLE_APP_PASSWORDS_URL = "https://support.google.com/accounts/answer/185833"

# Provider notes (provider_presets) by their English text: the table is
# built when the module loads, before the language is chosen, so each
# note is translated where it is shown.
_PRESET_NOTE_KEYS = {
    'Google requires an app password or OAuth2, not your normal password.': 'preset_note_google',
    'Outlook.com, Hotmail, Live and MSN accounts sign in with a Microsoft account. Choose Microsoft account as the sign-in method on the Login page.': 'preset_note_outlook',
    'Yahoo requires an app password.': 'preset_note_yahoo',
    'Apple requires an app-specific password.': 'preset_note_apple',
    'Fastmail requires an app password.': 'preset_note_fastmail',
    'ProtonMail requires the ProtonMail Bridge app running locally.': 'preset_note_proton',
}


def _preset_note(preset):
    note = preset.get("note") or ""
    key = _PRESET_NOTE_KEYS.get(note)
    return lang.t("dialogs", key, default=note) if key else note

_WELCOME_NOTICE = (
    "Gmail. Your normal Google password will be refused, but an app "
    "password works. Two-step verification has to be on first. The "
    "button below opens Google's instructions.\n\n"
    "Outlook.com, Hotmail, Live and Microsoft 365. These sign in with "
    "your Microsoft account. On the Login page, choose Microsoft "
    "account, then sign in with your browser or with a code.\n\n"
    "Other providers. A normal password or an app password usually "
    "works, and your provider's help pages list the server settings."
)


class WelcomePage(wx.adv.WizardPageSimple):
    """The gateway page, shown before anything is collected.

    It says up front what each kind of provider needs, before a name,
    an address and a password are typed: an app password for Gmail,
    a Microsoft account for Outlook.com and Microsoft 365.

    The notice is a read-only multiline field rather than static
    text: a screen reader can arrow through it line by line, which
    static text does not allow.
    """

    def __init__(self, parent):
        super().__init__(parent)

        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(
            self,
            label=lang.t("dialogs", "wiz_before_you_start", default="Before you start"),
        )
        heading.SetFont(heading.GetFont().Bold())
        sizer.Add(heading, 0, wx.ALL, 8)

        self.notice = wx.TextCtrl(
            self, value=lang.t("dialogs", "wiz_welcome_notice", default=_WELCOME_NOTICE),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.notice.SetName(
            lang.t("dialogs", "wiz_notice_name", default="Important notice")
        )
        sizer.Add(self.notice, 1, wx.EXPAND | wx.ALL, 8)

        self.google_button = wx.Button(
            self,
            label=lang.control_label(
                "wiz_google_instructions",
                "Open &Google app password instructions",
            ),
        )
        self.google_button.Bind(wx.EVT_BUTTON, self._on_google)
        sizer.Add(self.google_button, 0, wx.LEFT | wx.RIGHT, 8)

        hint = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "wiz_note_app_password",
                default=(
                    "If you already have an app password created for ZBox, "
                    "press Next to continue."
                ),
            ),
        )
        wrap_text(hint)
        sizer.Add(hint, 0, wx.ALL, 8)

        self.SetSizer(sizer)

    def _on_google(self, _event):
        wx.LaunchDefaultBrowser(_GOOGLE_APP_PASSWORDS_URL)


class IdentityPage(wx.adv.WizardPageSimple):
    def __init__(self, parent):
        super().__init__(parent)

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=lang.t("dialogs", "wiz_intro", default="Your name and email address"),
        )
        intro.SetFont(intro.GetFont().Bold())
        sizer.Add(intro, 0, wx.ALL, 8)

        name_label = wx.StaticText(
            self, label=lang.t("dialogs", "form_your_name", default="Your name")
        )
        self.name_field = wx.TextCtrl(self)
        self.name_field.SetName(
            lang.t("dialogs", "form_your_name", default="Your name")
        )

        email_label = wx.StaticText(
            self, label=lang.t("dialogs", "form_email_address", default="Email address")
        )
        self.email_field = wx.TextCtrl(self)
        self.email_field.SetName(
            lang.t("dialogs", "form_email_address", default="Email address")
        )

        form = wx.FlexGridSizer(2, 2, 8, 8)
        form.AddGrowableCol(1)
        form.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.name_field, 1, wx.EXPAND)
        form.Add(email_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.email_field, 1, wx.EXPAND)

        sizer.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)


class LoginPage(wx.adv.WizardPageSimple):
    def __init__(self, parent):
        super().__init__(parent)

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "wiz_note_login",
                default=(
                    "Login credentials.\n"
                    "If your login email is different from the address above, "
                    "for example your provider account is login@example.net but "
                    "you want to send and receive as you@example.org, enter "
                    "the login email here. You can set the custom send address "
                    "afterward in Account and Identities Settings."
                ),
            ),
        )
        wrap_text(intro)
        sizer.Add(intro, 0, wx.ALL, 8)

        # How this account signs in. Microsoft is chosen for the
        # Microsoft presets when the page opens, and can be chosen for
        # any other address, such as a Microsoft 365 work domain.
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
        self.method_choice.Bind(wx.EVT_RADIOBOX, self._on_method)
        sizer.Add(self.method_choice, 0, wx.EXPAND | wx.ALL, 8)

        self.same_as_identity = wx.CheckBox(
            self,
            label=lang.t(
                "dialogs", "wiz_login_same",
                default="Login email is the same as the email address above",
            ),
        )
        self.same_as_identity.SetValue(True)
        self.same_as_identity.Bind(wx.EVT_CHECKBOX, self._on_toggle_same)
        sizer.Add(self.same_as_identity, 0, wx.ALL, 8)

        login_label = wx.StaticText(
            self, label=lang.t("dialogs", "form_login_email", default="Login email")
        )
        self.login_label = login_label
        self.login_email_field = wx.TextCtrl(self)
        self.login_email_field.SetName(
            lang.t("dialogs", "form_login_email", default="Login email")
        )

        password_label = wx.StaticText(
            self, label=lang.t("dialogs", "form_password", default="Password")
        )
        self.password_field = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self._password_accessible = make_protected_field(
            self.password_field, "Password"
        )

        form = wx.FlexGridSizer(2, 2, 8, 8)
        self.form = form
        form.AddGrowableCol(1)
        form.Add(login_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.login_email_field, 1, wx.EXPAND)
        form.Add(password_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.password_field, 1, wx.EXPAND)
        self.password_label = password_label

        sizer.Add(form, 0, wx.EXPAND | wx.ALL, 8)

        self.show_password = wx.CheckBox(
            self, label=lang.control_label("wiz_show_password", "&Show password")
        )
        self.show_password.Bind(wx.EVT_CHECKBOX, self._on_show_password)
        sizer.Add(self.show_password, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # Shown in place of the password for Microsoft sign-in.
        self.signin = MicrosoftSignInPanel(self)
        sizer.Add(self.signin, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)

        # Hidden rather than disabled while the login email is the
        # same as the address above. Disabling left a control sitting
        # in the middle of the tab order that Tab has to step over,
        # and stepping over it is what produced the reported bug:
        # the first Tab from the checkbox onto the password field was
        # announced as "password checked", the new field's name with
        # the checkbox's state still attached, and only came out
        # right after tabbing away and shift-tabbing back. A hidden
        # control is not in the tab order at all, so Tab goes
        # straight from the checkbox to the password field with
        # nothing to skip.
        self._show_login_email(False)
        self._apply_method()

    def _show_login_email(self, show):
        self.form.Show(self.login_label, show)
        self.form.Show(self.login_email_field, show)
        self.login_email_field.Enable(show)
        self.Layout()

    def _on_show_password(self, _event):
        wanted = self.show_password.GetValue()
        if _set_password_masking(self.password_field, not wanted):
            set_field_protected(
                self.password_field, self._password_accessible,
                "Password", not wanted,
            )
            return
        self.show_password.SetValue(False)
        wx.MessageBox(
            lang.t(
                "dialogs", "wizard_password_not_shown",
                default="The password cannot be shown on this system. It is still "
                "typed and saved normally.",
            ),
            lang.t("dialogs", "title_show_password", default="Show Password"),
            wx.OK | wx.ICON_INFORMATION,
        )

    def _on_toggle_same(self, event):
        checked = self.same_as_identity.GetValue()
        self._show_login_email(not checked)
        if not checked:
            self.login_email_field.SetFocus()

    def effective_login_email(self, identity_email):
        if self.same_as_identity.GetValue():
            return identity_email
        return self.login_email_field.GetValue().strip()

    def auth_method(self):
        return "microsoft" if self.method_choice.GetSelection() == 1 else "password"

    def set_auth_method(self, method):
        self.method_choice.SetSelection(1 if method == "microsoft" else 0)
        self._apply_method()

    def password_value(self):
        """The typed password, or nothing for Microsoft sign-in."""
        if self.auth_method() == "microsoft":
            return ""
        return self.password_field.GetValue()

    def method_summary(self):
        # English, like every other line of the Finish page summary.
        return "Microsoft account" if self.auth_method() == "microsoft" else "Password"

    def _on_method(self, _event):
        self._apply_method()

    def _apply_method(self):
        """Hides what the chosen method does not use, so Tab never
        steps over it: the password row and Show password for
        Microsoft sign-in, the sign-in controls for a password."""
        microsoft = self.auth_method() == "microsoft"
        self.form.Show(self.password_label, not microsoft)
        self.form.Show(self.password_field, not microsoft)
        self.password_field.Enable(not microsoft)
        sizer = self.GetSizer()
        sizer.Show(self.show_password, not microsoft)
        sizer.Show(self.signin, microsoft)
        self.Layout()


class ServerPage(wx.adv.WizardPageSimple):
    ENCRYPTION_CHOICES = ["tls", "start-tls", "none"]

    def __init__(self, parent):
        super().__init__(parent)

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "wiz_note_servers",
                default=(
                    "Server settings, pre-filled where the domain is "
                    "recognized. Edit anything that needs to be different."
                ),
            ),
        )
        wrap_text(intro)
        sizer.Add(intro, 0, wx.ALL, 8)

        self.preset_note = wx.StaticText(self, label="")
        wrap_text(self.preset_note)
        sizer.Add(self.preset_note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.unsupported = False

        form = wx.FlexGridSizer(6, 2, 8, 8)
        form.AddGrowableCol(1)

        self.imap_host_field = wx.TextCtrl(self)
        self.imap_host_field.SetName(
            lang.t("dialogs", "srv_imap_host", default="Incoming (IMAP) server")
        )
        self.imap_port_field = wx.SpinCtrl(self, min=1, max=65535, initial=993)
        self.imap_port_field.SetName(
            lang.t("dialogs", "srv_imap_port", default="Incoming port")
        )
        self.imap_encryption_choice = wx.Choice(self, choices=self.ENCRYPTION_CHOICES)
        self.imap_encryption_choice.SetName(
            lang.t("dialogs", "srv_imap_encryption", default="Incoming encryption")
        )
        self.imap_encryption_choice.SetSelection(0)

        self.smtp_host_field = wx.TextCtrl(self)
        self.smtp_host_field.SetName(
            lang.t("dialogs", "srv_smtp_host", default="Outgoing (SMTP) server")
        )
        self.smtp_port_field = wx.SpinCtrl(self, min=1, max=65535, initial=465)
        self.smtp_port_field.SetName(
            lang.t("dialogs", "srv_smtp_port", default="Outgoing port")
        )
        self.smtp_encryption_choice = wx.Choice(self, choices=self.ENCRYPTION_CHOICES)
        self.smtp_encryption_choice.SetName(
            lang.t("dialogs", "srv_smtp_encryption", default="Outgoing encryption")
        )
        self.smtp_encryption_choice.SetSelection(0)

        form.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "srv_imap_host", default="Incoming (IMAP) server"
                ),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.imap_host_field, 1, wx.EXPAND)
        form.Add(
            wx.StaticText(
                self,
                label=lang.t("dialogs", "srv_imap_port", default="Incoming port"),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.imap_port_field, 1, wx.EXPAND)
        form.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "srv_imap_encryption", default="Incoming encryption"
                ),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.imap_encryption_choice, 1, wx.EXPAND)

        form.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "srv_smtp_host", default="Outgoing (SMTP) server"
                ),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.smtp_host_field, 1, wx.EXPAND)
        form.Add(
            wx.StaticText(
                self,
                label=lang.t("dialogs", "srv_smtp_port", default="Outgoing port"),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.smtp_port_field, 1, wx.EXPAND)
        form.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "srv_smtp_encryption", default="Outgoing encryption"
                ),
            ),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        form.Add(self.smtp_encryption_choice, 1, wx.EXPAND)

        sizer.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

    def apply_preset(self, preset):
        self.unsupported = preset_is_unsupported(preset)

        if preset is None:
            self.preset_note.SetLabel(
                lang.t(
                    "dialogs", "wiz_preset_none",
                    default=(
                        "No preset found for this domain. Enter your provider's "
                        "server settings manually; your provider's help pages "
                        "will list them."
                    ),
                )
            )
            return

        label = lang.t(
            "dialogs", "wiz_preset_applied",
            default=f"Preset applied for {preset['display_name']}.",
            name=preset["display_name"],
        )
        note = _preset_note(preset)
        if self.unsupported:
            label = lang.t(
                "dialogs", "wiz_not_supported",
                default="NOT SUPPORTED: {name}. {note} This account cannot be added in ZBox.",
                name=preset["display_name"], note=note,
            )
        elif note:
            label += " " + note
        self.preset_note.SetLabel(label)
        self.imap_host_field.SetValue(preset["imap_host"])
        self.imap_port_field.SetValue(preset["imap_port"])
        self.imap_encryption_choice.SetStringSelection(preset["imap_encryption"])
        self.smtp_host_field.SetValue(preset["smtp_host"])
        self.smtp_port_field.SetValue(preset["smtp_port"])
        self.smtp_encryption_choice.SetStringSelection(preset["smtp_encryption"])


def _servers_note():
    return lang.t(
        "dialogs", "wiz_ms_servers",
        default=(
            "Microsoft sign-in. The server settings for Outlook.com, Hotmail, "
            "Live and Microsoft 365 are filled in."
        ),
    )


def _apply_microsoft(page):
    """Fills a ServerPage for Microsoft sign-in, whatever the preset
    said about passwords."""
    page.unsupported = False
    page.preset_note.SetLabel(_servers_note())
    page.imap_host_field.SetValue(MICROSOFT_SERVERS["imap_host"])
    page.imap_port_field.SetValue(MICROSOFT_SERVERS["imap_port"])
    page.imap_encryption_choice.SetStringSelection(MICROSOFT_SERVERS["imap_encryption"])
    page.smtp_host_field.SetValue(MICROSOFT_SERVERS["smtp_host"])
    page.smtp_port_field.SetValue(MICROSOFT_SERVERS["smtp_port"])
    page.smtp_encryption_choice.SetStringSelection(MICROSOFT_SERVERS["smtp_encryption"])


class FinishPage(wx.adv.WizardPageSimple):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.summary = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(400, 200)
        )
        self.summary.SetName(
            lang.t("dialogs", "wiz_summary_name", default="Account summary")
        )
        sizer.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "wiz_review",
                    default="Review, then click Finish to save this account.",
                ),
            ),
            0, wx.ALL, 8,
        )
        sizer.Add(self.summary, 1, wx.EXPAND | wx.ALL, 8)
        # Audit finding 10: lets a wrong password or host be caught
        # here, against the account's real server settings, instead
        # of only surfacing later as an opaque Himalaya error on the
        # first fetch after the account is already saved.
        self.test_button = wx.Button(
            self, label=lang.control_label("wiz_test_connection", "Test &Connection")
        )
        sizer.Add(self.test_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(sizer)


class AccountWizard:
    """
    Wraps wx.adv.Wizard. Call run() to show it; returns the new
    Account on success, or None if cancelled.
    """

    def __init__(self, parent, account_manager):
        self.wizard = wx.adv.Wizard(
            parent,
            title=lang.t("dialogs", "title_new_account", default="New Account"),
        )
        if lang.is_rtl():
            self.wizard.SetLayoutDirection(wx.Layout_RightToLeft)
        # Taller than before: the login page now also holds the
        # sign-in method and, for Microsoft, the sign-in controls.
        self.wizard.SetPageSize(wx.Size(500, 440))
        self.account_manager = account_manager

        # Test Connection runs one real Himalaya subprocess (a full
        # IMAP login, up to 45 seconds against a wrong or unreachable
        # host), so it must not run on the GUI thread -- it used to,
        # and a bad server froze the whole app, "not responding" to
        # Windows and silent to NVDA/JAWS, with no way to cancel.
        #
        # Its own worker rather than main_frame's: the frame's
        # interactive queue also carries the 20-second auto-refresh,
        # so a slow test would stack those up behind it, and the
        # low-priority queue can be mid-way through a multi-minute
        # offline sync, which would leave the button looking dead.
        # One extra daemon thread, only while a wizard is open, and
        # only ever a handful of wizards per session.
        self._worker = HimalayaWorker()
        # Bumped on every test so a result arriving after the wizard
        # moved on (or was cancelled) is recognised as stale and
        # dropped rather than reported over whatever came after it.
        self._test_seq = 0
        # One id for the whole wizard, so a Microsoft sign-in, the
        # Test button and the account finally added all share it: the
        # tokens are stored under it before the account exists.
        self._account_id = uuid.uuid4().hex[:12]
        self._suggested_for = None

        self.welcome_page = WelcomePage(self.wizard)
        self.identity_page = IdentityPage(self.wizard)
        self.login_page = LoginPage(self.wizard)
        self.server_page = ServerPage(self.wizard)
        self.finish_page = FinishPage(self.wizard)

        wx.adv.WizardPageSimple.Chain(self.welcome_page, self.identity_page)
        wx.adv.WizardPageSimple.Chain(self.identity_page, self.login_page)
        wx.adv.WizardPageSimple.Chain(self.login_page, self.server_page)
        wx.adv.WizardPageSimple.Chain(self.server_page, self.finish_page)

        self.wizard.Bind(wx.adv.EVT_WIZARD_PAGE_CHANGING, self._on_page_changing)
        self.wizard.Bind(wx.adv.EVT_WIZARD_PAGE_CHANGED, self._on_page_changed)
        self.finish_page.test_button.Bind(wx.EVT_BUTTON, self._on_test_connection)
        self.login_page.signin.set_handlers(
            save_record=self._save_signin,
            login_hint=self._login_hint,
            on_signed_in=self._on_signed_in,
        )

    def _save_signin(self, record):
        ms_oauth.save_record(self.account_manager.paths.config, self._account_id, record)

    def _login_hint(self):
        identity_email = self.identity_page.email_field.GetValue().strip()
        return self.login_page.effective_login_email(identity_email)

    def _on_signed_in(self, record):
        """The address Microsoft signed in becomes the login email,
        when it differs from the address typed on the first page."""
        username = str(record.get("username") or "").strip()
        identity_email = self.identity_page.email_field.GetValue().strip()
        page = self.login_page
        if not username or username.lower() == identity_email.lower():
            return
        page.same_as_identity.SetValue(False)
        page._show_login_email(True)
        page.login_email_field.SetValue(username)

    def _suggest_method(self):
        """Picks the sign-in method from the address's preset, once
        per address, and never over a sign-in already done."""
        identity_email = self.identity_page.email_field.GetValue().strip().lower()
        if identity_email == self._suggested_for or self.login_page.signin.is_signed_in():
            return
        self._suggested_for = identity_email
        wanted = preset_oauth(preset_for_domain(identity_email))
        self.login_page.set_auth_method("microsoft" if wanted == "microsoft" else "password")

    def _on_page_changed(self, event):
        """Puts focus on the notice whenever the gateway page is
        shown, so a screen reader reads the text rather than landing
        on the Next button with the page unread.

        Also relabels the wizard's own Back and Next/Finish buttons
        from the language file. wxWidgets writes its English "< Back",
        "Next >" and "Finish" onto them on every page change, just
        before this event, and never reads ZBox's language files, so
        a translation only sticks if it is put back here each time."""
        if event.GetPage() is self.welcome_page:
            self.welcome_page.notice.SetFocus()
        if event.GetPage() is self.login_page:
            self._suggest_method()
        self._localize_wizard_buttons(event.GetPage())
        event.Skip()

    def _localize_wizard_buttons(self, page):
        back = self.wizard.FindWindowById(wx.ID_BACKWARD)
        if back is not None:
            back.SetLabel(lang.control_label("wiz_btn_back", "< &Back"))
        forward = self.wizard.FindWindowById(wx.ID_FORWARD)
        if forward is not None:
            if page is not None and page.GetNext() is None:
                forward.SetLabel(lang.control_label("wiz_btn_finish", "&Finish"))
            else:
                forward.SetLabel(lang.control_label("wiz_btn_next", "&Next >"))

    def _on_page_changing(self, event):
        page = event.GetPage()
        going_forward = event.GetDirection()

        if page is self.identity_page and going_forward:
            if "@" not in self.identity_page.email_field.GetValue():
                wx.MessageBox(
                    lang.t(
                        "dialogs", "wizard_email_required",
                        default="Enter a valid email address before continuing.",
                    ),
                    lang.t(
                        "dialogs", "title_missing_information",
                        default="Missing information",
                    ),
                    wx.OK | wx.ICON_WARNING,
                )
                event.Veto()
                return

        if page is self.login_page and going_forward:
            identity_email = self.identity_page.email_field.GetValue().strip()
            login_email = self.login_page.effective_login_email(identity_email)
            if self.login_page.auth_method() == "microsoft":
                if not self.login_page.signin.is_signed_in():
                    wx.MessageBox(
                        lang.t(
                            "errors", "wizard_ms_sign_in_first",
                            default=(
                                "Sign in with Microsoft before continuing. Use one "
                                "of the two sign-in buttons on this page."
                            ),
                        ),
                        lang.t(
                            "dialogs", "title_missing_information",
                            default="Missing information",
                        ),
                        wx.OK | wx.ICON_WARNING,
                    )
                    event.Veto()
                    return
                _apply_microsoft(self.server_page)
            else:
                preset = preset_for_domain(login_email)
                self.server_page.apply_preset(preset)

        if page is self.server_page and going_forward:
            if self.server_page.unsupported:
                wx.MessageBox(
                    lang.t(
                        "errors", "wizard_provider_unsupported",
                        default="ZBox cannot sign in to this provider. The note on "
                        "this page says why. Go back and use a different email "
                        "address, or cancel.",
                    ),
                    lang.t(
                        "dialogs", "title_unsupported_provider",
                        default="Unsupported provider",
                    ),
                    wx.OK | wx.ICON_ERROR,
                )
                event.Veto()
                return
            self._update_summary()

    def _update_summary(self):
        identity_email = self.identity_page.email_field.GetValue().strip()
        login_email = self.login_page.effective_login_email(identity_email)
        lines = [
            f"Name: {self.identity_page.name_field.GetValue()}",
            f"Email address (From): {identity_email}",
            f"Login email: {login_email}",
            f"Sign-in: {self.login_page.method_summary()}",
            f"Incoming: {self.server_page.imap_host_field.GetValue()}"
            f":{self.server_page.imap_port_field.GetValue()}"
            f" ({self.server_page.imap_encryption_choice.GetStringSelection()})",
            f"Outgoing: {self.server_page.smtp_host_field.GetValue()}"
            f":{self.server_page.smtp_port_field.GetValue()}"
            f" ({self.server_page.smtp_encryption_choice.GetStringSelection()})",
        ]
        self.finish_page.summary.SetValue("\n".join(lines))

    def _build_account(self):
        identity_email = self.identity_page.email_field.GetValue().strip()
        login_email = self.login_page.effective_login_email(identity_email)
        return Account(
            account_id=self._account_id,
            auth_method=self.login_page.auth_method(),
            display_name=self.identity_page.name_field.GetValue().strip() or identity_email,
            login_email=login_email,
            identity_email=identity_email,
            password=self.login_page.password_value(),
            imap_host=self.server_page.imap_host_field.GetValue().strip(),
            imap_port=self.server_page.imap_port_field.GetValue(),
            imap_encryption=self.server_page.imap_encryption_choice.GetStringSelection(),
            smtp_host=self.server_page.smtp_host_field.GetValue().strip(),
            smtp_port=self.server_page.smtp_port_field.GetValue(),
            smtp_encryption=self.server_page.smtp_encryption_choice.GetStringSelection(),
        )

    def _on_test_connection(self, event):
        account = self._build_account()
        if not account.login_email or not account.imap_host:
            wx.MessageBox(
                lang.t(
                    "dialogs", "wizard_test_needs_server",
                    default="Enter a login email and incoming server before testing.",
                ),
                lang.t("dialogs", "title_test_connection", default="Test Connection"),
                wx.OK | wx.ICON_WARNING,
            )
            return

        self._test_seq += 1
        token = self._test_seq
        button = self.finish_page.test_button
        button.Disable()
        # The label, not a busy cursor: a busy cursor says nothing to
        # a screen reader, and BeginBusyCursor/EndBusyCursor cannot be
        # paired reliably once the result arrives asynchronously (the
        # wizard may be gone by then, leaving the cursor stuck).
        button.SetLabel(
            lang.t("dialogs", "wiz_testing", default="Testing connection...")
        )

        def work():
            return self.account_manager.test_connection(account)

        def finished(token=token):
            """Restores the button, or reports False if this result is
            stale or the wizard's widgets are already destroyed."""
            if token != self._test_seq:
                return False
            try:
                self.finish_page.test_button.SetLabel(
                    lang.control_label("wiz_test_connection", "Test &Connection")
                )
                self.finish_page.test_button.Enable()
            except RuntimeError:
                return False  # wizard closed while the test was running
            return True

        def on_success(envelopes):
            if not finished():
                return
            wx.MessageBox(
                lang.t(
                    "dialogs", "wizard_test_succeeded",
                    default=f"Connection succeeded. {len(envelopes)} message(s) found in Inbox.",
                    count=len(envelopes),
                ),
                lang.t("dialogs", "title_test_connection", default="Test Connection"),
                wx.OK | wx.ICON_INFORMATION,
            )

        def on_error(exc):
            if not finished():
                return
            wx.MessageBox(
                lang.t(
                    "errors", "wizard_test_failed",
                    default=f"Connection failed:\n\n{exc}",
                    error=exc,
                ),
                lang.t("dialogs", "title_test_connection", default="Test Connection"),
                wx.OK | wx.ICON_ERROR,
            )

        self._worker.submit(work, on_success, on_error)

    def run(self):
        # The first page raises no page-changed event, so focus is
        # queued instead: CallAfter runs inside the modal loop the
        # next line starts.
        wx.CallAfter(self.welcome_page.notice.SetFocus)
        completed = self.wizard.RunWizard(self.welcome_page)
        # Any Test Connection result still in flight belongs to a
        # wizard that is finished with: bump the sequence so its
        # callback drops instead of touching widgets on the way out.
        self._test_seq += 1
        self.login_page.signin.cancel()
        if not completed or self.login_page.auth_method() != "microsoft":
            # Tokens from a sign-in this wizard did not end up using.
            ms_oauth.delete_record(self.account_manager.paths.config, self._account_id)
        if not completed:
            return None
        return self._build_account()
