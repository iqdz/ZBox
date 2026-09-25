"""
Help > About ZBox.

Replaces the old plain wx.MessageBox with an accessible dialog
matching documentation_dialog.py's own shape: a short read-only
description, then buttons for everything else an About page
traditionally carries -- the license, help getting a Gmail account
working without OAuth2 yet, where to support development, where to
find Harith's other apps, and a way to contact him.

Sized with accessible.fit_dialog rather than a fixed pixel size, like
every other dialog in ZBox, now that there are enough buttons for a
hardcoded size to clip under Windows text scaling.
"""

import os

import wx

import lang
from accessible import bind_close_accelerators, fit_dialog, make_read_only_viewer
from eula_dialog import EulaDialog, _open_link

# Paragraphs, not hard-wrapped lines. The viewer wraps them itself,
# and a screen reader reads a whole paragraph as one run instead of
# stopping at the end of every short line.
ABOUT_TEXT = (
    "ZBox\n"
    "By Harith Alhamdani\n\n"
    "ZBox is a portable, snappy, secure, and fully accessible email "
    "client. Free for personal use and light on system resources. It "
    "was created to give blind and low-vision users a real, "
    "feature-packed alternative to the bloated and sluggish options "
    "usually available. Under the hood, ZBox is powered by the robust "
    "Himalaya CLI email client.\n\n"
    "Privacy and Security\n"
    "ZBox comes bundled with a tracker and ad blocker (enabled by "
    "default), alongside optional junk mail rules and a phishing link "
    "checker. For secure portability, it uses Master Password "
    "protection: if the app folder is copied to another computer "
    "without this password, access to your linked accounts is "
    "blocked. If you plan to move ZBox to a new machine, be sure to "
    "set a master password here first. It also supports Windows Hello "
    "passkeys (fingerprint, PIN, or face recognition) and modern "
    "hardware security keys.\n\n"
    "Familiar Usability\n"
    "To make the transition from other email clients seamless, ZBox "
    "features friendly, flexible hotkeys. Multiple shortcuts often "
    "perform the same action; for example, both Alt+S and Ctrl+Enter "
    "will send a message. You can view the complete list under Help, "
    "Keyboard Shortcuts.\n\n"
    "Account Setup\n"
    "Native Google and Microsoft OAuth2 sign-in will be added in "
    "future updates. For now, you can link your Gmail account by "
    "generating an App Password instead of using your standard "
    "password (see the button below for instructions).\n\n"
    "Licenses and Credits\n"
    "Press View License below to read ZBox's license and view the "
    "third-party components that make it possible. These include "
    "Himalaya, the StevenBlack hosts and EasyPrivacy blocklists, the "
    "Phishing.Database list, and (in a built copy) Python, wxPython, "
    "imapclient, pywin32, keyring, and accessible_output2 with its "
    "screen reader client libraries."
)

GOOGLE_APP_PASSWORDS_URL = "https://myaccount.google.com/apppasswords"
SUPPORT_ZBOX_URL = "https://ko-fi.com/happs#"
HARITH_GITHUB_URL = "https://github.com/iqdz?tab=repositories"
CONTACT_HARITH_ADDRESS = "harith@gvoice.org"


def read_version(license_path):
    """The first line of version.txt, or "" when there is none.

    version.txt sits in the folder above docs: the project root when
    running from source, apps_files in a build (zbox.spec carries it
    there beside docs). It is local only and never committed, so a
    copy without it simply shows no version."""
    if not license_path:
        return ""
    folder = os.path.dirname(os.path.dirname(os.path.abspath(license_path)))
    try:
        with open(os.path.join(folder, "version.txt"), "r", encoding="utf-8-sig") as handle:
            first = handle.readline()
    except OSError:
        return ""
    return first.strip()


def with_version(text, version):
    """Puts the version after ZBox on the About text's first line."""
    if not version:
        return text
    first, sep, rest = text.partition("\n")
    if first.strip() == "ZBox":
        return "ZBox " + version + sep + rest
    return "ZBox " + version + "\n" + text


class AboutZBoxDialog(wx.Dialog):
    """Read-only About text plus every other button an About page
    carries. main_frame is the ZBoxMainFrame this was opened from
    (also this dialog's wx parent) -- kept as a separate reference so
    Contact Harith can open a real compose tab there rather than only
    ever falling back to the OS mail handler."""

    def __init__(self, parent, main_frame, license_path):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_about", default="About ZBox"),
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.main_frame = main_frame
        self.license_path = license_path

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.text.SetName(
            lang.t("dialogs", "about_text_name", default="About ZBox text")
        )
        make_read_only_viewer(self.text)
        self.text.SetValue(
            with_version(lang.body("about", ABOUT_TEXT), read_version(license_path))
        )
        sizer.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        buttons = wx.BoxSizer(wx.VERTICAL)

        license_button = wx.Button(
            self,
            label=lang.control_label("about_view_license", "View &License..."),
        )
        license_button.Bind(wx.EVT_BUTTON, self._on_view_license)
        buttons.Add(license_button, 0, wx.EXPAND | wx.BOTTOM, 4)

        google_button = wx.Button(
            self,
            label=lang.control_label(
                "about_google", "&Google App Passwords...",
            ),
        )
        google_button.Bind(
            wx.EVT_BUTTON, lambda evt: _open_link(GOOGLE_APP_PASSWORDS_URL)
        )
        buttons.Add(google_button, 0, wx.EXPAND | wx.BOTTOM, 4)

        support_button = wx.Button(
            self,
            label=lang.control_label(
                "about_support",
                "&Support ZBox -- One-Person Development and Maintenance",
            ),
        )
        support_button.Bind(
            wx.EVT_BUTTON, lambda evt: _open_link(SUPPORT_ZBOX_URL)
        )
        buttons.Add(support_button, 0, wx.EXPAND | wx.BOTTOM, 4)

        github_button = wx.Button(
            self,
            label=lang.control_label(
                "about_repos", "Harith's &Apps Repositories on GitHub",
            ),
        )
        github_button.Bind(wx.EVT_BUTTON, lambda evt: _open_link(HARITH_GITHUB_URL))
        buttons.Add(github_button, 0, wx.EXPAND | wx.BOTTOM, 4)

        contact_button = wx.Button(
            self, label=lang.control_label("about_contact", "&Contact Harith")
        )
        contact_button.Bind(wx.EVT_BUTTON, self._on_contact)
        buttons.Add(contact_button, 0, wx.EXPAND | wx.BOTTOM, 4)

        close_button = wx.Button(
            self, wx.ID_CLOSE,
            lang.t("dialogs", "btn_close_plain", default="Close"),
        )
        close_button.Bind(
            wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE)
        )
        buttons.Add(close_button, 0, wx.ALIGN_RIGHT | wx.TOP, 4)

        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)

        fit_dialog(self, sizer, min_width_chars=60)
        bind_close_accelerators(self, wx.ID_CLOSE)
        wx.CallAfter(self.text.SetFocus)

    def _on_view_license(self, event):
        dialog = EulaDialog(self, self.license_path, interactive=False)
        dialog.ShowModal()
        dialog.Destroy()

    def _on_contact(self, event):
        """Opens a ZBox compose tab addressed to Harith, the same
        path every mailto: link inside a rendered message already
        goes through (main_frame.open_compose_from_mailto) -- so
        contacting him stays inside ZBox and uses whichever account
        is already set up, rather than handing off to the OS mail
        handler. Deferred via CallAfter and this dialog closed first:
        it's modal, and would otherwise block reaching the new tab it
        just opened. Falls back to the OS mail handler only if there
        is genuinely no main_frame to open a compose tab on.
        """
        url = "mailto:%s" % CONTACT_HARITH_ADDRESS
        if self.main_frame is not None:
            wx.CallAfter(self.main_frame.open_compose_from_mailto, url)
            self.EndModal(wx.ID_CLOSE)
            return
        if not wx.LaunchDefaultBrowser(url):
            wx.MessageBox(
                lang.t(
                    "errors", "contact_no_mail_client",
                    default="Could not open a mail client for %s."
                    % CONTACT_HARITH_ADDRESS,
                    address=CONTACT_HARITH_ADDRESS,
                ),
                lang.t("dialogs", "title_contact_harith", default="Contact Harith"),
                wx.OK | wx.ICON_ERROR,
            )
