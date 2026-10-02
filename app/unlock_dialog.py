"""
The two dialogs that front ZBox's app lock, and the startup check
that shows the first of them.

StartupUnlockDialog is the lock screen. It appears on launch only
when "Lock ZBox when it is fully quit" is on in Settings, and it
accepts any one of the registered methods -- passkey, security key,
or master password. OR, not AND: whichever is nearest to hand is
enough, which is the whole point of registering more than one.

UnlockMethodsDialog is where they are added and removed, reached
from Settings > Privacy and Security.

Both are ordinary wx dialogs with a real label on every control. The
lock screen especially: it is the first thing a screen reader meets
on launch, before the main window exists, so nothing in it may be a
button whose name has to be guessed from context.
"""

import ctypes
import logging
import threading

import wx

import lang
from accessible import fit_dialog, note_text, wrap_text
import unlock_methods
import webauthn_unlock

_log = logging.getLogger("zbox.unlock")


def _com_initialize():
    """Best effort. The WebAuthn API brings up COM UI of its own, and
    the worker thread it now runs on has no apartment until one is
    asked for."""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception:  # noqa: BLE001
        pass


def _com_uninitialize():
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


class _WindowsPromptDialog(wx.Dialog):
    """Runs one blocking WebAuthn call without freezing ZBox.

    This exists because of a real failure, not as a precaution.
    WebAuthNAuthenticatorMakeCredential and its GetAssertion sibling
    do not return until the person has answered Windows' own prompt.
    Called straight from a button handler, that blocks the GUI
    thread: wx stops pumping messages, Windows declares the window
    not responding -- which a screen reader announces -- and a window
    Windows considers hung cannot be given the foreground, so its own
    security prompt opens behind ZBox instead of in front of it. Both
    symptoms, one cause.

    So the call goes to a worker thread and this small modal dialog
    runs an event loop in the meantime, keeping ZBox alive and
    responsive. Its window handle is what the prompt is parented to,
    so Windows has a live, foreground window to attach to.

    It has no Cancel and Escape does nothing, deliberately: the thing
    to cancel is the Windows prompt itself, which has its own cancel,
    and a second cancel here would only leave a thread still waiting
    on a prompt whose owner had gone.
    """

    def __init__(self, parent, title, message, work):
        super().__init__(parent, title=title, style=wx.CAPTION)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self._work = work
        self.result = None
        self.error = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(note_text(self, message), 0, wx.ALL, 15)
        fit_dialog(self, sizer)
        self.SetEscapeId(wx.ID_NONE)

        # Started on the next event-loop turn, which is inside
        # ShowModal. Starting it here instead would race: a fast
        # failure could finish and call EndModal before the modal
        # loop existed, and the dialog would then never close.
        wx.CallAfter(self._start)

    def _start(self):
        threading.Thread(
            target=self._run, args=(self.GetHandle(),),
            name="zbox-webauthn", daemon=True,
        ).start()

    def _run(self, hwnd):
        _com_initialize()
        try:
            self.result = self._work(hwnd)
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
        finally:
            _com_uninitialize()
            wx.CallAfter(self._finish)

    def _finish(self):
        if self.IsModal():
            self.EndModal(wx.ID_OK)


def run_windows_prompt(parent, title, message, work):
    """Calls work(hwnd) on a worker thread, waits for it without
    freezing, and returns its result or re-raises its exception on
    the GUI thread, exactly as a direct call would have."""
    dialog = _WindowsPromptDialog(parent, title, message, work)
    try:
        dialog.ShowModal()
        if dialog.error is not None:
            raise dialog.error
        return dialog.result
    finally:
        dialog.Destroy()


_PROMPT_TITLE = "Windows Security"
_PROMPT_MESSAGE = (
    "Windows is asking you to confirm. Answer its prompt with "
    "Windows Hello, your PIN, or your security key. This window "
    "closes by itself when Windows is finished."
)


def _prompt_title():
    return lang.t("dialogs", "unlock_prompt_title", default=_PROMPT_TITLE)


def _prompt_message():
    return lang.t("dialogs", "unlock_prompt_message", default=_PROMPT_MESSAGE)


def _method_line(method):
    kind = lang.t(
        "dialogs", "unlock_kind_%s" % method.get("kind"),
        default=unlock_methods.KIND_LABELS.get(
            method.get("kind"), method.get("kind") or "Unknown"
        ),
    )
    registered = method.get("registered") or "unknown date"
    line = "%s \u2014 %s, added %s" % (
        method.get("name") or "Unnamed", kind, registered,
    )
    if method.get("kind") != unlock_methods.KIND_PASSKEY:
        return line
    # Spelled out in the row itself rather than left to a colour or
    # an icon: whether a passkey belongs to this computer is the one
    # thing that decides if it can do anything, and a screen reader
    # reads the row.
    if unlock_methods.is_usable_here(method):
        return line + ", on this computer"
    return line + ", on %s \u2014 not usable here" % (
        method.get("machine_name") or "another computer"
    )


def _button_label(base, enabled):
    """A disabled button whose label still reads as an offer is a
    trap: a screen reader announces the name first and the disabled
    state second, if at all. Saying "unavailable" in the name itself
    means the reason is heard rather than inferred."""
    return base + "\u2026" if enabled else base + " (unavailable)"


def _master_password_set(config_dir):
    return unlock_methods.master_password_set(config_dir)


class StartupUnlockDialog(wx.Dialog):
    """The lock screen. Ends with wx.ID_OK only when one of the
    methods actually succeeded."""

    def __init__(self, parent, config_dir, master_set, methods):
        # No close box: the only two ways out are unlocking and the
        # Exit button, and a title-bar X that quietly did the same
        # thing as Exit would be a third, unlabelled one. Escape
        # still cancels, which exits, and that is standard enough to
        # be expected.
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_zbox_locked", default="ZBox is Locked"),
            style=wx.CAPTION | wx.SYSTEM_MENU,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.config_dir = config_dir
        # Usable, not merely registered. A passkey carried here from
        # another computer cannot answer, so offering a button for it
        # would be offering a button that always fails.
        self._has_passkey = bool(unlock_methods.usable_methods(
            config_dir, unlock_methods.KIND_PASSKEY
        ))
        self._has_security_key = bool(unlock_methods.usable_methods(
            config_dir, unlock_methods.KIND_SECURITY_KEY
        ))
        self._foreign_passkeys = unlock_methods.foreign_passkeys(config_dir)
        self._master_set = master_set

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(
            note_text(
                self,
                lang.t(
                    "dialogs", "unl_note_locked",
                    default="ZBox is locked. Use any one of the methods below to "
                            "unlock it. Cancelling closes ZBox without opening any "
                            "account.",
                ),
            ),
            0, wx.ALL, 10,
        )

        first_control = None

        if self._foreign_passkeys and not self._has_passkey:
            # The carried-folder case. Saying it here saves someone
            # hunting for a passkey prompt that is never coming, and
            # names the fix.
            sizer.Add(
                note_text(
                    self,
                    lang.t(
                        "dialogs", "unl_note_foreign_passkey",
                        default="The passkey saved with this folder belongs to "
                                "another computer, so it cannot be used here. "
                                "Unlock with your security key or master "
                                "password, then use Settings, Privacy and "
                                "Security, Unlock methods to set up a passkey on "
                                "this computer.",
                    ),
                ),
                0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10,
            )

        if self._has_passkey:
            self.passkey_button = wx.Button(
                self,
                label=lang.control_label(
                    "unl_passkey", "Unlock with &passkey (Windows Hello)",
                ),
            )
            self.passkey_button.Bind(wx.EVT_BUTTON, self._on_passkey)
            sizer.Add(self.passkey_button, 0, wx.EXPAND | wx.ALL, 10)
            first_control = self.passkey_button

        if self._has_security_key:
            self.security_key_button = wx.Button(
                self,
                label=lang.control_label(
                    "unl_security_key", "Unlock with &security key",
                ),
            )
            self.security_key_button.Bind(wx.EVT_BUTTON, self._on_security_key)
            sizer.Add(self.security_key_button, 0, wx.EXPAND | wx.ALL, 10)
            first_control = first_control or self.security_key_button

        self.password = None
        if master_set:
            if first_control is not None:
                sizer.Add(
                    wx.StaticLine(self), 0,
                    wx.EXPAND | wx.LEFT | wx.RIGHT, 10,
                )
            label = wx.StaticText(
                self,
                label=lang.control_label(
                    "unl_master_password_label", "&Master password:",
                ),
            )
            sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
            self.password = wx.TextCtrl(
                self, style=wx.TE_PASSWORD | wx.TE_PROCESS_ENTER
            )
            self.password.SetName(
                lang.t(
                    "dialogs", "unl_master_password_name",
                    default="Master password",
                )
            )
            self.password.Bind(wx.EVT_TEXT_ENTER, self._on_master_password)
            sizer.Add(
                self.password, 0,
                wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10,
            )
            unlock_button = wx.Button(
                self, label=lang.control_label("unl_unlock", "&Unlock")
            )
            unlock_button.Bind(wx.EVT_BUTTON, self._on_master_password)
            sizer.Add(
                unlock_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
            )
            # Focus goes here when there is a password to type:
            # typing starts immediately, and the buttons above are
            # one Shift+Tab away.
            first_control = self.password

        self.status_label = wx.StaticText(self, label="")
        wrap_text(self.status_label)
        sizer.Add(
            self.status_label, 0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10,
        )

        exit_button = wx.Button(
            self, wx.ID_CANCEL, label=lang.control_label("unl_exit", "E&xit ZBox")
        )
        sizer.Add(exit_button, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)
        if first_control is not None:
            first_control.SetFocus()

    # --- helpers ------------------------------------------------------

    def _set_status(self, text):
        self.status_label.SetLabel(text)
        wrap_text(self.status_label)
        self.Layout()

    def _run_method(self, kind, what):
        _log.info("Startup unlock attempt: %s", kind)
        try:
            run_windows_prompt(
                self, _prompt_title(), _prompt_message(),
                lambda hwnd: unlock_methods.verify(
                    hwnd, self.config_dir, kind
                ),
            )
        except webauthn_unlock.WebAuthnCancelled:
            # Dismissing the Windows prompt is a decision, not an
            # error: say so quietly and leave the dialog as it was.
            _log.info("Startup unlock cancelled: %s", kind)
            self._set_status(
                lang.t(
                    "dialogs",
                    "unl_status_cancelled_passkey"
                    if kind == unlock_methods.KIND_PASSKEY
                    else "unl_status_cancelled_key",
                    default="%s was cancelled. ZBox is still locked." % what,
                )
            )
            return
        except unlock_methods.NoUnlockMethods as exc:
            wx.MessageBox(
                str(exc),
                lang.t("dialogs", "title_unlock", default="Unlock"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        except (webauthn_unlock.WebAuthnFailed,
                webauthn_unlock.WebAuthnUnavailable) as exc:
            _log.warning("Startup unlock failed: %s: %s", kind, exc)
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_method_failed",
                    default="%s did not work.\n\n%s" % (what, exc),
                    what=what, error=exc,
                ),
                lang.t("dialogs", "title_unlock", default="Unlock"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        except Exception as exc:  # noqa: BLE001
            _log.exception("Startup unlock raised: %s", kind)
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_method_failed",
                    default="%s did not work.\n\n%s" % (what, exc),
                    what=what, error=exc,
                ),
                lang.t("dialogs", "title_unlock", default="Unlock"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        _log.info("Startup unlock succeeded: %s", kind)
        self.EndModal(wx.ID_OK)

    # --- handlers -----------------------------------------------------

    def _on_passkey(self, _event):
        self._run_method(unlock_methods.KIND_PASSKEY, lang.t('dialogs', 'unlock_the_passkey', default="The passkey"))

    def _on_security_key(self, _event):
        self._run_method(unlock_methods.KIND_SECURITY_KEY, lang.t('dialogs', 'unlock_the_security_key', default="The security key"))

    def _on_master_password(self, _event):
        import dpapi_secret_store

        entered = self.password.GetValue()
        if not entered:
            self._set_status(
                lang.t(
                    "dialogs", "unl_status_enter_password",
                    default="Enter the master password.",
                )
            )
            self.password.SetFocus()
            return
        try:
            correct = dpapi_secret_store.verify_master_password(
                self.config_dir, entered
            )
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_check_failed",
                    default="The master password could not be checked.\n\n%s" % exc,
                    error=exc,
                ),
                lang.t("dialogs", "title_unlock", default="Unlock"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return

        if not correct:
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_password_wrong",
                    default="That master password is not correct.",
                ),
                lang.t("dialogs", "title_unlock", default="Unlock"),
                wx.OK | wx.ICON_ERROR, self,
            )
            self.password.SetValue("")
            self.password.SetFocus()
            return
        self.EndModal(wx.ID_OK)


class UnlockMethodsDialog(wx.Dialog):
    """Settings > Privacy and Security > Unlock methods."""

    def __init__(self, parent, config_dir):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_unlock_methods", default="Unlock Methods",
            ),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.config_dir = config_dir

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(
            note_text(
                self,
                lang.t(
                    "dialogs", "unl_note_methods",
                    default="Any one of these unlocks ZBox when the app lock is "
                            "on, alongside the master password. A passkey uses "
                            "Windows Hello on this computer; a security key is a "
                            "FIDO2 key on USB or NFC and works on any computer "
                            "you plug it into.",
                ),
            ),
            0, wx.ALL, 10,
        )
        sizer.Add(
            note_text(
                self,
                lang.t(
                    "dialogs", "unl_note_not_encryption",
                    default="These unlock the app. They do not encrypt anything: "
                            "the master password is still the only thing that can "
                            "open the saved account passwords on a computer this "
                            "folder has not been used on before. It has to be set "
                            "first, and neither of these can be added until it "
                            "is.",
                ),
            ),
            0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10,
        )

        list_label = wx.StaticText(
            self,
            label=lang.control_label(
                "unl_methods_label", "&Registered methods:",
            ),
        )
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT, 10)

        self.method_list = wx.ListBox(self, choices=[], style=wx.LB_SINGLE)
        self.method_list.SetName(
            lang.t(
                "dialogs", "unl_methods_name",
                default="Registered unlock methods",
            )
        )
        self.method_list.Bind(wx.EVT_LISTBOX, self._on_selection_changed)
        sizer.Add(
            self.method_list, 1,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10,
        )

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.add_passkey_button = wx.Button(
            self, label=lang.control_label("unl_add_passkey", "Add &passkey\u2026")
        )
        self.add_passkey_button.Bind(wx.EVT_BUTTON, self._on_add_passkey)
        self.add_key_button = wx.Button(
            self, label=lang.control_label("unl_add_key", "Add security &key\u2026")
        )
        self.add_key_button.Bind(wx.EVT_BUTTON, self._on_add_security_key)
        self.reassign_button = wx.Button(
            self,
            label=lang.control_label(
                "unl_reassign", "Re-&assign passkey to this computer\u2026",
            ),
        )
        self.reassign_button.Bind(wx.EVT_BUTTON, self._on_reassign)
        self.test_button = wx.Button(
            self, label=lang.control_label("unl_test", "&Test\u2026")
        )
        self.test_button.Bind(wx.EVT_BUTTON, self._on_test)
        self.remove_button = wx.Button(
            self, label=lang.control_label("unl_remove", "&Remove")
        )
        self.remove_button.Bind(wx.EVT_BUTTON, self._on_remove)
        for button in (
            self.add_passkey_button, self.add_key_button,
            self.reassign_button, self.test_button, self.remove_button,
        ):
            buttons.Add(button, 0, wx.RIGHT, 8)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.status_label = wx.StaticText(self, label="")
        wrap_text(self.status_label)
        sizer.Add(
            self.status_label, 0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10,
        )

        sizer.Add(
            self.CreateButtonSizer(wx.CLOSE), 0, wx.ALIGN_RIGHT | wx.ALL, 10
        )
        close_button = self.FindWindow(wx.ID_CLOSE)
        if close_button:
            close_button.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_OK))
        self.SetEscapeId(wx.ID_CLOSE)

        self._refresh()
        fit_dialog(self, sizer)
        self.method_list.SetFocus()

    # --- helpers ------------------------------------------------------

    def _refresh(self):
        self.methods = unlock_methods.list_methods(self.config_dir)
        self._foreign = unlock_methods.foreign_passkeys(self.config_dir)
        selection = self.method_list.GetSelection()
        self.method_list.Set([_method_line(m) for m in self.methods])
        if self.methods:
            self.method_list.SetSelection(
                min(max(selection, 0), len(self.methods) - 1)
            )

        supported = unlock_methods.available()
        master_set = unlock_methods.master_password_set(self.config_dir)
        hello = supported and unlock_methods.passkeys_supported()
        can_add_passkey = master_set and hello
        can_add_key = master_set and supported
        can_reassign = can_add_passkey and bool(self._foreign)

        self.add_passkey_button.SetLabel(
            lang.control_label("unl_add_passkey", "Add &passkey\u2026")
            if can_add_passkey
            else lang.control_label(
                "unl_add_passkey_unavailable", "Add &passkey (unavailable)",
            )
        )
        self.add_passkey_button.Enable(can_add_passkey)
        self.add_key_button.SetLabel(
            lang.control_label("unl_add_key", "Add security &key\u2026")
            if can_add_key
            else lang.control_label(
                "unl_add_key_unavailable", "Add security &key (unavailable)",
            )
        )
        self.add_key_button.Enable(can_add_key)
        self.reassign_button.SetLabel(
            lang.control_label(
                "unl_reassign", "Re-&assign passkey to this computer\u2026",
            )
            if can_reassign
            else lang.control_label(
                "unl_reassign_unavailable",
                "Re-&assign passkey to this computer (unavailable)",
            )
        )
        self.reassign_button.Enable(can_reassign)
        self._on_selection_changed(None)

        if not master_set:
            self._set_status(
                lang.t(
                    "dialogs", "unl_status_no_master",
                    default="No master password is set, so a passkey or security "
                            "key cannot be added yet. Set one with the Master "
                            "password button in Settings, Privacy and Security, "
                            "then come back here. The master password is what "
                            "opens your saved account passwords on another "
                            "computer; a passkey only unlocks the app on this "
                            "one, so it cannot stand alone.",
                )
            )
        elif not supported:
            self._set_status(
                lang.t(
                    "dialogs", "unl_status_unsupported",
                    default="This computer's Windows does not support passkeys or "
                            "security keys, so only the master password can unlock "
                            "ZBox here.",
                )
            )
        elif self._foreign:
            count = len(self._foreign)
            if count == 1:
                foreign_key = (
                    "unl_status_foreign_one" if hello
                    else "unl_status_foreign_one_no_hello"
                )
            else:
                foreign_key = (
                    "unl_status_foreign_many" if hello
                    else "unl_status_foreign_many_no_hello"
                )
            self._set_status(
                lang.t(
                    "dialogs", foreign_key,
                    default=(
                        ("1 passkey belongs" if count == 1
                         else "%d passkeys belong" % count)
                        + " to another computer and cannot unlock ZBox here. "
                        + (
                            "Re-assign replaces them with one made on this "
                            "computer." if hello else
                            "Windows Hello is not set up here, so they cannot "
                            "be replaced on this computer; Remove forgets them."
                        )
                    ),
                    count=count,
                )
            )
        elif not hello:
            self._set_status(
                lang.t(
                    "dialogs", "unl_status_no_hello",
                    default="Windows Hello is not set up on this computer, so a "
                            "passkey cannot be added here. A security key still "
                            "can.",
                )
            )
        elif not self.methods:
            self._set_status(
                lang.t(
                    "dialogs", "unl_status_none_registered",
                    default="No unlock methods are registered yet.",
                )
            )
        else:
            self._set_status("")

    def _set_status(self, text):
        self.status_label.SetLabel(text)
        wrap_text(self.status_label)
        self.Layout()

    def _selected_method(self):
        index = self.method_list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self.methods):
            return None
        return self.methods[index]

    def _on_selection_changed(self, _event):
        method = self._selected_method()
        self.remove_button.Enable(method is not None)
        # Testing a passkey from another computer can only ever fail,
        # so the button says so by being unavailable rather than by
        # producing an error.
        self.test_button.Enable(
            method is not None and unlock_methods.is_usable_here(method)
        )

    def _add(self, kind, what):
        _log.info("Registering unlock method: %s", kind)
        try:
            method = run_windows_prompt(
                self, _prompt_title(), _prompt_message(),
                lambda hwnd: unlock_methods.register(
                    hwnd, self.config_dir, kind
                ),
            )
        except webauthn_unlock.WebAuthnCancelled:
            _log.info("Registration cancelled: %s", kind)
            self._set_status(
                lang.t(
                    "dialogs",
                    "unl_status_add_cancelled_passkey"
                    if kind == unlock_methods.KIND_PASSKEY
                    else "unl_status_add_cancelled_key",
                    default="Adding %s was cancelled." % what,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            _log.exception("Registration failed: %s", kind)
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_add_failed",
                    default="%s could not be added.\n\n%s" % (what.capitalize(), exc),
                    what=what.capitalize(), error=exc,
                ),
                lang.t("dialogs", "title_unlock_methods", default="Unlock Methods"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        _log.info("Registered unlock method: %s", kind)
        self._refresh()
        self._set_status(
            lang.t(
                "dialogs", "unl_status_added",
                default="Added: %s." % method["name"], name=method["name"],
            )
        )

    # --- handlers -----------------------------------------------------

    def _on_add_passkey(self, _event):
        self._add(unlock_methods.KIND_PASSKEY, "a passkey")

    def _on_add_security_key(self, _event):
        self._add(unlock_methods.KIND_SECURITY_KEY, "a security key")

    def _on_reassign(self, _event):
        """The carried-folder repair: make a passkey here, then forget
        the ones from other computers."""
        count = len(self._foreign)
        if not count:
            return
        if wx.MessageBox(
            lang.t(
                "dialogs", "unlock_replace_passkey_one",
                default="Set up a passkey on this computer and forget the one "
                "saved from another computer?\n\n"
                "Windows will ask you to confirm with Hello. The old "
                "passkey left on the other computer is untouched there and "
                "will keep working if you take this folder back to it, "
                "but it will have to be set up again.",
            ) if count == 1 else lang.t(
                "dialogs", "unlock_replace_passkey_many",
                default="Set up a passkey on this computer and forget the %d "
                "saved from other computers?\n\n"
                "Windows will ask you to confirm with Hello. The old "
                "passkeys left on the other computer are untouched there and "
                "will keep working if you take this folder back to it, "
                "but they will have to be set up again." % count,
                count=count,
            ),
            lang.t("dialogs", "title_unlock_methods", default="Unlock Methods"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return

        try:
            method, forgotten = run_windows_prompt(
                self, _prompt_title(), _prompt_message(),
                lambda hwnd: unlock_methods.reassign_passkey(
                    hwnd, self.config_dir
                ),
            )
        except webauthn_unlock.WebAuthnCancelled:
            self._set_status(
                lang.t(
                    "dialogs", "unl_status_reassign_cancelled",
                    default="Re-assigning was cancelled. Nothing was changed.",
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_passkey_setup_failed",
                    default="The passkey could not be set up on this computer, so "
                    "nothing was changed.\n\n%s" % exc,
                    error=exc,
                ),
                lang.t("dialogs", "title_unlock_methods", default="Unlock Methods"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return

        self._refresh()
        self._set_status(
            lang.t(
                "dialogs", "unl_status_reassigned",
                default="Added %s and forgot %d from other computers."
                        % (method["name"], forgotten),
                name=method["name"], count=forgotten,
            )
        )

    def _on_test(self, _event):
        """Proves a method works now, rather than at the next launch
        when getting it wrong means not getting in."""
        method = self._selected_method()
        if method is None:
            return
        try:
            run_windows_prompt(
                self, _prompt_title(), _prompt_message(),
                lambda hwnd: unlock_methods.verify(
                    hwnd, self.config_dir, method.get("kind")
                ),
            )
        except webauthn_unlock.WebAuthnCancelled:
            self._set_status(
                lang.t(
                    "dialogs", "unl_status_test_cancelled",
                    default="The test was cancelled.",
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning("Unlock method test failed: %s", exc)
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_method_test_failed",
                    default="That method did not work.\n\n%s" % exc, error=exc,
                ),
                lang.t("dialogs", "title_unlock_methods", default="Unlock Methods"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        wx.MessageBox(
            lang.t("dialogs", "unlock_method_works", default="That method works."),
            lang.t("dialogs", "title_unlock_methods", default="Unlock Methods"),
            wx.OK | wx.ICON_INFORMATION, self,
        )

    def _on_remove(self, _event):
        method = self._selected_method()
        if method is None:
            return
        last_one = (
            len(self.methods) == 1
            and not _master_password_set(self.config_dir)
        )
        warning = (
            lang.t(
                "dialogs", "unl_remove_warning_last",
                default="Remove %s?\n\nIt is the only unlock method registered and "
                        "no master password is set, so with the app lock on there "
                        "would be nothing left to unlock ZBox with. The lock is "
                        "skipped rather than locking you out, but ZBox would then "
                        "open unlocked."
                        % (method.get("name") or "this method"),
                name=method.get("name") or "this method",
            )
            if last_one else
            lang.t(
                "dialogs", "unl_remove_warning",
                default="Remove %s? ZBox will no longer accept it."
                        % (method.get("name") or "this method"),
                name=method.get("name") or "this method",
            )
        )
        if wx.MessageBox(
            warning,
            lang.t("dialogs", "title_unlock_methods", default="Unlock Methods"),
            wx.YES_NO | wx.ICON_WARNING, self
        ) != wx.YES:
            return
        unlock_methods.remove(self.config_dir, method.get("id"))
        self._refresh()
        self._set_status(
            lang.t("dialogs", "unl_status_removed", default="Removed.")
        )


def unlock_at_startup(paths):
    """The launch-time gate. True means carry on starting ZBox.

    Called before the main window exists, so the lock screen is the
    first and only window until it is answered. When nothing is
    registered -- no methods and no master password -- there is
    nothing to check against, and the lock is skipped rather than
    making the app unopenable.
    """
    config_dir = paths.config
    master_set = _master_password_set(config_dir)
    methods = unlock_methods.usable_methods(config_dir)
    if not master_set and not methods:
        # Usable, not registered: a folder carried to a new computer
        # with only the old computer's passkey in it has nothing that
        # can answer here, and a lock screen with no working way past
        # it is a lock-out, not a lock.
        return True

    dialog = StartupUnlockDialog(None, config_dir, master_set, methods)
    try:
        return dialog.ShowModal() == wx.ID_OK
    finally:
        dialog.Destroy()
