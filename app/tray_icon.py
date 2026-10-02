"""
System tray icon (Settings > System tray & window).

Always present while ZBox is running, independent of the
minimize_to_tray/close_to_tray settings -- Quit, Settings and the
unread count are meant to stay reachable through the tray regardless
of whether either setting is on, since close_to_tray in particular
means the tray icon's own Quit item is one of the only two ways left
to actually exit ZBox (the other is File > Exit / Ctrl+Shift+Q; see
settings_manager.py's own docstring on close_to_tray for why that
shortcut deliberately always really quits).

No bundled artwork exists anywhere in this project (checked before
writing this: no .ico file, no wx.Icon call anywhere in app/) --
rather than source third-party icon art and take on its licensing/
attribution question, the icon is drawn from scratch with wx's own
GraphicsContext, the same reasoning sound_manager.py used to
synthesize its notification tones instead of bundling found audio.
"""

import logging
import os

import wx
import wx.adv

import lang

logger = logging.getLogger("zbox.tray")

# The ZBox logo, made from ZBox_logo.png by scripts\make_icon.py:
# 16 to 256 px in one .ico. Beside this module in app\assets from
# source; in a build zbox.spec puts it in apps_files\assets, which is
# where this module's own folder is when frozen.
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "zbox.ico")


def app_icons():
    """Every size of the ZBox logo as a wx.IconBundle, for the main
    window's title bar, taskbar and Alt+Tab. None when the file is
    missing or unreadable, so a broken asset never stops startup."""
    if not os.path.isfile(ICON_PATH):
        logger.info("No app icon at %s; using the default.", ICON_PATH)
        return None
    try:
        bundle = wx.IconBundle(ICON_PATH, wx.BITMAP_TYPE_ICO)
    except Exception:  # noqa: BLE001 - an icon is never worth a crash
        logger.warning("Could not read the app icon %s.", ICON_PATH, exc_info=True)
        return None
    if bundle.IsEmpty():
        logger.warning("The app icon %s held no usable sizes.", ICON_PATH)
        return None
    try:
        sizes = [
            bundle.GetIconByIndex(index).GetWidth()
            for index in range(bundle.GetIconCount())
        ]
        logger.info("App icon loaded: %d sizes %s.", len(sizes), sizes)
    except Exception:  # noqa: BLE001 - the log line is only a check
        logger.debug("Could not list the app icon sizes.", exc_info=True)
    return bundle


def _logo_icon():
    """The logo at the system's small-icon size, for the tray, or
    None to fall back to the drawn envelope."""
    bundle = app_icons()
    if bundle is None:
        return None
    size = wx.SystemSettings.GetMetric(wx.SYS_SMALLICON_X)
    if size <= 0:
        size = 16
    icon = bundle.GetIcon(wx.Size(size, size), wx.IconBundle.FALLBACK_NEAREST_LARGER)
    return icon if icon.IsOk() else None


def _build_icon():
    """A plain envelope glyph, drawn once into a 32x32 bitmap and
    converted to a wx.Icon. Deliberately simple -- legible at the tiny
    size Windows actually renders a tray icon at -- and deliberately
    NOT redrawn per unread count: the unread total is spoken through
    the tooltip and the context menu's own line (see refresh() below),
    not through a visual-only badge, per this project's own
    accessibility-first rule (a sighted-only indicator is not
    acceptable as the SOLE way to see the count)."""
    size = 32
    bitmap = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bitmap)
    dc.SetBackground(wx.Brush(wx.Colour(0, 0, 0, 0)))
    dc.Clear()
    gc = wx.GraphicsContext.Create(dc)
    if gc is not None:
        body_colour = wx.Colour(41, 98, 168)   # a plain, readable blue
        flap_colour = wx.Colour(255, 255, 255)
        gc.SetBrush(wx.Brush(body_colour))
        gc.SetPen(wx.Pen(wx.Colour(20, 60, 110), 2))
        gc.DrawRectangle(3, 7, 26, 20)
        path = gc.CreatePath()
        path.MoveToPoint(3, 7)
        path.AddLineToPoint(16, 19)
        path.AddLineToPoint(29, 7)
        gc.SetPen(wx.Pen(flap_colour, 2))
        gc.StrokePath(path)
    dc.SelectObject(wx.NullBitmap)
    icon = wx.Icon()
    icon.CopyFromBitmap(bitmap)
    return icon


class ZBoxTaskBarIcon(wx.adv.TaskBarIcon):
    """Wraps wx.adv.TaskBarIcon. CreatePopupMenu below is invoked
    automatically by the base class both on a right-click AND on the
    Windows context-menu key/Shift+F10 with the tray icon selected via
    keyboard -- covers "right click or the Applications key opens its
    context menu" with no extra wiring needed on this end. Left-click
    activation (EVT_TASKBAR_LEFT_DOWN) is likewise how Windows reports
    both a mouse left-click AND Enter/Space pressed on a
    keyboard-selected tray icon -- NEEDS a real runtime check that
    holds for this wx/Windows combination, same as every other
    "should just work per the platform docs" tray/AT interaction in
    this project that got a NEEDS note until someone actually tried it
    live."""

    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        self._unread_count = 0
        # Built once and kept: the glyph itself never changes, only
        # the tooltip text SetIcon carries alongside it on refresh().
        self._icon = _logo_icon() or _build_icon()
        self.SetIcon(self._icon, "ZBox")
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self._on_left_down)

    def _on_left_down(self, _event):
        self.frame.restore_from_tray()

    def refresh(self, unread_count):
        self._unread_count = max(0, int(unread_count or 0))
        if self._unread_count == 1:
            tooltip = lang.t(
                "main_ui", "tray_tooltip_one",
                default="ZBox — 1 unread message", count=self._unread_count,
            )
        elif self._unread_count:
            tooltip = lang.t(
                "main_ui", "tray_tooltip_many",
                default=f"ZBox — {self._unread_count} unread messages",
                count=self._unread_count,
            )
        else:
            tooltip = "ZBox"
        self.SetIcon(self._icon, tooltip)

    def CreatePopupMenu(self):
        menu = wx.Menu()

        open_item = menu.Append(wx.ID_ANY, lang.menu_label("tray_open", "&Open ZBox"))
        self.Bind(wx.EVT_MENU, lambda e: self.frame.restore_from_tray(), open_item)

        menu.AppendSeparator()

        if self._unread_count == 1:
            unread_label = lang.t(
                "menus", "tray_unread_one",
                default="1 unread message", count=self._unread_count,
            )
        elif self._unread_count:
            unread_label = lang.t(
                "menus", "tray_unread_many",
                default=f"{self._unread_count} unread messages",
                count=self._unread_count,
            )
        else:
            unread_label = lang.t(
                "menus", "tray_no_unread", default="No unread messages"
            )
        unread_item = menu.Append(wx.ID_ANY, unread_label)
        unread_item.Enable(False)

        menu.AppendSeparator()

        settings_item = menu.Append(
            wx.ID_ANY, lang.menu_label("tray_settings", "&Settings...")
        )
        self.Bind(wx.EVT_MENU, lambda e: self.frame.open_settings_from_tray(), settings_item)

        quit_item = menu.Append(wx.ID_ANY, lang.menu_label("tray_quit", "&Quit ZBox"))
        self.Bind(wx.EVT_MENU, lambda e: self.frame.quit_from_tray(), quit_item)

        return menu
