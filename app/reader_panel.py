"""
The inline reading pane, the bottom pane of the Mail tab.

Split out of main_frame.py (audit item 11, stage 2). This shows a
preview of the selected message; opening a message in its own tab is
message_view_panel's job, and that is where the HTML view and its
accessibility handling live.
"""

import wx

import lang
from accessible import apply_content_background, make_read_only_viewer
from message_body import extract_message_body


class ReaderPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        apply_content_background(self)

        label = wx.StaticText(
            self, label=lang.t("dialogs", "rdr_heading", default="Message")
        )
        label.SetFont(label.GetFont().Bold())

        self.reader = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.reader.SetName(
            lang.t("dialogs", "rdr_name", default="Message reader")
        )
        make_read_only_viewer(self.reader)
        self.reader.SetValue(lang.t('dialogs', 'rdr_select_message', default="Select a message to read it here."))

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(label, 0, wx.ALL, 6)
        sizer.Add(self.reader, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(sizer)

    def show_loading(self):
        self.reader.SetValue(lang.t('dialogs', 'rdr_loading', default="Loading message..."))

    def show_error(self, message):
        self.reader.SetValue(lang.t('dialogs', 'rdr_load_failed', default='Could not load message.\n\n{message}', message=message))

    def set_message(self, message):
        self.show_body_text(extract_message_body(message))

    def show_body_text(self, body):
        """Text already extracted off the main thread (see
        main_frame._on_envelope_selected), so an arrow press never
        waits on a large message being converted."""
        self.reader.SetValue(body if body else "(no message body)")
