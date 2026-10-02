"""
Unified Folders selection dialog, reached from View > Unified
Folders. Each checked folder type becomes one combined subtree entry
in the account tree, aggregating that folder across every account,
for example a single Inbox node that combines all accounts' Inboxes.
"""

import wx

import lang
from accessible import fit_dialog, wrap_text
from settings_manager import FOLDER_TYPES


class UnifiedFoldersDialog(wx.Dialog):
    def __init__(self, parent, settings):
        super().__init__(parent,
                         title=lang.t(
                             "dialogs", "title_unified_folders",
                             default="Unified Folders",
                         ),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.settings = settings

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "unified_note_intro",
                default=(
                    "Choose which folders to combine across every account. "
                    "Each one checked becomes a single entry in the tree "
                    "that combines that folder from all your accounts."
                ),
            ),
        )
        wrap_text(intro, 46)
        sizer.Add(intro, 0, wx.ALL, 10)

        self.checkboxes = {}
        for folder_type in FOLDER_TYPES:
            checkbox = wx.CheckBox(self, label=folder_type)
            checkbox.SetValue(folder_type in settings.unified_folder_types)
            sizer.Add(checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
            self.checkboxes[folder_type] = checkbox

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)

    def apply_to_settings(self):
        self.settings.unified_folder_types = [
            folder_type
            for folder_type, checkbox in self.checkboxes.items()
            if checkbox.GetValue()
        ]
        return self.settings
