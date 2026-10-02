"""
Windows desktop (toast/Action Center) notifications for new mail.

Deliberately separate from sound_manager.py: that module is about
audio playback and winsound's own quirks, this one is about
wx.adv.NotificationMessage and its own. The two share a shape (a
settings-gated, account-gated, never-raise call triggered from the
same new-mail path in mail_fetch.py) but nothing else.

Gating, in order:
  1. Settings.desktop_notifications_enabled (Settings > Sound &
     Notifications) -- app-wide master switch, off turns every
     account's toast off, mirroring sounds_enabled.
  2. Account.notifications_muted (Account Settings' "Mute
     notifications for this account", or the Accounts menu / account
     context menu's "Mute Account" toggle) -- per-account override.
Both are checked here rather than by each caller, the same way
sound_manager.play checks sounds_enabled itself: one place callers
can't forget to gate correctly.

wx.adv.NotificationMessage.Show() hands the notification off to the
OS (on Windows 10+, a real Action Center toast when the underlying
wxWidgets/wxPython build supports it, otherwise a balloon fallback --
not this module's concern either way). sound_manager.play's own
docstring documents a real bug class here: a wx object built as a
purely local variable can be garbage-collected before an asynchronous
OS hand-off finishes using it. NotificationMessage is not documented
as strictly requiring this, but the risk is the same shape and the
fix is nearly free, so instances are kept alive in _live_notifications
for a short while rather than trusted to outlive their own Show()
call on nothing but luck.
"""

import logging
import lang

try:
    import wx.adv
    NOTIFICATIONS_AVAILABLE = True
except ImportError:
    # Not expected on ZBox's own Windows target (wx.adv is already
    # required for the account wizard's Wizard control), but bound
    # defensively the same way sound_manager binds winsound to None,
    # so a missing/odd wx build degrades to silence instead of a
    # crash.
    NOTIFICATIONS_AVAILABLE = False

logger = logging.getLogger("zbox.notifications")

# Strong references to recently-shown notifications, newest last.
# Capped rather than unbounded: nothing here needs to outlive the
# next few notifications, only to survive long enough for the OS to
# actually finish displaying it.
_live_notifications = []
_MAX_LIVE_NOTIFICATIONS = 5

TIMEOUT_SECONDS = 10


def show(settings, account, title, message):
    """
    Shows a Windows desktop notification, unless
    settings.desktop_notifications_enabled is off, this account is
    muted, wx.adv isn't available, or displaying it fails outright.
    Always a silent no-op on any of those rather than a dialog or a
    raised exception -- the same reasoning sound_manager.play and
    announce() both document: a decorative notification is never
    worth interrupting anything over.
    """
    if not getattr(settings, "desktop_notifications_enabled", True):
        return
    if account is not None and getattr(account, "notifications_muted", False):
        return
    if not NOTIFICATIONS_AVAILABLE:
        return

    try:
        notification = wx.adv.NotificationMessage(title, message)
        notification.SetFlags(wx.ICON_INFORMATION)
        notification.Show(timeout=TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Could not show desktop notification.", exc_info=True)
        return

    _live_notifications.append(notification)
    del _live_notifications[:-_MAX_LIVE_NOTIFICATIONS]


def show_new_mail(settings, account, new_envelopes):
    """
    The specific new-mail case mail_fetch.py calls on every
    genuinely-new arrival batch, phrased the same way
    main_frame._announce_new_mail speaks it: one notification per
    batch of new arrivals, not one per message.
    """
    count = len(new_envelopes)
    noun = lang.t('main_ui', 'notify_noun_one', default="message") if count == 1 else lang.t('main_ui', 'notify_noun_many', default="messages")
    name = account.display_name or account.identity_email
    show(settings, account, lang.t('main_ui', 'notify_new_mail_title', default="New Mail"), lang.t('main_ui', 'notify_new_mail_body', default='{count} new {noun} for {name}.', count=count, noun=noun, name=name))
