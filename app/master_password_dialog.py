"""
Master password dialogs.

Two of them, for the two moments the master password exists for:

MasterPasswordPrompt runs at startup on a computer that has never
opened this folder before. The stored secrets are sealed with a data
key wrapped for a different Windows account, and the master password
is the only thing that opens them here (see dpapi_secret_store).
Getting it right also records a DPAPI wrap for this machine, so this
dialog appears once per computer, not once per launch.

MasterPasswordDialog is Tools > Master Password: set one, change it,
or remove it.

Both are ordinary wx dialogs with real labels on every field, because
a password box a screen reader cannot name is a password box nobody
can fill in.
"""

import logging

import wx

import lang

from accessible import fit_dialog, wrap_text


def _password_field(parent, sizer, label_text, name):
    label = wx.StaticText(parent, label=label_text)
    sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
    field = wx.TextCtrl(parent, style=wx.TE_PASSWORD)
    field.SetName(name)
    sizer.Add(field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
    return field


class MasterPasswordPrompt(wx.Dialog):
    """Asks for the master password so this computer can read the
    stored account passwords."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_master_password_required",
                default="Master Password Required",
            ),
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)

        sizer = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "mp_note_intro",
                default=(
                    "These accounts were set up on another computer. "
                    "Enter the master password to unlock their saved "
                    "passwords here. You will only be asked once on this "
                    "computer.\n\n"
                    "Without it, each account has to be signed in to its "
                    "mail server again."
                ),
            ),
        )
        wrap_text(intro, 52)
        sizer.Add(intro, 0, wx.ALL, 10)

        self.password = _password_field(
            self, sizer, lang.control_label('mp_label_master', "&Master password:"), lang.t('dialogs', 'mp_name_master', default="Master password")
        )

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)
        self.password.SetFocus()

    def get_password(self):
        return self.password.GetValue()


class MasterPasswordDialog(wx.Dialog):
    """Tools > Master Password. Sets, changes or removes it."""

    def __init__(self, parent, master_is_set, required_note=None):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_master_password", default="Master Password",
            ),
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.master_is_set = master_is_set

        sizer = wx.BoxSizer(wx.VERTICAL)
        if required_note:
            note = wx.StaticText(self, label=required_note)
            note.SetFont(note.GetFont().Bold())
            wrap_text(note, 52)
            sizer.Add(note, 0, wx.ALL, 10)
        if master_is_set:
            explanation = (
                lang.t('dialogs', 'mp_explain_set', default="A master password is set. It is what lets this ZBox "
                "folder open its saved account passwords on a "
                "different computer.\n\n"
                "Leave both new password boxes empty and press OK to "
                "remove it. Removing it does not affect this "
                "computer, but the folder will no longer work on any "
                "other one without signing every account in again.")
            )
        else:
            explanation = (
                lang.t('dialogs', 'mp_explain_unset', default="No master password is set, so the saved account "
                "passwords can only be read on this computer and this "
                "Windows account. Copy the folder elsewhere and every "
                "account has to be signed in to its mail server "
                "again.\n\n"
                "Set one and ZBox will ask for it once on each new "
                "computer, then unlock normally there from then on. "
                "This computer will not ask again.\n\n"
                "Write it down and keep it somewhere safe. It is not "
                "stored anywhere and cannot be recovered: if you "
                "forget it, every account has to be signed in to its "
                "mail server again.")
            )
        intro = wx.StaticText(self, label=explanation)
        wrap_text(intro, 52)
        sizer.Add(intro, 0, wx.ALL, 10)

        self.current = None
        if master_is_set:
            self.current = _password_field(
                self, sizer, lang.control_label('mp_label_current', "C&urrent master password:"),
                lang.t('dialogs', 'mp_name_current', default="Current master password"),
            )

        self.new_password = _password_field(
            self, sizer, lang.control_label('mp_label_new', "&New master password:"), lang.t('dialogs', 'mp_name_new', default="New master password")
        )
        self.confirm = _password_field(
            self, sizer, lang.control_label('mp_label_confirm', "&Confirm new master password:"),
            lang.t('dialogs', 'mp_name_confirm', default="Confirm new master password"),
        )

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)
        (self.current or self.new_password).SetFocus()

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, event):
        new = self.new_password.GetValue()
        confirm = self.confirm.GetValue()

        if new != confirm:
            wx.MessageBox(
                lang.t(
                    "errors", "master_password_mismatch",
                    default="The two new master password boxes do not match.",
                ),
                lang.t("dialogs", "title_master_password", default="Master Password"),
                wx.OK | wx.ICON_ERROR, self,
            )
            self.confirm.SetValue("")
            self.new_password.SetFocus()
            return

        if not new and not self.master_is_set:
            wx.MessageBox(
                lang.t(
                    "errors", "master_password_required",
                    default="Enter a master password, or press Cancel.",
                ),
                lang.t("dialogs", "title_master_password", default="Master Password"),
                wx.OK | wx.ICON_ERROR, self,
            )
            self.new_password.SetFocus()
            return

        event.Skip()

    def get_current_password(self):
        return self.current.GetValue() if self.current else None

    def get_new_password(self):
        """The new master password, or "" when the user is asking for
        it to be removed."""
        return self.new_password.GetValue()


def ensure_master_password_set(parent, config_dir, note):
    """Makes sure a master password exists before ZBox stores an
    account secret.

    It is required rather than offered, because without one the
    store has no key that travels: the secrets are readable by
    this Windows account on this machine and nowhere else, so a
    folder moved to a new computer silently loses every account.
    Asking here, at the moments it matters (startup with accounts
    already set up, before the first account is added, and -- on a
    genuinely fresh install -- right after the EULA is accepted, see
    main_frame.ZBoxApp._show_eula_if_needed), is the only warning
    that arrives while it can still be acted on.

    parent may be None, for the moment this runs before any frame
    exists (the EULA-acceptance call), or a wx.Window otherwise --
    exactly like every other dialog constructor here.

    Extracted from ZBoxMainFrame._ensure_master_password_set (which
    is now a thin wrapper calling this with self as parent) so the
    EULA gate can run the exact same prompt, loop and consequence
    explanation without a second copy of this logic.

    Returns True when a master password is in force.
    """
    import dpapi_secret_store

    try:
        if dpapi_secret_store.has_master_password(config_dir):
            return True
    except Exception:
        logging.getLogger("zbox.main_frame").exception(
            "Could not read the secret store's state."
        )
        return True

    while True:
        dialog = MasterPasswordDialog(parent, False, required_note=note)
        try:
            result = dialog.ShowModal()
            new_password = dialog.get_new_password()
        finally:
            dialog.Destroy()

        if result == wx.ID_OK and new_password:
            try:
                dpapi_secret_store.set_master_password(
                    config_dir, new_password
                )
            except Exception as exc:
                logging.getLogger("zbox.main_frame").exception(
                    "Setting the master password failed."
                )
                wx.MessageBox(
                    lang.t(
                        "errors", "master_password_not_set",
                        default="The master password could not be set: %s" % exc,
                        error=exc,
                    ),
                    lang.t(
                        "dialogs", "title_master_password",
                        default="Master Password",
                    ),
                    wx.OK | wx.ICON_ERROR, parent,
                )
                return False
            wx.MessageBox(
                lang.t(
                    "dialogs", "master_password_set",
                    default="Master password set. Keep it somewhere safe: ZBox "
                    "asks for it once on each new computer, and it "
                    "cannot be recovered if it is lost.",
                ),
                lang.t("dialogs", "title_master_password", default="Master Password"),
                wx.OK | wx.ICON_INFORMATION, parent,
            )
            return True

        answer = wx.MessageBox(
            lang.t(
                "dialogs", "master_password_offer",
                default="Without a master password, the saved account "
                "passwords can only be read on this computer and this "
                "Windows account. Copy ZBox anywhere else and every "
                "account has to be signed in again.\n\n"
                "Set one now?",
            ),
            lang.t("dialogs", "title_master_password", default="Master Password"),
            wx.YES_NO | wx.ICON_WARNING, parent,
        )
        if answer != wx.YES:
            return False
