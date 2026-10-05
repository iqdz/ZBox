"""
The Microsoft sign-in controls, shared by the account wizard and
Account and Identities Settings.

A read-only status field says where things stand, and the buttons
start a browser sign-in or a code sign-in. The network work runs on a
background thread (app/ms_oauth.py); results come back through
wx.CallAfter and are dropped if the panel has moved on or is gone.

Controls that do not apply are hidden rather than disabled, so the
tab order never has to step over them: Copy code and Open sign-in
page appear only while a code is waiting, Stop waiting only while a
sign-in is running. Every user-started step is spoken; when focus was
inside the panel it moves to the status field instead, so the screen
reader reads it once rather than twice.
"""

import logging
import threading

import wx

import lang
import ms_oauth
from accessible import make_read_only_viewer
from spoken_feedback import speak

logger = logging.getLogger("zbox.ms_signin")


class MicrosoftSignInPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._save_record = None
        self._login_hint = None
        self._on_signed_in = None
        self._cancel = None
        self._seq = 0
        self._signed_in = False
        self._username = ""
        self._user_code = ""
        self._verification_uri = ""

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 72),
        )
        self.status.SetName(lang.t("dialogs", "ms_status_name", "Microsoft sign-in status"))
        make_read_only_viewer(self.status)
        sizer.Add(self.status, 0, wx.EXPAND | wx.BOTTOM, 6)

        self.browser_button = wx.Button(
            self, label=lang.control_label("ms_browser_button", "Sign &in with browser"),
        )
        self.code_button = wx.Button(
            self, label=lang.control_label("ms_code_button", "Sign in with a co&de"),
        )
        self.copy_button = wx.Button(
            self, label=lang.control_label("ms_copy_code", "Co&py code"),
        )
        self.open_button = wx.Button(
            self, label=lang.control_label("ms_open_page", "&Open sign-in page"),
        )
        self.stop_button = wx.Button(
            self, label=lang.control_label("ms_stop", "Stop &waiting"),
        )
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for button in (self.browser_button, self.code_button, self.copy_button,
                       self.open_button, self.stop_button):
            buttons.Add(button, 0, wx.RIGHT | wx.BOTTOM, 6)
        sizer.Add(buttons, 0, wx.EXPAND)
        self.SetSizer(sizer)

        self.browser_button.Bind(wx.EVT_BUTTON, self._on_browser)
        self.code_button.Bind(wx.EVT_BUTTON, self._on_code)
        self.copy_button.Bind(wx.EVT_BUTTON, self._on_copy)
        self.open_button.Bind(wx.EVT_BUTTON, self._on_open)
        self.stop_button.Bind(wx.EVT_BUTTON, self._on_stop)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        self._show_controls(waiting=False, code=False)
        self.status.SetValue(self._idle_text())

    # --- For the owner ------------------------------------------------

    def set_handlers(self, save_record, login_hint=None, on_signed_in=None):
        """save_record(record) stores the tokens, login_hint() gives
        the address to suggest, on_signed_in(record) follows success."""
        self._save_record = save_record
        self._login_hint = login_hint
        self._on_signed_in = on_signed_in

    def is_signed_in(self):
        return self._signed_in

    def signed_in_as(self):
        return self._username

    def show_existing(self, username):
        """For an account that is already signed in."""
        self._signed_in = True
        self._username = username or ""
        self.status.SetValue(self._signed_in_text())

    def show_needs_sign_in(self):
        """For an account whose stored sign-in can no longer be used."""
        self._signed_in = False
        self._username = ""
        if ms_oauth.is_configured():
            self.status.SetValue(lang.t("dialogs", 
                "ms_needs_sign_in",
                "Microsoft sign-in for this account is needed again. Sign in with "
                "your browser, or with a code on any device.",
            ))

    def cancel(self):
        """Stops any sign-in in progress and drops its result."""
        self._seq += 1
        if self._cancel is not None:
            self._cancel.set()
            self._cancel = None

    # --- Texts --------------------------------------------------------

    def _idle_text(self):
        if not ms_oauth.is_configured():
            return lang.t("dialogs", 
                "ms_not_configured",
                "Microsoft sign-in is not set up in this copy of ZBox.",
            )
        if self._signed_in:
            return self._signed_in_text()
        return lang.t("dialogs", 
            "ms_not_signed_in",
            "Not signed in yet. Sign in with your browser, or with a code on any device.",
        )

    def _signed_in_text(self):
        if self._username:
            return lang.t("dialogs", "ms_signed_in", "Signed in as {address}.", address=self._username)
        return lang.t("dialogs", "ms_signed_in_plain", "Signed in.")

    def _reason(self, exc):
        kind = getattr(exc, "kind", "")
        if kind == "cancelled":
            return lang.t("dialogs", "ms_reason_cancelled", "Signing in was stopped.")
        if kind == "timeout":
            return lang.t("dialogs", "ms_reason_timeout", "No answer arrived in time.")
        if kind == "denied":
            return lang.t("dialogs", "ms_reason_denied", "Access was not allowed.")
        if kind == "expired":
            return lang.t("dialogs", "ms_reason_expired", "The code expired. Ask for a new one.")
        if kind == "network":
            return lang.t("dialogs", 
                "ms_reason_network",
                "Microsoft could not be reached. Check the connection and try again.",
            )
        if kind == "not_configured":
            return self._idle_text()
        detail = getattr(exc, "detail", "") or str(exc)
        return lang.t("dialogs", "ms_reason_server", "Microsoft said: {detail}", detail=detail)

    # --- Showing ------------------------------------------------------

    def _show_controls(self, waiting, code):
        configured = ms_oauth.is_configured()
        self.browser_button.Enable(configured and not waiting)
        self.code_button.Enable(configured and not waiting)
        self.copy_button.Show(code)
        self.open_button.Show(code)
        self.stop_button.Show(waiting)
        self.Layout()
        parent = self.GetParent()
        if parent is not None:
            parent.Layout()

    def _say(self, text, focus=None):
        """Shows text in the status field, and gets it read once: by
        moving focus there when focus is in this panel (or nowhere),
        by speaking it otherwise."""
        self.status.SetValue(text)
        if focus is None:
            current = wx.Window.FindFocus()
            focus = current is None or current is self or self.IsDescendant(current)
            if current is None:
                speak(self, text)
        if focus:
            self.status.SetFocus()
        else:
            speak(self, text)

    # --- Running a sign-in --------------------------------------------

    def _begin(self):
        self.cancel()
        self._seq += 1
        cancel = threading.Event()
        self._cancel = cancel
        self._user_code = ""
        self._verification_uri = ""
        return self._seq, cancel

    def _run(self, seq, work):
        def body():
            try:
                record = work()
            except ms_oauth.OAuthError as exc:
                wx.CallAfter(self._failed, seq, exc)
                return
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                logger.warning("Microsoft sign-in failed unexpectedly.", exc_info=True)
                wx.CallAfter(self._failed, seq, ms_oauth.OAuthError("server", str(exc)))
                return
            wx.CallAfter(self._succeeded, seq, record)

        threading.Thread(target=body, daemon=True).start()

    def _on_browser(self, _event):
        seq, cancel = self._begin()
        hint = ""
        if self._login_hint is not None:
            try:
                hint = self._login_hint() or ""
            except Exception:  # noqa: BLE001
                hint = ""
        page_done = lang.t("dialogs", 
            "ms_page_done", "Signed in. You can close this page and return to ZBox.",
        )
        page_failed = lang.t("dialogs", 
            "ms_page_failed", "Sign-in did not finish. Return to ZBox for details.",
        )
        self._show_controls(waiting=True, code=False)
        self._say(lang.t("dialogs", 
            "ms_browser_waiting",
            "Your browser shows Microsoft's sign-in page. Finish signing in there, "
            "then come back to ZBox.",
        ), focus=True)

        def open_url(url):
            wx.CallAfter(wx.LaunchDefaultBrowser, url)

        self._run(seq, lambda: ms_oauth.sign_in_with_browser(
            open_url, cancel, page_done, page_failed, hint,
        ))

    def _on_code(self, _event):
        seq, cancel = self._begin()
        # The address chooses the scopes, as for the browser sign-in.
        hint = ""
        if self._login_hint is not None:
            try:
                hint = self._login_hint() or ""
            except Exception:  # noqa: BLE001
                hint = ""
        self._show_controls(waiting=True, code=False)
        self._say(lang.t("dialogs", "ms_code_asking", "Asking Microsoft for a sign-in code..."), focus=True)

        def work():
            info = ms_oauth.start_device_code(hint)
            wx.CallAfter(self._show_code, seq, info)
            return ms_oauth.complete_device_code(info, cancel)

        self._run(seq, work)

    def _show_code(self, seq, info):
        if not self or seq != self._seq:
            return
        self._user_code = info.get("user_code", "")
        self._verification_uri = info.get("verification_uri", "")
        self._show_controls(waiting=True, code=True)
        self._say(lang.t("dialogs", 
            "ms_code_waiting",
            "On any device, open {address} and enter the code {code}. "
            "ZBox waits here until you finish signing in.",
            address=self._verification_uri, code=self._user_code,
        ), focus=True)

    def _succeeded(self, seq, record):
        if not self or seq != self._seq:
            return
        self._cancel = None
        try:
            if self._save_record is not None:
                self._save_record(record)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.warning("Could not save the Microsoft sign-in.", exc_info=True)
            self._failed(seq, ms_oauth.OAuthError("server", str(exc)))
            return
        self._signed_in = True
        self._username = str(record.get("username") or "")
        self._show_controls(waiting=False, code=False)
        self._say(self._signed_in_text())
        if self._on_signed_in is not None:
            self._on_signed_in(record)

    def _failed(self, seq, exc):
        if not self or seq != self._seq:
            return
        self._cancel = None
        self._show_controls(waiting=False, code=False)
        text = lang.t("dialogs", "ms_failed", "Sign-in did not finish. {reason}", reason=self._reason(exc))
        if self._signed_in:
            text += " " + self._signed_in_text()
        self._say(text)

    # --- Buttons ------------------------------------------------------

    def _on_copy(self, _event):
        if not self._user_code:
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(self._user_code))
            finally:
                wx.TheClipboard.Close()
            speak(self, lang.t("dialogs", "ms_code_copied", "Code copied."))

    def _on_open(self, _event):
        if self._verification_uri:
            wx.LaunchDefaultBrowser(self._verification_uri)

    def _on_stop(self, _event):
        if self._cancel is not None:
            self._cancel.set()
        self.status.SetFocus()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self.cancel()
        event.Skip()
