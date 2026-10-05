"""
Cache clean up. Its controls are on the Maintenance and Cleanup tab of
Settings (settings_dialog), which Tools > Cache Clean Up Configuration
opens; this module keeps the helpers they and the daily clean up use.

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

