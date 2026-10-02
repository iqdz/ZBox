"""
The EULA / license gate shown once, on a genuinely fresh install,
before ZBox's main window is ever created -- and the same license
text reachable afterward, read-only, from Help > About ZBox.

Two modes of the one dialog, chosen by `interactive`:

gate mode (interactive=True): the license text, buttons that open
each external component's own license page in the user's own
browser, and three controls -- "I Agree" (a checkbox), "Next"
(disabled until Agree is checked), and "I Disagree" (always
available, and the only way out that does not require agreeing to
anything). Escape acts as Disagree, for the same reason
StartupUnlockDialog's own missing close box exists: no unlabelled
way out that skips the explicit choice.

viewer mode (interactive=False): the same text and the same
external-link buttons, with a plain Close button and no gating --
what Help > About ZBox's "View License..." button opens.

The license text is read straight from docs/LICENSE.txt (see
paths.license_file), the same "one tracked file, read at display
time" pattern documentation_dialog.py already uses for the manual --
so editing the license never means touching this dialog's code.

The external links open with wx.LaunchDefaultBrowser rather than an
in-app WebView, matching how ZBox already treats every other link a
message can contain (see main_frame.open_compose_from_mailto's own
reasoning for mailto: links) -- and, more importantly here, so a
screen reader is reading the user's own configured browser rather
than a WebView2 instance, which is exactly the render engine ZBox's
own HTML message view has to work around for accessibility reasons.
"""

import wx

import lang

from accessible import bind_close_accelerators, make_read_only_viewer, note_text


# (label, url) -- the third-party components named in the license
# text, in the same order they appear there.
EXTERNAL_LICENSE_LINKS = (
    ("Open Himalaya's license page in your browser",
     "https://github.com/pimalaya/himalaya"),
    ("Open StevenBlack hosts' license page in your browser",
     "https://github.com/StevenBlack/hosts"),
    ("Open EasyList/EasyPrivacy's license page in your browser",
     "https://easylist.to/pages/licence.html"),
    ("Open Phishing.Database's license page in your browser",
     "https://github.com/Phishing-Database/Phishing.Database/blob/master/LICENSE"),
)


def _open_link(url):
    if not wx.LaunchDefaultBrowser(url):
        wx.MessageBox(
            lang.t(
                "errors", "open_link_failed",
                default="Could not open your browser for:\n%s" % url, url=url,
            ),
            lang.t("dialogs", "title_open_link", default="Open Link"),
            wx.OK | wx.ICON_ERROR,
        )


class EulaDialog(wx.Dialog):
    """See module docstring for the two modes."""

    def __init__(self, parent, license_path, interactive):
        title = (
            lang.t(
                "dialogs", "title_license_agreement",
                default="ZBox License Agreement",
            )
            if interactive
            else lang.t("dialogs", "eula_heading", default="ZBox License")
        )
        style = wx.CAPTION | (0 if interactive else wx.SYSTEM_MENU)
        super().__init__(parent, title=title, size=(760, 640), style=style)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.interactive = interactive

        try:
            with open(license_path, "r", encoding="utf-8") as handle:
                self._text = handle.read()
        except OSError:
            self._text = (
                lang.t('dialogs', 'eula_unreadable', default="The license file could not be read:\n%s") % license_path
            )

        sizer = wx.BoxSizer(wx.VERTICAL)

        if interactive:
            sizer.Add(
                note_text(
                    self,
                    lang.t('dialogs', 'eula_read_note', default="Read the license below, including the third-party "
                    "sections. You must check I Agree before Next "
                    "becomes available. I Disagree closes ZBox without "
                    "starting it, and removes what this run has "
                    "created so far."),
                ),
                0, wx.ALL, 10,
            )

        heading = wx.StaticText(
            self, label=lang.t("dialogs", "eula_heading", default="ZBox License")
        )
        heading.SetFont(heading.GetFont().Bold())
        sizer.Add(heading, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.text.SetName(
            lang.t("dialogs", "eula_text_name", default="ZBox license text")
        )
        make_read_only_viewer(self.text)
        self.text.SetValue(self._text)
        sizer.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        links_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "eula_third_party",
                default="Third-party component license pages:",
            ),
        )
        sizer.Add(links_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        links_sizer = wx.BoxSizer(wx.VERTICAL)
        for index, (label, url) in enumerate(EXTERNAL_LICENSE_LINKS):
            button = wx.Button(self, label=lang.t("dialogs", "eula_link_%d" % index, default=label))
            button.Bind(wx.EVT_BUTTON, lambda evt, u=url: _open_link(u))
            links_sizer.Add(button, 0, wx.EXPAND | wx.BOTTOM, 4)
        sizer.Add(links_sizer, 0, wx.EXPAND | wx.ALL, 8)

        if interactive:
            self.agree_check = wx.CheckBox(
                self,
                label=lang.control_label(
                    "eula_agree", "&I Agree to the terms above",
                ),
            )
            self.agree_check.SetName(
                lang.t(
                    "dialogs", "eula_agree_name",
                    default="I have read and agree to the ZBox license terms",
                )
            )
            self.agree_check.Bind(wx.EVT_CHECKBOX, self._on_agree_toggled)
            sizer.Add(self.agree_check, 0, wx.ALL, 8)

            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.disagree_button = wx.Button(
                self, label=lang.control_label("eula_disagree", "I &Disagree")
            )
            self.disagree_button.Bind(wx.EVT_BUTTON, self._on_disagree)
            self.next_button = wx.Button(
                self, label=lang.control_label("eula_next", "&Next")
            )
            self.next_button.Enable(False)
            self.next_button.Bind(wx.EVT_BUTTON, self._on_next)
            buttons.Add(self.disagree_button, 0, wx.RIGHT, 8)
            buttons.Add(self.next_button, 0)
            sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

            self.SetEscapeId(wx.ID_NONE)
            self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
            # Ctrl+W and Ctrl+F4 mean the same thing Escape already
            # does on this dialog -- Disagree, not a no-op close --
            # so all three keys a reader might reach for to close a
            # "page" agree with each other here too. Deliberately not
            # accessible.bind_close_accelerators: that helper closes
            # with a plain result, which is the wrong action on a
            # gate where closing IS declining.
            disagree_w_id = wx.NewIdRef()
            disagree_f4_id = wx.NewIdRef()
            self.Bind(wx.EVT_MENU, self._on_disagree, id=disagree_w_id)
            self.Bind(wx.EVT_MENU, self._on_disagree, id=disagree_f4_id)
            self.SetAcceleratorTable(wx.AcceleratorTable([
                (wx.ACCEL_CTRL, ord("W"), disagree_w_id),
                (wx.ACCEL_CTRL, wx.WXK_F4, disagree_f4_id),
            ]))
        else:
            close_button = wx.Button(
                self, wx.ID_CLOSE,
                lang.t("dialogs", "btn_close_plain", default="Close"),
            )
            close_button.Bind(
                wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE)
            )
            sizer.Add(close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            bind_close_accelerators(self, wx.ID_CLOSE)

        self.SetSizer(sizer)
        wx.CallAfter(self.text.SetFocus)

    def _on_agree_toggled(self, event):
        self.next_button.Enable(self.agree_check.GetValue())

    def _on_next(self, event):
        self.EndModal(wx.ID_OK)

    def _on_disagree(self, event):
        self.EndModal(wx.ID_CANCEL)

    def _on_char_hook(self, event):
        # Escape acts as I Disagree -- deliberately, so there is no
        # unlabelled way out of this gate that skips the explicit
        # choice. See StartupUnlockDialog's own missing close box for
        # the same reasoning applied to a different first-run gate.
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._on_disagree(event)
            return
        event.Skip()
