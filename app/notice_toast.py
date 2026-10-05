"""
A notice that is seen and heard, and goes by itself.

It replaced every information-only popup with an OK button: file
attached, attachment saved, master password set, select a contact
first, and the like. Pressing OK after a message that only reports
what happened is an extra step for everyone, and for a screen reader
user it also moves focus away and back.

notify() does three things:
1. shows a small notice with the title and the text, at the bottom of
   the window it belongs to, with no buttons and no taskbar entry. It
   never takes focus, so the cursor stays exactly where it was. It
   closes by itself after three seconds, longer for longer text, up to
   ten; a click closes it early.
2. writes the text to the status bar, through the main window's
   announce().
3. speaks it there, when spoken announcements are on
   (spoken_feedback).

The notice is owned by the main window, not by a dialog, so it is not
destroyed with a dialog that closes right after reporting something.
"""

import logging

import wx

import lang
import spoken_feedback

logger = logging.getLogger("zbox.notice_toast")

MIN_MS = 3000
MAX_MS = 10000
PER_CHAR_MS = 60
WRAP_CHARS = 60


def hold_ms(text):
    """How long the notice stays: three seconds, plus time for longer
    text, never more than ten."""
    return max(MIN_MS, min(MAX_MS, 1500 + PER_CHAR_MS * len(str(text or ""))))


def _main_frame(window):
    """The main window: the nearest parent whose class has announce(),
    or the application's top window."""
    current = window
    while current is not None:
        if callable(getattr(type(current), "announce", None)):
            return current
        try:
            current = current.GetParent()
        except Exception:  # noqa: BLE001
            break
    try:
        top = wx.GetApp().GetTopWindow()
    except Exception:  # noqa: BLE001
        return None
    if top is not None and callable(getattr(type(top), "announce", None)):
        return top
    return None


def _placement_window(window, frame):
    """The window the notice sits over: the dialog or frame window
    belongs to, when it is showing; otherwise the main window."""
    try:
        top = window.GetTopLevelParent() if window is not None else None
        if top is not None and top.IsShown():
            return top
    except Exception:  # noqa: BLE001
        pass
    return frame


class _Notice(wx.Frame):
    def __init__(self, owner, place, text, title):
        style = wx.FRAME_NO_TASKBAR | wx.FRAME_TOOL_WINDOW | wx.STAY_ON_TOP | wx.BORDER_SIMPLE
        if owner is not None:
            style |= wx.FRAME_FLOAT_ON_PARENT
        super().__init__(owner, title=title, style=style)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_INFOBK))
        heading = wx.StaticText(panel, label=title)
        font = heading.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        heading.SetFont(font)
        body = wx.StaticText(panel, label=text)
        for control in (heading, body):
            control.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_INFOTEXT))
        body.Wrap(panel.GetTextExtent("x" * WRAP_CHARS).width)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(heading, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        sizer.Add(body, 0, wx.ALL, 12)
        panel.SetSizer(sizer)
        sizer.Fit(panel)
        self.SetClientSize(panel.GetSize())
        self._place(place)
        for control in (self, panel, heading, body):
            control.Bind(wx.EVT_LEFT_UP, self._on_click)
        self.ShowWithoutActivating()
        wx.CallLater(hold_ms(text), self._close)

    def _place(self, place):
        try:
            area = place.GetScreenRect() if place is not None else wx.GetClientDisplayRect()
        except Exception:  # noqa: BLE001
            area = wx.GetClientDisplayRect()
        width, height = self.GetSize()
        x = area.x + max(0, (area.width - width) // 2)
        y = area.y + max(0, area.height - height - 48)
        self.SetPosition((x, y))

    def _on_click(self, _event):
        self._close()

    def _close(self):
        try:
            if self:
                self.Destroy()
        except RuntimeError:
            pass  # already gone with its owner


def notify(window, text, title="ZBox"):
    """Shows, writes and, when speech is on, speaks a notice that needs
    no answer. window is where it came from; None means the main
    window."""
    text = str(text or "")
    if not text:
        return
    frame = _main_frame(window)
    try:
        _Notice(frame, _placement_window(window, frame), text, title)
    except Exception:  # noqa: BLE001 - a notice is never worth an error
        logger.debug("The notice could not be shown.", exc_info=True)
    if frame is not None:
        try:
            frame.announce(text, title=title)
            return
        except Exception:  # noqa: BLE001
            logger.debug("The notice could not be announced.", exc_info=True)
    spoken_feedback.speak(window, text)
