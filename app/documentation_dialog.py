"""
ZBox Documentation dialog.

Shows the full technical description of ZBox (what it is, the UI
layout, menus, functions, hotkeys and how it works) straight from
docs/zbox_documentation.txt, and provides a single Copy to Clipboard
button that puts the whole text on the clipboard for pasting
anywhere. The text file itself lives next to the app and can also be
opened or copied directly from the folder.
"""

import wx

import lang

from accessible import bind_close_accelerators, make_read_only_viewer


class DocumentationDialog(wx.Dialog):
    """Read-only viewer for docs/zbox_documentation.txt with a
    one-button copy of the full text to the clipboard."""

    def __init__(self, parent, doc_path):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_documentation", default="ZBox Documentation",
            ),
            size=(760, 640),
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.doc_path = doc_path

        try:
            with open(doc_path, "r", encoding="utf-8") as handle:
                self._text = handle.read()
        except OSError:
            self._text = (lang.t('dialogs', 'doc_unreadable', default='The documentation file could not be read:') + '\n') + doc_path

        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "title_documentation", default="ZBox Documentation",
            ),
        )
        heading.SetFont(heading.GetFont().Bold())
        sizer.Add(heading, 0, wx.ALL, 8)

        self.text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.text.SetName(
            lang.t(
                "dialogs", "doc_text_name",
                default="ZBox documentation text",
            )
        )
        make_read_only_viewer(self.text)
        self.text.SetValue(self._text)
        sizer.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy_button = wx.Button(
            self, label=lang.t("dialogs", "doc_copy", default="Copy to Clipboard")
        )
        copy_button.Bind(wx.EVT_BUTTON, self._on_copy)
        close_button = wx.Button(
            self, wx.ID_CLOSE,
            lang.t("dialogs", "btn_close_plain", default="Close"),
        )
        close_button.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        buttons.Add(copy_button, 0, wx.ALL, 8)
        buttons.Add(close_button, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)

        bind_close_accelerators(self, wx.ID_CLOSE)
        # Land on the documentation text so arrow keys read it
        # immediately (after the dialog is actually shown).
        wx.CallAfter(self.text.SetFocus)

    def _on_copy(self, event):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self._text))
            wx.TheClipboard.Close()
            wx.MessageBox(
                lang.t(
                    "dialogs", "documentation_copied",
                    default="The full ZBox documentation was copied to the clipboard.",
                ),
                lang.t("dialogs", "title_documentation", default="ZBox Documentation"),
                wx.OK | wx.ICON_INFORMATION,
            )
        else:
            wx.MessageBox(
                lang.t(
                    "errors", "clipboard_unavailable",
                    default="The clipboard could not be opened.",
                ),
                lang.t("dialogs", "title_documentation", default="ZBox Documentation"),
                wx.OK | wx.ICON_ERROR,
            )
