"""
The first-run language page, shown before the license agreement on a
fresh start (see main_frame.ZBoxApp._show_eula_if_needed).

One list, Next and Cancel. The list names each language the way its
own speakers write it, and comes preselected with the language that
matches Windows' own display language, or English when none does
(lang.match_system_language). So on most machines the person only
has to press Enter.

Changing the selection loads that language at once and relabels the
page, so someone who does not read English hears the rest of this
page, and the license page after it, in their own language. Cancel,
Escape, Ctrl+W and Ctrl+F4 all mean Cancel, which the caller treats
exactly as I Disagree on the license page: ZBox closes without
starting.

Every word on the page reuses a key the language files already carry
(set_language_label, eula_next, btn_cancel_plain), so adding this
page needed no translation work. The title is the product name.
"""

import wx

import lang

from accessible import bind_close_accelerators, fit_dialog


class LanguageDialog(wx.Dialog):
    """See module docstring. languages is lang.available()'s list of
    (code, name); selected_code is the code to preselect."""

    def __init__(self, parent, lang_dir, languages, selected_code):
        super().__init__(parent, title="ZBox", style=wx.CAPTION)
        self._lang_dir = lang_dir
        self._languages = list(languages) or [(lang.DEFAULT_CODE, "English")]
        codes = [code for code, _name in self._languages]
        if selected_code not in codes:
            selected_code = lang.DEFAULT_CODE if lang.DEFAULT_CODE in codes else codes[0]

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.label = wx.StaticText(self, label="")
        sizer.Add(self.label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.choice = wx.Choice(
            self, choices=[name for _code, name in self._languages]
        )
        self.choice.SetSelection(codes.index(selected_code))
        self.choice.Bind(wx.EVT_CHOICE, self._on_choice)
        sizer.Add(self.choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.cancel_button = wx.Button(self, wx.ID_CANCEL, "")
        self.next_button = wx.Button(self, wx.ID_OK, "")
        self.next_button.SetDefault()
        self.next_button.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_OK))
        self.cancel_button.Bind(
            wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CANCEL)
        )
        buttons.Add(self.cancel_button, 0, wx.RIGHT, 8)
        buttons.Add(self.next_button, 0)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 12)

        bind_close_accelerators(self, wx.ID_CANCEL)

        self._load(selected_code)
        fit_dialog(self, sizer)
        wx.CallAfter(self.choice.SetFocus)

    def selected_code(self):
        index = self.choice.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._languages):
            return lang.DEFAULT_CODE
        return self._languages[index][0]

    def _on_choice(self, event):
        self._load(self.selected_code())
        event.Skip()

    def _load(self, code):
        """Loads code as the interface language and relabels the page
        in it. Mirroring for a right-to-left language is best effort
        on a page already built; the license page after this one is
        built fresh and mirrors fully."""
        lang.load(self._lang_dir, code)
        label = lang.control_label("set_language_label", "Interface &language:")
        self.label.SetLabel(label)
        self.choice.SetName(label.replace("&", ""))
        self.next_button.SetLabel(lang.control_label("eula_next", "&Next"))
        self.cancel_button.SetLabel(
            lang.t("dialogs", "btn_cancel_plain", default="Cancel")
        )
        direction = (
            wx.Layout_RightToLeft if lang.is_rtl() else wx.Layout_LeftToRight
        )
        try:
            self.SetLayoutDirection(direction)
            for child in self.GetChildren():
                child.SetLayoutDirection(direction)
        except Exception:  # noqa: BLE001 -- never stop the page over mirroring
            pass
        self.Layout()
