"""
The attachment dialogs and the Full Headers dialog.

AttachmentDialog: one attachment. Its title is the attachment's name
and format, a line below gives name, format and size, and the buttons
are Open, Save and Close. It only answers which one was pressed; the
message tab does the work.

AttachmentsDialog: a message with more than one attachment. One
button per attachment, labelled the same way the single-attachment
button is in the message tab, each opening an AttachmentDialog, plus
Save All Attachments and Close.

FullHeadersDialog: View > Full Headers. A list with one row per
header, "Name: value", in the order received, so a screen reader
reads each header as one line instead of a raw block. Enter or
Ctrl+C copies the selected header, Copy All copies them all, Escape
closes.
"""

import wx

import lang
from attachment_extract import header_line

ID_ATTACHMENT_OPEN = wx.NewIdRef()
ID_ATTACHMENT_SAVE = wx.NewIdRef()


def _copy_to_clipboard(text):
    if not wx.TheClipboard.Open():
        return False
    try:
        wx.TheClipboard.SetData(wx.TextDataObject(text))
        return True
    finally:
        wx.TheClipboard.Close()


class AttachmentDialog(wx.Dialog):
    """ShowModal returns ID_ATTACHMENT_OPEN, ID_ATTACHMENT_SAVE or
    wx.ID_CLOSE."""

    def __init__(self, parent, label, info):
        super().__init__(parent, title=label)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        sizer = wx.BoxSizer(wx.VERTICAL)
        text = wx.StaticText(self, label=info)
        sizer.Add(text, 0, wx.ALL, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        open_button = wx.Button(
            self, ID_ATTACHMENT_OPEN, lang.t("dialogs", "att_open", default="Open"))
        save_button = wx.Button(
            self, ID_ATTACHMENT_SAVE, lang.t("dialogs", "att_save", default="Save..."))
        close_button = wx.Button(self, wx.ID_CLOSE)
        for button in (open_button, save_button, close_button):
            buttons.Add(button, 0, wx.ALL, 5)
            button.Bind(wx.EVT_BUTTON, self._on_button)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.ALL, 5)
        self.SetSizerAndFit(sizer)
        self.SetEscapeId(wx.ID_CLOSE)
        self.SetAffirmativeId(ID_ATTACHMENT_OPEN)
        open_button.SetDefault()
        open_button.SetFocus()
        self.CentreOnParent()

    def _on_button(self, event):
        self.EndModal(event.GetId())


class AttachmentsDialog(wx.Dialog):
    """on_pick(position) is called with the dialog still open, so the
    single-attachment dialog opens on top of it and returns here.
    on_save_all() closes this dialog and starts Save All."""

    def __init__(self, parent, labels, on_pick, on_save_all):
        import wx.lib.scrolledpanel as scrolled

        super().__init__(
            parent,
            title=lang.t("dialogs", "mv_attachments", default="Attachments"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self._on_pick = on_pick
        self._on_save_all = on_save_all
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel = scrolled.ScrolledPanel(self)
        panel_sizer = wx.BoxSizer(wx.VERTICAL)
        self._first = None
        for position, label in enumerate(labels):
            button = wx.Button(panel, label=label.replace("&", "&&"))
            button.Bind(wx.EVT_BUTTON, lambda _event, p=position: self._on_pick(p))
            panel_sizer.Add(button, 0, wx.EXPAND | wx.ALL, 3)
            if self._first is None:
                self._first = button
        panel.SetSizer(panel_sizer)
        panel.SetupScrolling(scroll_x=False)
        rows = min(len(labels), 12)
        height = (self._first.GetBestSize().height + 6) * max(rows, 1) + 10 if self._first else 60
        panel.SetMinSize((420, height))
        sizer.Add(panel, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        save_all = wx.Button(
            self, label=lang.t("dialogs", "att_save_all", default="Save All Attachments"))
        save_all.Bind(wx.EVT_BUTTON, self._on_save_all_button)
        close_button = wx.Button(self, wx.ID_CLOSE)
        close_button.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CLOSE))
        buttons.Add(save_all, 0, wx.ALL, 5)
        buttons.Add(close_button, 0, wx.ALL, 5)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.ALL, 5)
        self.SetSizerAndFit(sizer)
        self.SetEscapeId(wx.ID_CLOSE)
        if self._first is not None:
            self._first.SetFocus()
        self.CentreOnParent()

    def _on_save_all_button(self, _event):
        self.EndModal(wx.ID_CLOSE)
        self._on_save_all()


class FullHeadersDialog(wx.Dialog):
    def __init__(self, parent, rows, announce=None):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_full_headers", default="Full Headers"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=(760, 520),
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self._lines = [header_line(name, value) for name, value in rows]
        self._announce = announce or (lambda text: None)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER)
        self.list.SetName(lang.t("dialogs", "mv_message_headers", default="Message headers"))
        self.list.InsertColumn(0, "", width=720)
        for row, line in enumerate(self._lines):
            self.list.InsertItem(row, line)
        if self._lines:
            self.list.Select(0)
            self.list.Focus(0)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _event: self._copy_selected())
        self.list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy_all = wx.Button(self, label=lang.t("dialogs", "hdr_copy_all", default="Copy All"))
        copy_all.Bind(wx.EVT_BUTTON, lambda _event: self._copy_all())
        close_button = wx.Button(self, wx.ID_CLOSE)
        close_button.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CLOSE))
        buttons.Add(copy_all, 0, wx.ALL, 5)
        buttons.Add(close_button, 0, wx.ALL, 5)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.ALL, 5)
        self.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)
        self.list.SetFocus()
        self.CentreOnParent()

    def _on_list_key(self, event):
        if event.ControlDown() and event.GetKeyCode() in (ord("C"), ord("c")):
            self._copy_selected()
            return
        event.Skip()

    def _copy_selected(self):
        index = self.list.GetFirstSelected()
        if index < 0 or index >= len(self._lines):
            return
        if _copy_to_clipboard(self._lines[index]):
            self._announce(lang.t("actions_announcements", "header_copied", default="Copied."))

    def _copy_all(self):
        if _copy_to_clipboard("\n".join(self._lines)):
            self._announce(lang.t(
                "actions_announcements", "headers_copied", default="All headers copied."))
