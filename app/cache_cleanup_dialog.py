"""
Tools > Cache Clean Up Configuration.

Two controls, both about ZBox's local copies of mail and never about
the mail itself:

* Clear Offline Message Cache Now -- the whole-cache reset that used
  to sit directly on the Tools menu. Ctrl+Shift+Delete still runs it
  from anywhere; the key moved to the frame's accelerator table when
  the menu item moved here.

* Automatic clean up by message date -- once a day, while ZBox is
  idle, cached message bodies and offline copies older than the
  chosen age are deleted, judged by each message's own Date header.
  That includes messages still in the account: the point is a cache
  that stays bounded, and anything removed is downloaded again the
  next time it is opened.

The age is picked from a native single-choice list, four fixed
values, which reads more plainly to a screen reader than a number
field with an unexplained range.
"""

import wx

import lang
from accessible import fit_dialog, wrap_text
from settings_manager import CACHE_AGE_CHOICES

# ShowModal result for "run Clear Offline Message Cache now". A plain
# return code, above wx's stock id range. The frame runs the clear
# after this dialog is destroyed, so the confirmation and the spoken
# result are never parented to a window hidden behind a modal.
ID_CLEAR_NOW = 6101

CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
CLEANUP_IDLE_SECONDS = 10 * 60


def age_choice_label(days):
    return "1 day" if days == 1 else "%d days" % days


def age_button_label(days):
    return lang.t('dialogs', 'cc_auto_button', default="Automatic clean up: cached messages older than %s...") % age_choice_label(days)


def cleanup_due(now, last_run, idle_seconds, message_tab_open):
    """
    Whether the daily clean up should run on this timer tick. now and
    last_run are time.time() values; idle_seconds is how long since
    ZBox last saw a keypress.

    Never while a message tab is open: reading in the HTML view with a
    screen reader sends ZBox no keys, so idle time alone would call
    someone mid-read idle. A last_run in the future means the clock
    was set back, and is treated as due rather than blocking the
    clean up until the clock catches up; an unreadable one counts as
    never run.
    """
    if message_tab_open:
        return False
    if idle_seconds < CLEANUP_IDLE_SECONDS:
        return False
    try:
        last = float(last_run or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if last > now:
        return True
    return now - last >= CLEANUP_INTERVAL_SECONDS


class CacheCleanupDialog(wx.Dialog):
    def __init__(self, parent, settings):
        super().__init__(parent,
                         title=lang.t(
                             "dialogs", "title_cache_cleanup",
                             default="Cache Clean Up Configuration",
                         ),
                         style=wx.DEFAULT_DIALOG_STYLE)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.settings = settings
        self.selected_days = settings.cache_max_message_age_days

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, label=lang.t(
            "dialogs", "cache_note_intro",
            default=(
                "These remove ZBox's local copies only. Nothing is deleted "
                "from any mail server, and a removed message is downloaded "
                "again the next time you open it."
            ),
        ))
        wrap_text(intro, 46)
        sizer.Add(intro, 0, wx.ALL, 10)

        self.clear_button = wx.Button(
            self,
            label=lang.t(
                "dialogs", "cache_clear_now",
                default="Clear Offline Message Cache Now... (Ctrl+Shift+Delete)",
            ),
        )
        self.clear_button.Bind(wx.EVT_BUTTON, self._on_clear_now)
        sizer.Add(self.clear_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        age_note = wx.StaticText(self, label=lang.t(
            "dialogs", "cache_note_age",
            default=(
                "Automatic clean up runs once a day, when ZBox has had no "
                "keyboard activity for ten minutes and no message is open. "
                "It goes by each message's own date, including messages "
                "that are still in your account."
            ),
        ))
        wrap_text(age_note, 46)
        sizer.Add(age_note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.age_button = wx.Button(self, label=age_button_label(self.selected_days))
        self.age_button.Bind(wx.EVT_BUTTON, self._on_choose_age)
        sizer.Add(self.age_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)

    def _on_clear_now(self, event):
        self.EndModal(ID_CLEAR_NOW)

    def _on_choose_age(self, event):
        labels = [
            lang.t('dialogs', 'cc_age_choice', default="Every message cache %s old") % age_choice_label(days)
            for days in CACHE_AGE_CHOICES
        ]
        chooser = wx.SingleChoiceDialog(
            self,
            lang.t(
                "dialogs", "cache_age_prompt",
                default="Automatically remove cached messages older than:",
            ),
            lang.t(
                "dialogs", "title_automatic_cleanup",
                default="Automatic Clean Up",
            ),
            labels,
        )
        if self.selected_days in CACHE_AGE_CHOICES:
            chooser.SetSelection(CACHE_AGE_CHOICES.index(self.selected_days))
        if chooser.ShowModal() == wx.ID_OK:
            self.selected_days = CACHE_AGE_CHOICES[chooser.GetSelection()]
            self.age_button.SetLabel(age_button_label(self.selected_days))
        chooser.Destroy()
        self.age_button.SetFocus()

    def apply_to_settings(self):
        self.settings.cache_max_message_age_days = self.selected_days
        return self.settings
