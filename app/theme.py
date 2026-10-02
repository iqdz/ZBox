"""
The interface theme: Follow system, Light, Dark, and three dark
palettes that are easier on the eyes than plain black and white.

wxWidgets 3.3 (wxPython 4.3) can draw a whole Windows app dark --
menus, scroll bars, lists, tree, dialogs -- through
wx.App.MSWEnableDarkMode. Windows only allows that before the first
window exists, so apply_theme runs once at the top of
ZBoxApp.OnInit and a change in Settings takes effect after a restart.

Screen readers are not affected: this changes colours only, never a
name, a role or the Tab order. Windows High Contrast always wins; if
it is on, no theme is applied, because a low-vision user chose those
exact colours.

Every palette only replaces system colours. Panes already paint
themselves with the system window colour (accessible.content_background),
so they follow whichever theme is in force without knowing about it.
"""

import logging

logger = logging.getLogger("zbox.theme")

# Colour sets for the dark palettes, as #RRGGBB. "dark" uses wx's own
# dark colours and needs no entry here.
PALETTES = {
    # Deep navy with soft blue-white text.
    "midnight": {
        "window": "#0F1B2D", "text": "#DCE6F2", "face": "#16243A",
        "highlight": "#2F6FB3", "highlight_text": "#FFFFFF", "gray": "#8FA3BF",
    },
    # Brown tones with cream text, low in blue light for evenings.
    "warm": {
        "window": "#221C17", "text": "#EADBC8", "face": "#2E2620",
        "highlight": "#8A5A2B", "highlight_text": "#FFF8EE", "gray": "#A89886",
    },
    # Charcoal with off-white text and a calm green selection.
    "soft": {
        "window": "#2A2D31", "text": "#F2EFE8", "face": "#33373C",
        "highlight": "#4F7A6A", "highlight_text": "#FFFFFF", "gray": "#A7ABB0",
    },
}


def high_contrast_on():
    """Whether Windows High Contrast is switched on. False when it
    cannot be read."""
    try:
        import ctypes
        from ctypes import wintypes

        class HIGHCONTRAST(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwFlags", wintypes.DWORD),
                ("lpszDefaultScheme", wintypes.LPWSTR),
            ]

        info = HIGHCONTRAST()
        info.cbSize = ctypes.sizeof(HIGHCONTRAST)
        SPI_GETHIGHCONTRAST = 0x0042
        HCF_HIGHCONTRASTON = 0x00000001
        if ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETHIGHCONTRAST, info.cbSize, ctypes.byref(info), 0
        ):
            return bool(info.dwFlags & HCF_HIGHCONTRASTON)
    except Exception:  # noqa: BLE001 -- not Windows, or no such call
        pass
    return False


def _palette_settings(wx, palette):
    """A wx.DarkModeSettings that answers from palette, or None when
    this wx build cannot be customised; the plain dark colours are
    used then."""
    base = getattr(wx, "DarkModeSettings", None)
    if base is None:
        return None

    colour = {name: wx.Colour(value) for name, value in palette.items()}
    mapping = {}
    for sys_name, key in (
        ("SYS_COLOUR_WINDOW", "window"),
        ("SYS_COLOUR_LISTBOX", "window"),
        ("SYS_COLOUR_WINDOWTEXT", "text"),
        ("SYS_COLOUR_LISTBOXTEXT", "text"),
        ("SYS_COLOUR_BTNTEXT", "text"),
        ("SYS_COLOUR_MENUTEXT", "text"),
        ("SYS_COLOUR_CAPTIONTEXT", "text"),
        ("SYS_COLOUR_BTNFACE", "face"),
        ("SYS_COLOUR_MENU", "face"),
        ("SYS_COLOUR_MENUBAR", "face"),
        ("SYS_COLOUR_APPWORKSPACE", "face"),
        ("SYS_COLOUR_HIGHLIGHT", "highlight"),
        ("SYS_COLOUR_MENUHILIGHT", "highlight"),
        ("SYS_COLOUR_HIGHLIGHTTEXT", "highlight_text"),
        ("SYS_COLOUR_GRAYTEXT", "gray"),
    ):
        index = getattr(wx, sys_name, None)
        if index is not None:
            mapping[int(index)] = colour[key]

    class _PaletteSettings(base):
        def GetColour(self, index):
            try:
                found = mapping.get(int(index))
            except Exception:  # noqa: BLE001
                found = None
            if found is not None:
                return found
            return super().GetColour(index)

    try:
        return _PaletteSettings()
    except Exception:  # noqa: BLE001
        logger.warning("Could not build the theme palette.", exc_info=True)
        return None


def apply_theme(app, choice):
    """Switches the chosen theme on. Call once, before any window is
    created. Never raises: a theme must never stop ZBox starting.
    Returns what was actually applied, for the log."""
    choice = str(choice or "system").lower()
    if choice == "light":
        logger.info("Interface theme: light.")
        return "light"
    if high_contrast_on():
        logger.info("Interface theme: Windows High Contrast is on; theme not applied.")
        return "high_contrast"
    try:
        import wx

        # An instance method in wxPython: calling it on the wx.App
        # class raises "first argument of unbound method".
        enable = getattr(app, "MSWEnableDarkMode", None)
        if enable is None:
            logger.info("Interface theme: this wx build has no dark mode; staying light.")
            return "light"
        if choice == "system":
            flags = getattr(wx.App, "DarkMode_Auto", 0)
            ok = enable(flags)
        else:
            flags = getattr(wx.App, "DarkMode_Always", 1)
            settings = None
            if choice in PALETTES:
                settings = _palette_settings(wx, PALETTES[choice])
                # Kept alive for the life of the app: wx reads colours
                # from it every time a control is drawn.
                app._theme_settings = settings
            ok = enable(flags, settings) if settings is not None else enable(flags)
        logger.info("Interface theme: %s (dark mode %s).", choice,
                    "on" if ok else "not available")
        return choice if ok else "light"
    except Exception:  # noqa: BLE001
        logger.warning("Could not apply the interface theme.", exc_info=True)
        return "light"
