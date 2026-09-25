"""
Saying something out loud to a screen reader.

ZBox's normal channel for feedback is the status bar, and the audit
was right that it is unreliable: NVDA and JAWS only speak it when the
reader has status-bar reporting switched on, which is off by default.
For an action whose entire purpose is to tell you something -- "who
sent this?" -- silence is the whole failure.

A dialog is announced by every screen reader, in every configuration,
with no setup. The cost is that it interrupts, so this one dismisses
itself: it appears, is read, and goes, and focus returns to exactly
where it was.

Two details matter more than they look.

The hold time is a setting, not a constant. Screen reader speech
rates vary enormously -- somebody at 450 words a minute has heard the
whole sentence in half a second, somebody at 150 has not -- and a
dialog that closes mid-word is worse than no dialog, because it
truncates the one thing it existed to say.

The text goes in the dialog body, not the title. A screen reader
reads a dialog's title and then its focused control; putting the
announcement in both means hearing it twice.
"""

import logging

import wx

import lang
from accessible import make_read_only_viewer, wrap_text

logger = logging.getLogger("zbox.announce")

DEFAULT_HOLD_MS = 1800
MIN_HOLD_MS = 0
MAX_HOLD_MS = 6000

# Speaking without taking focus.
#
# A dialog is read by every screen reader in every configuration,
# which is why AnnouncementDialog below was built first, but it takes
# focus, moves the system caret and is visible to a sighted user --
# all wrong for something that is only reporting what just happened.
#
# The first attempt at a replacement raised the Windows live-region
# event (EVENT_OBJECT_LIVEREGIONCHANGED) on a hidden control. Tried
# live with NVDA on 16 September 2026: silent. A plain wx control
# carries no UI Automation LiveSetting for the event to mean anything
# against, and Windows reports no failure, so the app could not even
# tell it had said nothing. Removed rather than left in as a
# best-effort, because a speaker that cannot report failure makes the
# fallback below unreachable.
#
# What replaced it is accessible_output2 (MIT): it hands the text
# straight to whichever screen reader is running -- NVDA, JAWS,
# Dolphin, System Access, PC Talker, ZDSR -- through that reader's
# own client library, with Microsoft SAPI as the last resort. No
# window, no focus change, nothing on screen, and it speaks over
# whatever the reader was saying, which is what makes an action
# report land while you are already moving to the next message.
#
# The dialog below is the fallback for one case only: the library is
# not installed. It is not a second channel and never runs alongside
# this one.
_speaker = False  # False = not built yet, None = unavailable


def configure(directory):
    """Kept for the frame's startup call. The speech library finds its
    own reader libraries inside its package, so there is no folder to
    point it at any more; this only resets the cached speaker."""
    global _speaker
    _speaker = False


def _get_speaker():
    """The speech output, or None. Built once."""
    global _speaker
    if _speaker is not False:
        return _speaker
    _speaker = None
    try:
        from accessible_output2.outputs.auto import Auto

        _speaker = Auto()
        logger.info("Spoken announcements are going through %s.", type(_speaker).__name__)
    except Exception:  # noqa: BLE001 - not installed, or no output
        # it can use. Either way the dialog fallback still speaks.
        logger.info(
            "No speech output available; announcements use the dialog instead.",
            exc_info=True,
        )
    return _speaker


def warm_up():
    """Builds the speech output now instead of on the first
    announcement. The frame calls it during startup: built lazily it
    froze the window for 4.1 s, live on 24 September 2026, in the
    middle of a delete. Main thread only, like every call here, since
    the readers' COM objects belong to the thread that made them."""
    _get_speaker()


def speak(parent, text):
    """Says `text` without taking focus. True when a reader took it;
    False means the caller must fall back to AnnouncementDialog.

    interrupt=True on purpose: an action report is about what just
    happened and is worthless once the reader has moved on to the
    next row's subject line.

    parent is unused and kept in the signature so call sites do not
    have to change if a windowed path is ever needed again.
    """
    if not text:
        return False
    speaker = _get_speaker()
    if speaker is None:
        return False
    try:
        speaker.speak(str(text), interrupt=True)
        return True
    except Exception:  # noqa: BLE001 - an announcement is never worth
        # an exception; the dialog still says it.
        logger.debug("The speech output refused the text.", exc_info=True)
        return False


class AnnouncementDialog(wx.Dialog):
    """A short-lived dialog that exists to be read aloud."""

    def __init__(self, parent, text, title="ZBox", hold_ms=DEFAULT_HOLD_MS):
        super().__init__(parent, title=title, style=wx.CAPTION)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)

        sizer = wx.BoxSizer(wx.VERTICAL)

        # A read-only text control rather than a StaticText: it takes
        # focus, which is what makes the reader speak it immediately
        # and lets the user review it with arrow keys if the hold was
        # too short. make_read_only_viewer stops it announcing itself
        # as an editable field.
        self.message = wx.TextCtrl(
            self, value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE,
        )
        self.message.SetName(title)
        make_read_only_viewer(self.message)

        width = self.GetTextExtent("x" * 52).width
        line_height = self.message.GetCharHeight() or 16
        lines = max(1, text.count("\n") + 1 + len(text) // 52)
        self.message.SetMinSize((width, line_height * min(lines, 6) + line_height))

        hint = wx.StaticText(self, label="")
        wrap_text(hint, 52)

        sizer.Add(self.message, 1, wx.EXPAND | wx.ALL, 12)
        sizer.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(sizer)
        self.Centre()

        self.message.SetFocus()
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

        self._timer = None
        if hold_ms and hold_ms > 0:
            self._timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
            self._timer.Start(hold_ms, oneShot=True)

    def _on_key(self, event):
        # Any key dismisses, so nobody has to hunt for the button that
        # is not there. Arrow keys are the exception: they are how you
        # re-read the text if the hold was too short for your speech
        # rate.
        if event.GetKeyCode() in (wx.WXK_UP, wx.WXK_DOWN, wx.WXK_LEFT,
                                  wx.WXK_RIGHT, wx.WXK_HOME, wx.WXK_END):
            self._stop_timer()  # reading it means it should stay
            event.Skip()
            return
        self._finish()

    def _on_timer(self, _event):
        self._finish()

    def _stop_timer(self):
        if self._timer is not None:
            self._timer.Stop()
            self._timer = None

    def _finish(self):
        self._stop_timer()
        if self.IsModal():
            self.EndModal(wx.ID_OK)
        else:
            self.Destroy()


def announce(parent, text, title="ZBox", hold_ms=DEFAULT_HOLD_MS):
    """Says something, then puts focus back where it was.

    Focus restoration is not optional. Without it a reader is left
    standing wherever the dialog dropped them -- typically the frame
    itself -- and has to work out where they were, which for a
    keyboard-only user costs far more than the announcement was worth.
    """
    previous = wx.Window.FindFocus()
    dialog = AnnouncementDialog(parent, text, title=title, hold_ms=hold_ms)
    try:
        dialog.ShowModal()
    except Exception:
        logger.exception("Announcement dialog failed.")
    finally:
        try:
            dialog.Destroy()
        except Exception:
            pass

    if previous is not None:
        try:
            previous.SetFocus()
        except RuntimeError:
            # The window went away while the dialog was up (a tab
            # closed, a refresh replaced the list). Nothing to restore.
            pass
