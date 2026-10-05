"""
Shift+M in the message list: move the selected message(s) by typing part
of a folder name.

A text box and, under it, the folders that match what is typed so far,
narrowing with every key. The folders, their wording and the move itself
are exactly those of the list's Move To menu (main_frame's
_move_copy_targets); this dialog only picks one of them.

Keys: typing filters; Down arrow goes from the text box into the list;
Enter picks the selected folder, or the first match when none is
selected; Escape cancels. With no match, OK says so and focus stays in
the text box.
"""

import wx

import lang
import notice_toast
from accessible import fit_dialog


class FolderSearchDialog(wx.Dialog):
    def __init__(self, parent, targets):
        """targets: [(real_folder, shown), ...] from _move_copy_targets."""
        super().__init__(
            parent,
            title=lang.t("dialogs", "move_title_move_to", default="Move To"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self._targets = list(targets)
        self._matches = list(self._targets)
        self._chosen = None

        # Each label is created right before its own control, because
        # JAWS names controls by creation order.
        filter_text = lang.t("dialogs", "folder_search_filter", default="Folder name contains")
        filter_label = wx.StaticText(self, label=filter_text)
        self.filter_field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.filter_field.SetName(filter_text)
        list_text = lang.t("dialogs", "folder_search_list", default="Matching folders")
        list_label = wx.StaticText(self, label=list_text)
        self.folder_list = wx.ListBox(
            self, choices=[shown for _real, shown in self._matches], style=wx.LB_SINGLE,
        )
        self.folder_list.SetName(list_text)
        self.folder_list.SetMinSize((320, self.folder_list.GetCharHeight() * 12))
        if self._matches:
            self.folder_list.SetSelection(0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(filter_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(self.filter_field, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(list_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.folder_list, 1, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.filter_field.Bind(wx.EVT_TEXT, self._on_filter)
        self.filter_field.Bind(wx.EVT_TEXT_ENTER, self._on_pick)
        self.filter_field.Bind(wx.EVT_KEY_DOWN, self._on_filter_key)
        self.folder_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_pick)
        self.Bind(wx.EVT_BUTTON, self._on_pick, id=wx.ID_OK)

        fit_dialog(self, sizer)
        wx.CallAfter(self.filter_field.SetFocus)

    def _on_filter(self, event):
        typed = self.filter_field.GetValue().strip().lower()
        self._matches = [
            (real, shown) for real, shown in self._targets
            if not typed or typed in shown.lower() or typed in str(real).lower()
        ]
        self.folder_list.Set([shown for _real, shown in self._matches])
        if self._matches:
            self.folder_list.SetSelection(0)

    def _on_filter_key(self, event):
        if event.GetKeyCode() == wx.WXK_DOWN and not event.HasAnyModifiers():
            if self._matches:
                if self.folder_list.GetSelection() == wx.NOT_FOUND:
                    self.folder_list.SetSelection(0)
                self.folder_list.SetFocus()
            return
        event.Skip()

    def _on_pick(self, event):
        if not self._matches:
            notice_toast.notify(
                self,
                lang.t(
                    "dialogs", "folder_search_no_match",
                    default="No folder matches that name.",
                ),
                lang.t("dialogs", "move_title_move_to", default="Move To"),
            )
            self.filter_field.SetFocus()
            return
        index = self.folder_list.GetSelection()
        if index == wx.NOT_FOUND:
            index = 0
        self._chosen = self._matches[index]
        self.EndModal(wx.ID_OK)

    def chosen(self):
        """(real_folder, shown), or None when cancelled."""
        return self._chosen
