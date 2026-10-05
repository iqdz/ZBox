"""
Settings dialog, reached from Tools > Settings, in seven tabs. Options:
- whether to write the debug log file at all;
- the default message body view (rendered HTML, or plain Text) used
  when a message opens in its own tab;
- how long (0-2000 ms) ZBox holds a synthetic Ctrl press after an
  HTML message opens, to silence the screen reader's browser
  announcement (on by default; 0 = off);
- how long (a level from 1 to 10, 100-1000 ms) ZBox waits after an
  HTML message's page reports itself loaded before handing it
  keyboard focus, giving Windows' accessibility layer time to finish
  building that page's accessible tree first;
- which engine renders the HTML message view (Edge/WebView2 or
  legacy MSHTML);
- which WebView2 runtime the Edge backend is pointed at: a fixed
  version bundled with ZBox, or the system-wide Evergreen one that
  Windows Update services on its own schedule;
- where "Save All" writes attachments when a message has more than
  one;
- whether bare S, A, J, W and K in the message list flag, archive,
  mark junk, watch or ignore immediately, or leave those keys to
  normal list type-ahead;
- whether notification sounds play at all, and which sound
  theme (folder under data/sounds/) supplies them;
- how many days Trash and Junk keep old mail before it is
  automatically and permanently purged;
- creating (or repairing, after the folder is moved) a ZBox
  shortcut on the Desktop.
"""

import os

import wx

from about_dialog import AboutPanel
from accessible import _fit_scrolled_children, fit_dialog, wrap_text
import lang
import notice_toast
import spoken_feedback
import sound_manager
from cache_cleanup_dialog import age_button_label, age_choice_label
from settings_manager import CACHE_AGE_CHOICES, UI_THEMES
from message_view_panel import DEFAULT_READING_FONT_POINT_SIZE, clamp_reading_font_size


# The values each choice list offers, in the units the settings store.
SILENCE_VALUES = (0,) + tuple(range(100, 2001, 100))      # milliseconds
FOCUS_DELAY_LEVELS = tuple(range(1, 11))                   # level x 100 ms
ANNOUNCE_VALUES = (0,) + tuple(range(300, 6001, 300))     # milliseconds
AUTOSAVE_VALUES = (0, 15, 30, 60, 120, 180, 300, 600)     # seconds
PURGE_VALUES = (0, 1, 7, 14, 30, 60, 90, 180, 365)        # days


def _seconds_text(ms):
    """A millisecond value as the seconds a person would say."""
    seconds = ms / 1000.0
    if seconds == 1:
        return lang.t("dialogs", "set_val_one_second", default="1 second")
    return lang.t("dialogs", "set_val_seconds", default="{seconds} seconds",
                  seconds="%g" % seconds)


def _silence_text(ms):
    if ms == 0:
        return lang.t("dialogs", "set_silence_off", default="Off")
    return _seconds_text(ms)


def _focus_text(level):
    return _seconds_text(level * 100)


def _announce_text(ms):
    if ms == 0:
        return lang.t("dialogs", "set_announce_wait", default="Waits for a key")
    return _seconds_text(ms)


def _autosave_text(seconds):
    if seconds == 0:
        return lang.t('dialogs', 'set_autosave_off', default="Off \u2014 use Save Draft (Ctrl+S) to save manually")
    if seconds < 60:
        return lang.t('dialogs', 'set_autosave_seconds', default='Every {seconds} seconds', seconds=seconds)
    if seconds == 60:
        return lang.t("dialogs", "set_autosave_one_minute", default="Every minute")
    return lang.t("dialogs", "set_autosave_minutes", default="Every {minutes} minutes",
                  minutes="%g" % (seconds / 60.0))


def _purge_text(days):
    if days == 0:
        return lang.t('dialogs', 'set_purge_off', default="Off \u2014 Trash and Junk are never auto-purged")
    if days == 1:
        return lang.t('dialogs', 'set_purge_one_day', default="After 1 day")
    return lang.t('dialogs', 'set_purge_days', default='After {days} days', days=days)


def _values_with(values, current):
    """The offered values, plus the stored one in its place when it is
    not among them, so opening Settings never changes it unseen."""
    values = list(values)
    if current not in values:
        values.append(current)
        values.sort()
    return values


def _value_choice(parent, values, current, text_of, label):
    """A choice list of real values in place of a slider: a screen
    reader says the value itself, never a percentage. Named after its
    own label, without the colon."""
    values = _values_with(values, current)
    choice = wx.Choice(parent, choices=[text_of(value) for value in values])
    choice.SetSelection(values.index(current))
    choice.SetName(label.GetLabel().replace("&", "").rstrip(": \uff1a"))
    choice._zbox_values = values
    return choice


def _chosen_value(choice, fallback):
    values = getattr(choice, "_zbox_values", None) or []
    index = choice.GetSelection()
    if 0 <= index < len(values):
        return values[index]
    return fallback


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, settings, page=None):
        super().__init__(parent,
                         title=lang.t("dialogs", "title_settings", default="Settings"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.settings = settings

        # Seven categories, General first and About ZBox last, in a
        # standard Windows list on the left, each category's settings
        # beside it. A list, not a tab row: in dark mode wx paints a tab
        # row itself, and JAWS and NVDA then read every freshly painted
        # tab title on each switch; a list stays a native control in
        # light and dark mode, and the reader says only the item landed
        # on. Up and Down Arrow move between categories; Ctrl+Tab and
        # Ctrl+Shift+Tab work from anywhere in the dialog
        # (_on_tab_keys). Each category is a plain panel holding a
        # scrolling area: at 150 or 200 percent Windows text scaling it
        # can be taller than the screen, which would put OK and Cancel
        # below the bottom edge, unreachable by keyboard as well as by
        # mouse. Each block below is built in the scrolling area of the
        # category it belongs to (_page), in the order it is read there.
        self.settings_tabs = wx.Listbook(self, style=wx.LB_LEFT)
        self._pages = {}
        self._scrollers = {}
        for key, title in (
            ("general", lang.t("dialogs", "set_tab_general", default="General")),
            ("personalization", lang.t("dialogs", "set_tab_personalization", default="Personalization")),
            ("accessibility", lang.t("dialogs", "set_tab_accessibility", default="Accessibility and Shortcut Keys")),
            ("maintenance", lang.t("dialogs", "set_tab_maintenance", default="Maintenance and Cleanup")),
            ("sounds", lang.t("dialogs", "set_tab_sounds", default="Sounds and Notifications")),
            ("security", lang.t("dialogs", "set_tab_security", default="Security")),
        ):
            tab_page = wx.Panel(self.settings_tabs)
            scroller = wx.ScrolledWindow(tab_page, style=wx.VSCROLL)
            scroller.SetScrollRate(0, 12)
            scroller.SetSizer(wx.BoxSizer(wx.VERTICAL))
            holder = wx.BoxSizer(wx.VERTICAL)
            holder.Add(scroller, 1, wx.EXPAND)
            tab_page.SetSizer(holder)
            self.settings_tabs.AddPage(tab_page, title)
            self._pages[key] = tab_page
            self._scrollers[key] = scroller
        panel, sizer = self._page("general")
        # Interface language. Built from whatever files are in the
        # lang folder, the same way the sound theme choice is built
        # from the theme folders, so adding a language never means
        # changing code. Each entry is named the way its own speakers
        # write it, from the file's own meta section.
        lang_dir = getattr(getattr(parent, "paths", None), "lang_dir", None)
        self._languages = lang.available(lang_dir) if lang_dir else []
        if not self._languages:
            self._languages = [(lang.DEFAULT_CODE, "English")]
        only_english = len(self._languages) <= 1

        language_label_text = lang.control_label(
            "set_language_label_only_english",
            "Interface &language: (English only)",
        ) if only_english else lang.control_label(
            "set_language_label", "Interface &language:"
        )
        self.language_label = wx.StaticText(panel, label=language_label_text)

        self.language_choice = wx.Choice(
            panel, choices=[name for _code, name in self._languages]
        )
        self.language_choice.SetName(language_label_text.replace("&", ""))
        current_language = getattr(settings, "language", lang.DEFAULT_CODE)
        codes = [code for code, _name in self._languages]
        if current_language not in codes:
            current_language = codes[0]
        self.language_choice.SetSelection(codes.index(current_language))
        # Never disabled, even with English alone. A disabled control
        # is skipped by the Tab order completely and its label is a
        # static text that never takes focus, so to a keyboard user it
        # does not exist at all. The label says "English only" and the
        # note below says how to add a language; both are heard
        # because the control itself can still be reached.

        language_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_language_note",
                default="Takes effect the next time ZBox starts. Language files "
                "are plain text files in ZBox's lang folder, beside the "
                "sounds folder. To add one, copy the en folder, rename "
                "the copy and the .lang file inside it to your language "
                "code, and translate the text to the right of each "
                "equals sign. Anything not yet translated is spoken in "
                "English rather than left silent, so a part-finished "
                "file is safe to use.",
            ),
        )
        wrap_text(language_note)

        sizer.Add(self.language_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(self.language_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(language_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.debug_checkbox = wx.CheckBox(
            panel,
            label=lang.t(
                "dialogs", "set_debug_logging",
                default="(Enable debug logging saved to: data\\logs)",
            ),
        )
        self.debug_checkbox.SetValue(settings.debug_logging)

        note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_debug_note",
                default="Debug logging helps diagnose problems if something goes "
                "wrong. Turning it off stops new entries from being "
                "written; existing log files are not deleted. Takes "
                "effect the next time ZBox starts.",
            ),
        )
        note

        sizer.Add(self.debug_checkbox, 0, wx.ALL, 10)
        wrap_text(note)
        sizer.Add(note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("personalization")

        # Interface theme. Colours only: names, roles and the Tab
        # order are the same in every theme. See theme.py.
        theme_names = {
            "system": lang.t("dialogs", "set_theme_system", default="Follow system"),
            "light": lang.t("dialogs", "set_theme_light", default="Light"),
            "dark": lang.t("dialogs", "set_theme_dark", default="Dark"),
            "midnight": lang.t("dialogs", "set_theme_midnight", default="Midnight blue"),
            "warm": lang.t("dialogs", "set_theme_warm", default="Warm dark"),
            "soft": lang.t("dialogs", "set_theme_soft", default="Soft contrast"),
        }
        self._ui_themes = list(UI_THEMES)
        theme_label_text = lang.control_label("set_ui_theme_label", "Interface &theme:")
        theme_label = wx.StaticText(panel, label=theme_label_text)
        self.ui_theme_choice = wx.Choice(
            panel, choices=[theme_names[code] for code in self._ui_themes]
        )
        self.ui_theme_choice.SetName(theme_label_text.replace("&", ""))
        current_theme = getattr(settings, "ui_theme", "system")
        if current_theme not in self._ui_themes:
            current_theme = "system"
        self.ui_theme_choice.SetSelection(self._ui_themes.index(current_theme))
        theme_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_ui_theme_note",
                default="Colours for menus, lists, dialogs and the Text message "
                "view. Follow system uses Windows' own light or dark app mode. "
                "Midnight blue, Warm dark and Soft contrast are dark themes "
                "with gentler colours. HTML messages keep their own colours, "
                "and Windows High Contrast always takes priority. Takes effect "
                "the next time ZBox starts.",
            ),
        )
        wrap_text(theme_note)
        sizer.Add(theme_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(self.ui_theme_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(theme_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.headers_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_headers_checkbox",
                "Show &message headers above an open message",
            ),
        )
        self.headers_checkbox.SetValue(
            getattr(settings, "show_message_headers", True)
        )

        headers_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_headers_note",
                default="From, To, Cc, Date and Subject, shown above the "
                "message body in both Text and HTML view. Without it "
                "there is no way to tell who sent a message from "
                "inside it without closing the tab and going back to "
                "the list. Focus still lands in the message body; the "
                "header block is one Shift+Tab away. Takes effect the "
                "next time a message is opened.",
            ),
        )
        wrap_text(headers_note)

        sizer.Add(self.headers_checkbox, 0, wx.ALL, 10)
        sizer.Add(headers_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        zoom_label = wx.StaticText(
            panel,
            label=lang.t("dialogs", "set_zoom_label", default="Message font and zoom:"),
        )
        zoom_label.SetFont(zoom_label.GetFont().Bold())

        self.reading_font_override_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_font_override", "&Override the default message font size"
            ),
        )
        current_reading_size = getattr(settings, "reading_font_size", None)
        self.reading_font_override_checkbox.SetValue(current_reading_size is not None)
        self.reading_font_override_checkbox.Bind(
            wx.EVT_CHECKBOX, self._on_reading_font_override_checkbox
        )

        self.reading_font_size_spin = wx.SpinCtrl(
            panel, min=6, max=72,
            initial=clamp_reading_font_size(
                current_reading_size or DEFAULT_READING_FONT_POINT_SIZE
            ),
        )
        self.reading_font_size_spin.SetName(
            lang.t(
                "dialogs", "set_font_size_name", default="Message font size (points)"
            )
        )
        self.reading_font_size_spin.Enable(current_reading_size is not None)

        # Created before its field, like every other label here: a
        # screen reader names a field from the label that precedes it
        # in creation order, and this one used to be created after it.
        reading_font_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_reading_font_label",
                default="Reading font (blank for default):",
            ),
        )
        self.reading_font_family_field = wx.TextCtrl(
            panel, value=getattr(settings, "reading_font_family", "") or ""
        )
        self.reading_font_family_field.SetName(
            lang.t("dialogs", "set_reading_font_name", default="Reading font")
        )

        zoom_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_zoom_note",
                default="Makes the open message's own text bigger or smaller, "
                "independent of the rest of ZBox and of Windows' own "
                "display scaling. Applies to both the Text and HTML "
                "views. “Reading font” optionally forces a "
                "specific font face (e.g. Consolas, OpenDyslexic) for "
                "the message body; leave it blank to keep the normal "
                "font. Ctrl+Shift+= / Ctrl+Shift+- / Ctrl+Shift+0 in "
                "the View menu raise, lower or reset just the font "
                "size without opening this dialog, and apply "
                "immediately to every open message. Changes made here "
                "also apply immediately on OK.",
            ),
        )
        wrap_text(zoom_note)

        sizer.Add(zoom_label, 0, wx.ALL, 10)
        sizer.Add(self.reading_font_override_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.reading_font_size_spin, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(reading_font_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(
            self.reading_font_family_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16
        )
        sizer.Add(zoom_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        view_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_view_label",
                default="Message body view when opening a message:",
            ),
        )
        view_label.SetFont(view_label.GetFont().Bold())

        self.text_view_radio = wx.RadioButton(
            panel,
            label=lang.t("dialogs", "set_view_text", default="Text"),
            style=wx.RB_GROUP,
        )
        self.html_view_radio = wx.RadioButton(
            panel, label=lang.t("dialogs", "set_view_html", default="HTML (rendered)")
        )

        if settings.default_message_view == "html":
            self.html_view_radio.SetValue(True)
        else:
            self.text_view_radio.SetValue(True)

        view_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_view_note",
                default="HTML is the default. It shows the message as it was "
                "sent, and it is read by NVDA and JAWS: when an HTML "
                "page opens, ZBox silences the browser's own "
                "announcement, puts the reading cursor on the first "
                "line of the message, and keeps ZBox's own shortcuts "
                "working inside the page, including F6 to move focus "
                "back out of it. Text shows the same message as "
                "plain, unformatted text in an ordinary edit control "
                "-- no images, no layout, and nothing that depends on "
                "a browser engine being installed. Press Ctrl+B "
                "inside an open message to switch either way.",
            ),
        )
        view_note

        sizer.Add(view_label, 0, wx.ALL, 10)
        sizer.Add(self.text_view_radio, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.html_view_radio, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        wrap_text(view_note)
        sizer.Add(view_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        engine_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_engine_label", default="HTML rendering engine:"
            ),
        )
        engine_label.SetFont(engine_label.GetFont().Bold())

        self.engine_edge_radio = wx.RadioButton(
            panel,
            label=lang.t(
                "dialogs", "set_engine_edge",
                default="Edge / WebView2 (modern rendering)",
            ),
            style=wx.RB_GROUP,
        )
        self.engine_ie_radio = wx.RadioButton(
            panel,
            label=lang.t(
                "dialogs", "set_engine_ie",
                default="Legacy MSHTML (best screen-reader support)",
            ),
        )
        if getattr(settings, "webview_backend", "edge") == "ie":
            self.engine_ie_radio.SetValue(True)
        else:
            self.engine_edge_radio.SetValue(True)

        engine_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_engine_note",
                default="Edge renders modern CSS but runs the page in a "
                "separate process, and it will only hand its content "
                "to a screen reader through an interface wxPython "
                "cannot reach -- so the message can open with nothing "
                "readable in it. Legacy MSHTML is the engine Outlook "
                "Express and Windows Mail used: it runs in-process "
                "and publishes a normal document tree, so NVDA and "
                "JAWS read it in browse mode like any web page. "
                "Takes effect the next time a message is opened.",
            ),
        )
        engine_note

        sizer.Add(engine_label, 0, wx.ALL, 10)
        sizer.Add(self.engine_edge_radio, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.engine_ie_radio, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        wrap_text(engine_note)
        sizer.Add(engine_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        runtime_label = wx.StaticText(
            panel,
            label=lang.t("dialogs", "set_runtime_label", default="WebView2 runtime:"),
        )
        runtime_label.SetFont(runtime_label.GetFont().Bold())

        self.runtime_auto_radio = wx.RadioButton(
            panel,
            label=lang.t(
                "dialogs", "set_runtime_auto",
                default="Automatic \u2014 use the bundled runtime if present "
                        "(recommended)",
            ),
            style=wx.RB_GROUP,
        )
        self.runtime_fixed_radio = wx.RadioButton(
            panel,
            label=lang.t(
                "dialogs", "set_runtime_fixed", default="Bundled fixed version only"
            ),
        )
        self.runtime_evergreen_radio = wx.RadioButton(
            panel,
            label=lang.t(
                "dialogs", "set_runtime_evergreen",
                default="System-wide runtime (updated by Windows)",
            ),
        )

        runtime_mode = getattr(settings, "webview2_runtime", "auto")
        if runtime_mode == "fixed":
            self.runtime_fixed_radio.SetValue(True)
        elif runtime_mode == "evergreen":
            self.runtime_evergreen_radio.SetValue(True)
        else:
            self.runtime_auto_radio.SetValue(True)

        runtime_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_runtime_note",
                default="The system-wide runtime is serviced by Windows "
                "Update, so the engine reading your mail can change "
                "without notice \u2014 which is how a working HTML "
                "message view can break on a machine where nothing "
                "about ZBox changed. A copy bundled in the deps "
                "folder pins the exact version ZBox was tested "
                "against. Run tools\\get_webview2.bat to install one. "
                "If no bundled copy is present ZBox uses the "
                "system-wide runtime either way, so it always "
                "starts. Takes effect the next time ZBox starts.",
            ),
        )
        runtime_note

        self.runtime_status_label = wx.StaticText(
            panel, label=self._runtime_status_text(parent)
        )

        sizer.Add(runtime_label, 0, wx.ALL, 10)
        sizer.Add(self.runtime_auto_radio, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.runtime_fixed_radio, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.runtime_evergreen_radio, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(
            self.runtime_status_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16
        )
        wrap_text(runtime_note)
        sizer.Add(runtime_note, 0, wx.EXPAND | wx.ALL, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("accessibility")

        # The speech switch, the top line of this tab. Shows whether ZBox
        # speaks now; stored only when changed here, so automatic stays
        # automatic (spoken_feedback).
        self.announce_actions_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_announce_actions", "Speak &announcements for actions, new mail and notices"
            ),
        )
        self._speech_shown = spoken_feedback.enabled(settings)
        self.announce_actions_checkbox.SetValue(self._speech_shown)
        sizer.Add(self.announce_actions_checkbox, 0, wx.ALL, 10)

        # --- Screen reader announcements control -------------------
        # A real wx.StaticBox, with these controls parented to it
        # rather than merely sitting next to it: that is what makes
        # NVDA/JAWS announce "Screen reader announcements control
        # grouping" on the way in, so the three settings that exist
        # purely to shape what the reader says are found as one thing
        # instead of three unrelated sliders scattered down a long
        # dialog.
        sr_box = wx.StaticBox(
            panel,
            label=lang.t(
                "dialogs", "set_sr_box",
                default="Screen reader announcements control",
            ),
        )
        sr_sizer = wx.StaticBoxSizer(sr_box, wx.VERTICAL)

        sr_intro = wx.StaticText(
            sr_box,
            label=lang.t(
                "dialogs", "set_sr_intro",
                default="How much ZBox lets your screen reader say while a "
                "message opens, and how long its own spoken "
                "announcements stay up.",
            ),
        )
        wrap_text(sr_intro)
        sr_sizer.Add(sr_intro, 0, wx.EXPAND | wx.ALL, 10)

        silence_label = wx.StaticText(
            sr_box,
            label=lang.t(
                "dialogs", "set_silence_label",
                default="HTML screen-reader silence hold:",
            ),
        )
        silence_label.SetFont(silence_label.GetFont().Bold())

        self.silence_choice = _value_choice(
            sr_box, SILENCE_VALUES,
            max(0, min(2000, int(settings.html_silence_hold_ms))),
            _silence_text, silence_label,
        )

        silence_note = wx.StaticText(
            sr_box,
            label=lang.t(
                "dialogs", "set_silence_note",
                default="When an HTML message opens, ZBox can hold a synthetic "
                "Ctrl press for up to 2 seconds to silence the screen "
                "reader's browser announcement. The message's subject "
                "is already used as the page title, which stops the "
                "base64 URL from being read aloud, but the reader still "
                "announces the control and the document type over the "
                "start of the message, so this is on by default. Lower "
                "it to 0 to turn it off, or raise it if chatter still "
                "slips through.",
            ),
        )

        sr_sizer.Add(silence_label, 0, wx.ALL, 10)
        sr_sizer.Add(self.silence_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        wrap_text(silence_note)
        sr_sizer.Add(silence_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sr_sizer.Add(wx.StaticLine(sr_box), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        focus_delay_label = wx.StaticText(
            sr_box,
            label=lang.t(
                "dialogs", "set_focus_delay_label",
                default="WebView accessibility focus delay:",
            ),
        )
        focus_delay_label.SetFont(focus_delay_label.GetFont().Bold())

        self.focus_delay_choice = _value_choice(
            sr_box, FOCUS_DELAY_LEVELS,
            max(1, min(10, int(settings.webview_focus_delay_level))),
            _focus_text, focus_delay_label,
        )

        focus_delay_note = wx.StaticText(
            sr_box,
            label=lang.t(
                "dialogs", "set_focus_delay_note",
                default="When an HTML message finishes loading, ZBox waits this "
                "long before actually focusing it. WebView2 reporting "
                "a page loaded and Windows finishing that page's "
                "accessibility tree are not the same moment; focusing "
                "too early is what makes NVDA/JAWS announce a generic "
                "\u201cWeb content page\u201d instead of the message, or "
                "report the message unavailable for a stretch after "
                "opening. Level 1 is 100 ms, level 10 is a full second. "
                "Raise it if you still see this; lower it once opening "
                "feels solid, for a snappier open.",
            ),
        )

        sr_sizer.Add(focus_delay_label, 0, wx.ALL, 10)
        sr_sizer.Add(self.focus_delay_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        wrap_text(focus_delay_note)
        sr_sizer.Add(focus_delay_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sr_sizer.Add(wx.StaticLine(sr_box), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        announce_label = wx.StaticText(
            sr_box,
            label=lang.t(
                "dialogs", "set_announce_label", default="Announcement hold:"
            ),
        )
        announce_label.SetFont(announce_label.GetFont().Bold())

        self.announce_choice = _value_choice(
            sr_box, ANNOUNCE_VALUES,
            max(0, min(6000, int(getattr(settings, "announcement_hold_ms", 1800)))),
            _announce_text, announce_label,
        )

        announce_note = wx.StaticText(
            sr_box,
            label=lang.t(
                "dialogs", "set_announce_note",
                default="Ctrl+U and Ctrl+Shift+U report the sender in a small "
                "dialog that dismisses itself, because the status bar "
                "is only spoken if your screen reader has status-bar "
                "reporting switched on. This is how long that dialog "
                "stays up. Set it to match your speech rate: too short "
                "and it closes mid-sentence, which defeats the point. "
                "Any key dismisses it early; the arrow keys keep it "
                "open so you can re-read. 0 means it waits for a key "
                "and never closes on its own.",
            ),
        )
        wrap_text(announce_note)

        sr_sizer.Add(announce_label, 0, wx.ALL, 10)
        sr_sizer.Add(self.announce_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sr_sizer.Add(announce_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(sr_sizer, 0, wx.EXPAND | wx.ALL, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("general")

        attachment_dir_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_attachment_dir_label",
                default="Attachments saving folder:",
            ),
        )
        attachment_dir_label.SetFont(attachment_dir_label.GetFont().Bold())

        attachment_dir_row = wx.BoxSizer(wx.HORIZONTAL)
        self.attachment_dir_field = wx.TextCtrl(
            panel, value=getattr(settings, "attachment_save_dir", "") or ""
        )
        self.attachment_dir_field.SetName(
            lang.t(
                "dialogs", "set_attachment_dir_name",
                default="Attachments saving folder",
            )
        )
        self.attachment_dir_browse_button = wx.Button(
            panel, label=lang.control_label("set_browse", "&Browse\u2026")
        )
        self.attachment_dir_browse_button.Bind(
            wx.EVT_BUTTON, self._on_browse_attachment_dir
        )
        attachment_dir_row.Add(
            self.attachment_dir_field, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8
        )
        attachment_dir_row.Add(
            self.attachment_dir_browse_button, 0, wx.ALIGN_CENTER_VERTICAL
        )

        default_attachment_dir = os.path.join(
            os.environ.get("USERPROFILE") or os.path.expanduser("~"),
            "Downloads", "attachments",
        )
        attachment_dir_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_attachment_dir_note",
                default="Where \u201cSave All\u201d writes attachments when a "
                "message has more than one. Created automatically if it "
                "doesn't exist. A message with a single attachment "
                "still asks where to save it, as before. Leave blank "
                "for the default:\n" + default_attachment_dir,
                path=default_attachment_dir,
            ),
        )
        wrap_text(attachment_dir_note)

        sizer.Add(attachment_dir_label, 0, wx.ALL, 10)
        sizer.Add(
            attachment_dir_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16
        )
        sizer.Add(
            attachment_dir_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        autosave_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_autosave_label", default="Draft autosave interval:"
            ),
        )
        autosave_label.SetFont(autosave_label.GetFont().Bold())

        autosave_now = int(getattr(settings, "draft_autosave_seconds", 60))
        self.autosave_choice = _value_choice(
            panel, AUTOSAVE_VALUES,
            0 if autosave_now <= 0 else max(15, min(600, autosave_now)),
            _autosave_text, autosave_label,
        )

        autosave_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_autosave_note",
                default="How often an open compose tab saves itself to Drafts "
                "while you're writing. Save Draft (Ctrl+S) still saves "
                "on demand either way, and closing a tab with unsaved "
                "writing always offers to save it. 0 turns autosave "
                "off. Below 15 seconds is not offered -- that would be "
                "a server round trip competing with your typing.",
            ),
        )
        wrap_text(autosave_note)

        sizer.Add(autosave_label, 0, wx.ALL, 10)
        sizer.Add(self.autosave_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(autosave_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("maintenance")

        self.clear_inbox_cache_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_clear_inbox_cache", "Clear message cache on moving away from Inbox"
            ),
        )
        self.clear_inbox_cache_checkbox.SetValue(
            getattr(settings, "clear_cache_leaving_inbox", False)
        )
        clear_inbox_cache_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_clear_inbox_cache_note",
                default="When on, a message's offline copy and saved body are "
                "deleted as soon as it leaves an Inbox, also when it was deleted "
                "or moved somewhere else, such as on your phone, so it never "
                "shows up where it no longer is. Only the Inbox keeps these "
                "files; every other folder is always read from the server.",
            ),
        )
        wrap_text(clear_inbox_cache_note)
        sizer.Add(self.clear_inbox_cache_checkbox, 0, wx.ALL, 10)
        sizer.Add(
            clear_inbox_cache_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        auto_purge_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_auto_purge_label",
                default="Auto-delete old Trash and Junk:",
            ),
        )
        auto_purge_label.SetFont(auto_purge_label.GetFont().Bold())

        self.auto_purge_choice = _value_choice(
            panel, PURGE_VALUES,
            max(0, min(365, int(getattr(settings, "auto_purge_days", 30)))),
            _purge_text, auto_purge_label,
        )

        auto_purge_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_auto_purge_note",
                default="Permanently removes messages from Trash and Junk once "
                "they have sat there this many days -- the same "
                "permanent removal Empty Trash and Empty Junk perform "
                "on demand, run automatically once a day while ZBox is "
                "open. On by default at 30 days. 0 turns it off. This "
                "cannot be undone once a message is removed.",
            ),
        )
        wrap_text(auto_purge_note)

        sizer.Add(auto_purge_label, 0, wx.ALL, 10)
        sizer.Add(self.auto_purge_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(auto_purge_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self._build_cache_cleanup_group(panel, sizer)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("accessibility")

        self.bare_key_shortcuts_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_bare_keys",
                "Enable single-key &S (flag), A (archive), J "
                "(mark junk)/Shift+J (mark not junk), W (watch "
                "thread), K (ignore thread), M (move to)/Shift+M "
                "(move by folder name) and C (copy to) shortcuts "
                "in the message list",
            ),
        )
        self.bare_key_shortcuts_checkbox.SetValue(
            getattr(settings, "bare_key_shortcuts_enabled", True)
        )

        bare_key_shortcuts_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_bare_keys_note",
                default="When on, pressing S, A, J, W or K alone in the message "
                "list toggles the flag, archives, marks the selected "
                "message(s) as junk (Shift+J marks them not junk), or "
                "watches/ignores the thread the selection is part of, "
                "immediately -- Archive and Mark as Junk have no "
                "confirmation and no undo. Turning this off returns S, "
                "A, J, W and K to normal first-letter list navigation. "
                "Ctrl+S and Ctrl+Shift+A keep working either way. Takes "
                "effect immediately.",
            ),
        )
        wrap_text(bare_key_shortcuts_note)
        bare_key_move_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_bare_keys_note_move",
                default="M and C open the Move To and Copy To menus of "
                "folders for the selected message(s), and Shift+M opens a "
                "box where you type part of a folder name to move them "
                "there. Turning this off returns M and C to first-letter "
                "navigation too.",
            ),
        )
        wrap_text(bare_key_move_note)

        sizer.Add(self.bare_key_shortcuts_checkbox, 0, wx.ALL, 10)
        sizer.Add(
            bare_key_shortcuts_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )
        sizer.Add(
            bare_key_move_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        # Off by default: a screen reader then speaks each message row's
        # cells without the column names before them.
        self.list_headers_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_list_headers", "Show column headers in the message list"
            ),
        )
        self.list_headers_checkbox.SetValue(bool(getattr(settings, "show_list_headers", False)))
        sizer.Add(self.list_headers_checkbox, 0, wx.ALL, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("sounds")

        sound_label = wx.StaticText(
            panel,
            label=lang.control_label("set_sound_label", "Sound && Notifications:"),
        )
        sound_label.SetFont(sound_label.GetFont().Bold())
        sizer.Add(sound_label, 0, wx.ALL, 10)

        self.sounds_enabled_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_sounds_enabled", "&Play notification sounds"
            ),
        )
        self.sounds_enabled_checkbox.SetValue(
            getattr(settings, "sounds_enabled", True)
        )
        self.sounds_enabled_checkbox.Bind(
            wx.EVT_CHECKBOX, self._on_sounds_enabled_checkbox
        )

        sounds_enabled_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_sounds_note",
                default="Short tones for progress, new messages, sent, replied "
                "or forwarded actions, deletions, and search results. "
                "Off disables every sound in the app; screen reader "
                "and status bar announcements are unaffected either "
                "way.",
            ),
        )
        wrap_text(sounds_enabled_note)

        sizer.Add(self.sounds_enabled_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(
            sounds_enabled_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        self.desktop_notifications_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_desktop_notifications",
                "Show &desktop notifications for new mail",
            ),
        )
        self.desktop_notifications_checkbox.SetValue(
            getattr(settings, "desktop_notifications_enabled", True)
        )

        desktop_notifications_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_desktop_notifications_note",
                default="A Windows notification for each new-mail batch, "
                "separate from the sound above. Off disables it for "
                "every account; an individual account can also be "
                "muted on its own in Account and Identities Settings.",
            ),
        )
        wrap_text(desktop_notifications_note)

        sizer.Add(self.desktop_notifications_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(
            desktop_notifications_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        sounds_dir = getattr(getattr(parent, "paths", None), "sounds_dir", None)
        self._sound_themes = (
            sound_manager.available_themes(sounds_dir)
            or [sound_manager.DEFAULT_THEME]
        )
        only_default_theme = len(self._sound_themes) <= 1

        self._theme_label_text = lang.t(
            "dialogs", "set_theme_label_only_default",
            default="Change sounds theme: (default theme only)",
        ) if only_default_theme else lang.t(
            "dialogs", "set_theme_label", default="Change sounds theme:"
        )
        self.theme_label = wx.StaticText(panel, label=self._theme_label_text)

        # Never disabled, for the same reason the Interface language
        # choice above is not. A disabled control is skipped by the Tab
        # order completely and its label is a static text that never
        # takes focus, so to a keyboard user the whole setting is
        # simply missing. Why it may have nothing to offer is said in
        # its own name instead, which is read when it is reached.
        self.theme_choice = wx.Choice(panel, choices=list(self._sound_themes))
        current_theme = (
            getattr(settings, "sound_theme", sound_manager.DEFAULT_THEME)
            or sound_manager.DEFAULT_THEME
        )
        if current_theme not in self._sound_themes:
            current_theme = sound_manager.DEFAULT_THEME
        self.theme_choice.SetSelection(self._sound_themes.index(current_theme))

        theme_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_theme_note_only_default",
                default="Custom themes are folders placed inside ZBox's sounds "
                "folder, each supplying its own progress-tick.wav, "
                "incoming-message.wav, action-done.wav, delete.wav, "
                "searching.wav, searching_done.wav and none_found.wav. "
                "A theme file that's missing falls back to the default "
                "sound. Only \u201cdefault\u201d is available until you "
                "add another theme folder.",
            )
            if only_default_theme else
            lang.t(
                "dialogs", "set_theme_note",
                default="Custom themes are folders placed inside ZBox's sounds "
                "folder, each supplying its own progress-tick.wav, "
                "incoming-message.wav, action-done.wav, delete.wav, "
                "searching.wav, searching_done.wav and none_found.wav. "
                "A theme file that's missing falls back to the default "
                "sound.",
            ),
        )
        wrap_text(theme_note)

        sizer.Add(self.theme_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(self.theme_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(theme_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self._update_theme_choice_name()

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("security")

        # --- App and Data Security ----------------------------------
        # A real wx.StaticBox with the controls parented to it, same
        # reasoning as the screen reader group above: NVDA/JAWS
        # announce "App and Data Security grouping" on the way in, so
        # the app lock, the methods that open it and the master
        # password are met as one subject rather than three unrelated
        # controls in a long dialog. Master Password used to live in
        # the Tools menu on its own; it is here now, next to the
        # things it belongs with. Renamed from "Privacy and Security"
        # so it stops overlapping in name with Tools > Privacy and
        # Content Blocking, a different dialog about a different kind
        # of privacy (message content, not app/account access) -- this
        # box is about locking the app and protecting stored data.
        security_box = wx.StaticBox(
            panel,
            label=lang.t(
                "dialogs", "set_security_box", default="App and Data Security"
            ),
        )
        security_sizer = wx.StaticBoxSizer(security_box, wx.VERTICAL)

        self.lock_on_quit_checkbox = wx.CheckBox(
            security_box,
            label=lang.control_label(
                "set_lock_on_quit", "&Lock ZBox when it is fully quit"
            ),
        )
        self.lock_on_quit_checkbox.SetValue(
            getattr(settings, "lock_on_full_quit", False)
        )
        security_sizer.Add(self.lock_on_quit_checkbox, 0, wx.ALL, 10)

        lock_note = wx.StaticText(
            security_box,
            label=lang.t(
                "dialogs", "set_lock_note",
                default="Starting ZBox then asks for one of your unlock "
                "methods before any account is opened. Any one of "
                "them is enough: a passkey, a security key, or the "
                "master password. Closing to the tray is not a quit "
                "and does not re-lock; File > Exit and the tray "
                "icon's Quit are. If nothing is registered and no "
                "master password is set, the lock is skipped rather "
                "than shutting you out of your own mail.",
            ),
        )
        wrap_text(lock_note)
        security_sizer.Add(
            lock_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        security_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.unlock_methods_button = wx.Button(
            security_box,
            label=lang.control_label(
                "set_unlock_methods_button", "&Unlock methods\u2026"
            ),
        )
        self.unlock_methods_button.Bind(
            wx.EVT_BUTTON, self._on_unlock_methods
        )
        self.master_password_button = wx.Button(
            security_box,
            label=lang.control_label(
                "set_master_password_button", "Master pass&word\u2026"
            ),
        )
        self.master_password_button.Bind(
            wx.EVT_BUTTON, self._on_master_password
        )
        security_buttons.Add(self.unlock_methods_button, 0, wx.RIGHT, 8)
        security_buttons.Add(self.master_password_button, 0)
        security_sizer.Add(
            security_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        self.security_status_label = wx.StaticText(security_box, label="")
        security_sizer.Add(
            self.security_status_label, 0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10,
        )

        security_note = wx.StaticText(
            security_box,
            label=lang.t(
                "dialogs", "set_security_note",
                default="The master password is the only one of the three "
                "that encrypts anything: it is what lets this folder "
                "open its saved account passwords on another "
                "computer. A passkey or security key unlocks the app "
                "on this one. Neither replaces the master password "
                "on a computer ZBox has not been used on before.",
            ),
        )
        wrap_text(security_note)
        security_sizer.Add(
            security_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        # Private mode (private_mode.py). Turning it on asks for the
        # master password and warns first; see _on_private_mode.
        self.private_mode_checkbox = wx.CheckBox(
            security_box,
            # The key name stays in English in every language.
            label=lang.control_label(
                "set_private_mode", "&Private mode: keep no mail on this computer or drive"
            ) + " (Windows+Ctrl+Shift+P)",
        )
        self.private_mode_checkbox.SetValue(bool(getattr(settings, "private_mode", False)))
        self.private_mode_checkbox.Bind(wx.EVT_CHECKBOX, self._on_private_mode)
        self._private_confirmed = False
        private_note = wx.StaticText(
            security_box,
            label=lang.t(
                "dialogs", "set_private_mode_note",
                default="Messages are read from the server and kept in memory "
                "only. No offline copies, saved messages or debug logs are kept, "
                "and opened attachments and the browser data of HTML mail are "
                "deleted when ZBox closes. Closing also ends every program ZBox "
                "started, including those showing an attachment, empties the "
                "clipboard, and stops trusting this computer unless private mode "
                "was turned on from it. Windows still keeps some records no "
                "program can remove, such as its list of USB drives, the page "
                "file, and antivirus and network logs.",
            ),
        )
        wrap_text(private_note)
        security_sizer.Add(self.private_mode_checkbox, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        security_sizer.Add(
            private_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        self._refresh_security_status()
        sizer.Add(security_sizer, 0, wx.EXPAND | wx.ALL, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        panel, sizer = self._page("general")

        tray_label = wx.StaticText(
            panel,
            label=lang.control_label("set_tray_label", "System tray && window:"),
        )
        tray_label.SetFont(tray_label.GetFont().Bold())
        sizer.Add(tray_label, 0, wx.ALL, 10)

        self.minimize_to_tray_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_minimize_to_tray", "&Minimize to the system tray"
            ),
        )
        self.minimize_to_tray_checkbox.SetValue(
            getattr(settings, "minimize_to_tray", False)
        )
        minimize_to_tray_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_minimize_note",
                default="Minimizing ZBox hides its taskbar button and leaves it "
                "running behind the tray icon instead. Left-click the "
                "tray icon (or Enter/Space when it has keyboard focus) "
                "to bring the window back.",
            ),
        )
        wrap_text(minimize_to_tray_note)
        sizer.Add(self.minimize_to_tray_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(
            minimize_to_tray_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        self.close_to_tray_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_close_to_tray", "&Close to the system tray"
            ),
        )
        self.close_to_tray_checkbox.SetValue(
            getattr(settings, "close_to_tray", False)
        )
        close_to_tray_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_close_note",
                default="The window's Close button, Alt+F4, and Ctrl+W pressed "
                "on the Mail tab hide ZBox to the tray instead of "
                "quitting. File > Exit (Ctrl+Shift+Q) and the tray "
                "icon's own Quit item always actually quit ZBox either "
                "way, so there is always one keyboard path to exit "
                "without touching the tray.",
            ),
        )
        wrap_text(close_to_tray_note)
        sizer.Add(self.close_to_tray_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(
            close_to_tray_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        self.stay_maximized_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_stay_maximized", "Always keep ZBox &maximized"
            ),
        )
        self.stay_maximized_checkbox.SetValue(
            getattr(settings, "stay_maximized", False)
        )
        stay_maximized_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_stay_maximized_note",
                default="ZBox launches maximized and is put back to maximized "
                "if you un-maximize it. The menu bar, title bar and "
                "window border are unaffected -- this is not a "
                "borderless full-screen mode, so every Alt-key menu "
                "shortcut keeps working.",
            ),
        )
        wrap_text(stay_maximized_note)
        sizer.Add(self.stay_maximized_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(
            stay_maximized_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        self.run_at_startup_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_run_at_startup", "&Run ZBox when Windows starts"
            ),
        )
        self.run_at_startup_checkbox.SetValue(
            getattr(settings, "run_at_windows_startup", False)
        )
        run_at_startup_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_run_at_startup_note",
                default="Starts ZBox automatically when you sign in to "
                "Windows, opened straight to the tray rather than "
                "showing its window. Takes effect immediately, not "
                "only on the next launch.",
            ),
        )
        wrap_text(run_at_startup_note)
        sizer.Add(self.run_at_startup_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(
            run_at_startup_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        self.desktop_shortcut_button = wx.Button(
            panel,
            label=lang.control_label(
                "set_desktop_shortcut_button", "Create des&ktop shortcut"
            ),
        )
        self.desktop_shortcut_button.Bind(
            wx.EVT_BUTTON, self._on_create_desktop_shortcut
        )
        desktop_shortcut_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_desktop_shortcut_note",
                default="Puts a ZBox shortcut on your Desktop, replacing one "
                "that is already there. A shortcut stores the exact "
                "location of this ZBox folder, so if you move or copy "
                "ZBox somewhere else, press this again on the new copy "
                "to point the Desktop shortcut at it. Takes effect "
                "immediately and does not wait for OK.",
            ),
        )
        wrap_text(desktop_shortcut_note)
        sizer.Add(self.desktop_shortcut_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(
            desktop_shortcut_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self._build_updates_group(panel, sizer, settings)

        # About ZBox, the last tab (about_dialog.AboutPanel).
        about = AboutPanel(
            self.settings_tabs, parent,
            getattr(getattr(parent, "paths", None), "license_file", None),
        )
        self.settings_tabs.AddPage(
            about, lang.t("dialogs", "title_about", default="About ZBox")
        )
        self._pages["about"] = about

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.settings_tabs, 1, wx.EXPAND | wx.ALL, 6)
        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        outer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        # Each tab's scrolling area gets a real size first, with room
        # left for the tab row as well as the title bar and the buttons.
        for key in self._scrollers:
            _fit_scrolled_children(self._pages[key], reserve=240)
        fit_dialog(self, outer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_tab_keys)

        # Opens on the category it was asked for, General otherwise,
        # with focus on the category list so the arrow keys work at once.
        # Help > Updates > Update Settings opens General at Check for
        # updates automatically, as before.
        keys = list(self._pages)
        self.settings_tabs.SetSelection(keys.index(page) if page in keys else 0)
        if getattr(parent, "_settings_focus_updates", False):
            wx.CallAfter(self.update_auto_checkbox.SetFocus)
        elif getattr(parent, "_settings_focus_private", False):
            # Windows+Ctrl+Shift+P: App and Data Security, at Private mode.
            wx.CallAfter(self.private_mode_checkbox.SetFocus)
        else:
            wx.CallAfter(self.settings_tabs.GetListView().SetFocus)

    def _page(self, key):
        """The scrolling area of one settings tab and its sizer."""
        scroller = self._scrollers[key]
        return scroller, scroller.GetSizer()

    def _on_tab_keys(self, event):
        """Ctrl+Tab and Ctrl+Shift+Tab switch to the next and previous
        category from any control, wrapping around, and put focus on the
        category list so the new category's name is spoken. Handled here
        and not passed on, so one press moves exactly one category."""
        if event.GetKeyCode() == wx.WXK_TAB and event.ControlDown() and not event.AltDown():
            count = self.settings_tabs.GetPageCount()
            if count:
                step = -1 if event.ShiftDown() else 1
                self.settings_tabs.SetSelection(
                    (self.settings_tabs.GetSelection() + step) % count
                )
                self.settings_tabs.GetListView().SetFocus()
            return
        event.Skip()

    def _build_cache_cleanup_group(self, panel, sizer):
        """Maintenance and Cleanup: the cache clean-up controls, which
        had a dialog of their own (Tools > Cache Clean Up Configuration
        now opens this tab). They remove ZBox's local copies only. The
        age is saved with OK, like every other setting here."""
        self._cache_age_days = getattr(self.settings, "cache_max_message_age_days", 30)
        intro = wx.StaticText(panel, label=lang.t(
            "dialogs", "cache_note_intro",
            default=(
                "These remove ZBox's local copies only. Nothing is deleted "
                "from any mail server, and a removed message is downloaded "
                "again the next time you open it."
            ),
        ))
        wrap_text(intro)
        sizer.Add(intro, 0, wx.EXPAND | wx.ALL, 10)

        self.cache_clear_button = wx.Button(
            panel,
            label=lang.t(
                "dialogs", "cache_clear_now",
                default="Clear Offline Message Cache Now... (Ctrl+Shift+Delete)",
            ),
        )
        self.cache_clear_button.Bind(wx.EVT_BUTTON, self._on_cache_clear_now)
        sizer.Add(self.cache_clear_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        age_note = wx.StaticText(panel, label=lang.t(
            "dialogs", "cache_note_age",
            default=(
                "Automatic clean up runs once a day, when ZBox has had no "
                "keyboard activity for ten minutes and no message is open. "
                "It goes by each message's own date, including messages "
                "that are still in your account."
            ),
        ))
        wrap_text(age_note)
        sizer.Add(age_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.cache_age_button = wx.Button(panel, label=age_button_label(self._cache_age_days))
        self.cache_age_button.Bind(wx.EVT_BUTTON, self._on_cache_choose_age)
        sizer.Add(self.cache_age_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

    def _on_cache_clear_now(self, _event):
        """Saves like OK, closes, then runs the frame's own Clear Offline
        Message Cache, so its confirmation and spoken result never sit
        on top of this dialog. The same way Check now works."""
        handler = getattr(self.GetParent(), "_on_clear_message_cache", None)
        self.EndModal(wx.ID_OK)
        if handler is not None:
            wx.CallAfter(handler, None)

    def _on_cache_choose_age(self, _event):
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
        if self._cache_age_days in CACHE_AGE_CHOICES:
            chooser.SetSelection(CACHE_AGE_CHOICES.index(self._cache_age_days))
        if chooser.ShowModal() == wx.ID_OK:
            self._cache_age_days = CACHE_AGE_CHOICES[chooser.GetSelection()]
            self.cache_age_button.SetLabel(age_button_label(self._cache_age_days))
        chooser.Destroy()
        self.cache_age_button.SetFocus()

    def _build_updates_group(self, panel, sizer, settings):
        """Settings > Updates: the automatic check, what to do when an
        update is found, and Check now. See update_actions.py."""
        updates_label = wx.StaticText(
            panel, label=lang.t("dialogs", "set_updates_label", default="Updates:"),
        )
        updates_label.SetFont(updates_label.GetFont().Bold())
        sizer.Add(updates_label, 0, wx.ALL, 10)

        self.update_auto_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_update_auto", "Check for &updates automatically",
            ),
        )
        self.update_auto_checkbox.SetValue(getattr(settings, "update_auto_check", True))
        sizer.Add(self.update_auto_checkbox, 0, wx.LEFT | wx.RIGHT, 16)

        mode_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_update_mode_label", default="When an update is found:",
            ),
        )
        self.update_ask_radio = wx.RadioButton(
            panel,
            label=lang.control_label("set_update_ask", "Ask &me"),
            style=wx.RB_GROUP,
        )
        self.update_silent_radio = wx.RadioButton(
            panel,
            label=lang.control_label(
                "set_update_silent", "Download and install &silently",
            ),
        )
        silent = getattr(settings, "update_mode", "ask") == "silent"
        self.update_silent_radio.SetValue(silent)
        self.update_ask_radio.SetValue(not silent)
        sizer.Add(mode_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        sizer.Add(self.update_ask_radio, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.update_silent_radio, 0, wx.LEFT | wx.RIGHT, 16)

        update_note = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_update_note",
                default="ZBox checks GitHub for a newer release at most once a "
                "day, only while it is idle, and sends nothing but its version "
                "number. Ask me shows the new version and its release notes, "
                "with Yes, update now and Remind me later. Download and install "
                "silently fetches the update in idle time and installs it the "
                "next time you fully exit ZBox, never while you are working. An "
                "update replaces only ZBox's own program files: your accounts, "
                "passwords, settings, mail, logs, sound themes and any language "
                "you added are never touched, and the previous version is kept "
                "and restored by itself if the new one fails to start. Help, "
                "Updates also has Check for Updates Now and the releases page "
                "for updating by hand.",
            ),
        )
        wrap_text(update_note)
        sizer.Add(update_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 10)

        self.update_check_now_button = wx.Button(
            panel,
            label=lang.control_label("set_update_check_now", "Check &now"),
        )
        self.update_check_now_button.Bind(wx.EVT_BUTTON, self._on_update_check_now)
        sizer.Add(self.update_check_now_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

    def _on_update_check_now(self, _event):
        """Saves like OK, closes, then runs the frame's own Check for
        Updates Now, so its prompt never sits on top of this dialog."""
        handler = getattr(self.GetParent(), "_on_check_updates", None)
        self.EndModal(wx.ID_OK)
        if handler is not None:
            wx.CallAfter(handler, None)

    def _on_private_mode(self, event):
        """Private mode on: a master password must exist and be entered,
        then the warning, with No as the default. Any refusal leaves the
        box off, with focus back on it. Turning it off needs neither."""
        box = self.private_mode_checkbox
        if not box.GetValue():
            self._private_confirmed = False
            return
        if getattr(self.settings, "private_mode", False):
            return  # it was on when Settings opened
        import dpapi_secret_store

        title = lang.t("dialogs", "title_private_mode", default="Private Mode")
        config_dir = self._config_dir()
        try:
            has_master = bool(config_dir) and dpapi_secret_store.has_master_password(config_dir)
        except Exception:  # noqa: BLE001
            has_master = False
        if not has_master:
            wx.MessageBox(
                lang.t(
                    "dialogs", "private_mode_need_master",
                    default="Private mode needs a master password, because only "
                    "the master password opens your accounts on a computer that "
                    "is not yours. Set one with the Master password button, then "
                    "turn on private mode.",
                ),
                title, wx.OK | wx.ICON_WARNING, self,
            )
            box.SetValue(False)
            box.SetFocus()
            return
        dialog = wx.PasswordEntryDialog(
            self,
            lang.t(
                "dialogs", "private_mode_enter_master",
                default="Enter the master password to turn on private mode.",
            ),
            title,
        )
        try:
            entered = dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else ""
        finally:
            dialog.Destroy()
        correct = False
        if entered:
            try:
                correct = bool(dpapi_secret_store.verify_master_password(config_dir, entered))
            except Exception:  # noqa: BLE001 - a wrong password raises here
                correct = False
            if not correct:
                wx.MessageBox(
                    lang.t(
                        "errors", "unlock_password_wrong",
                        default="That master password is not correct.",
                    ),
                    title, wx.OK | wx.ICON_ERROR, self,
                )
        if not correct:
            box.SetValue(False)
            box.SetFocus()
            return
        answer = wx.MessageBox(
            lang.t(
                "dialogs", "private_mode_confirm",
                default="Private mode keeps no copies of your mail on this "
                "computer or on the drive ZBox runs from. Turning it on deletes "
                "ZBox's offline copies, saved messages, opened attachments, the "
                "browser data of HTML mail and the debug logs. Nothing on your "
                "mail servers is deleted, and your accounts, contacts and "
                "settings are kept. Turn on private mode?",
            ),
            title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self,
        )
        if answer == wx.YES:
            self._private_confirmed = True
        else:
            box.SetValue(False)
        box.SetFocus()

    def _config_dir(self):
        paths = getattr(self.GetParent(), "paths", None)
        return getattr(paths, "config", None)

    def _refresh_security_status(self):
        """One line saying what is actually in force, because "Lock
        ZBox when it is fully quit" on its own does not say what
        would open it again."""
        config_dir = self._config_dir()
        if not config_dir:
            self.security_status_label.SetLabel("")
            return

        parts = []
        try:
            import unlock_methods

            count = len(unlock_methods.usable_methods(config_dir))
            stale = len(unlock_methods.foreign_passkeys(config_dir))
            if count:
                parts.append(
                    "1 unlock method usable here" if count == 1
                    else lang.t('dialogs', 'set_unlock_many', default="%d unlock methods usable here") % count
                )
            else:
                parts.append(lang.t('dialogs', 'set_no_unlock', default="No passkey or security key usable here"))
            if stale:
                parts.append(
                    "%d passkey%s from another computer, re-assign it in "
                    "Unlock methods" % (stale, "" if stale == 1 else "s")
                )
        except Exception:  # noqa: BLE001
            parts.append(lang.t('dialogs', 'set_unlock_unreadable', default="Unlock methods could not be read"))

        try:
            import dpapi_secret_store

            parts.append(
                "master password set"
                if dpapi_secret_store.has_master_password(config_dir)
                else "no master password set"
            )
        except Exception:  # noqa: BLE001
            parts.append("master password state unknown")

        self.security_status_label.SetLabel("; ".join(parts) + ".")
        wrap_text(self.security_status_label)
        self.Layout()

    def _on_unlock_methods(self, _event):
        config_dir = self._config_dir()
        if not config_dir:
            wx.MessageBox(
                lang.t(
                    "errors", "unlock_methods_no_config_folder",
                    default="ZBox could not find its configuration folder, so "
                    "unlock methods cannot be managed here.",
                ),
                lang.t("dialogs", "title_unlock_methods", default="Unlock Methods"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        from unlock_dialog import UnlockMethodsDialog

        dialog = UnlockMethodsDialog(self, config_dir)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        self._refresh_security_status()

    def _on_master_password(self, _event):
        """Runs the frame's own Master Password flow, so there is one
        implementation of it rather than a second copy here."""
        handler = getattr(self.GetParent(), "_on_master_password", None)
        if handler is None:
            wx.MessageBox(
                lang.t(
                    "errors", "master_password_not_here",
                    default="The master password cannot be changed from here.",
                ),
                lang.t("dialogs", "title_master_password", default="Master Password"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        handler(None)
        self._refresh_security_status()

    def _on_create_desktop_shortcut(self, _event):
        """Acts immediately rather than on OK, and stores nothing in
        settings: the shortcut is a file on the Desktop that the user
        may delete or move at any moment, so a saved "shortcut
        created" flag would claim a state ZBox cannot keep true.
        Pressing it again is also the repair after the ZBox folder
        moves -- it overwrites the existing file with the current
        folder's path.
        """
        base = getattr(getattr(self.GetParent(), "paths", None), "base", None)
        if not base:
            wx.MessageBox(
                lang.t(
                    "errors", "shortcut_no_folder",
                    default="ZBox could not work out its own folder, so a shortcut "
                    "cannot be created from here.",
                ),
                lang.t("dialogs", "title_desktop_shortcut", default="Desktop Shortcut"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        from desktop_shortcut import create_desktop_shortcut

        try:
            path = create_desktop_shortcut(base)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(
                lang.t(
                    "errors", "shortcut_failed_settings",
                    default="The desktop shortcut could not be created:\n\n%s" % exc,
                    error=exc,
                ),
                lang.t("dialogs", "title_desktop_shortcut", default="Desktop Shortcut"),
                wx.OK | wx.ICON_ERROR, self,
            )
            return
        notice_toast.notify(
            self,
            lang.t(
                "dialogs", "shortcut_created_settings",
                default="Shortcut created:\n%s\n\nIt starts ZBox from:\n%s"
                % (path, base),
                path=path, base=base,
            ),
            lang.t("dialogs", "title_desktop_shortcut", default="Desktop Shortcut"),
        )

    def _runtime_status_text(self, parent):
        """Names both runtimes out loud: which bundled version is
        installed, if any, and which system-wide version would be
        used otherwise. The system-wide version is here so that a
        future "HTML messages stopped reading" report can say which
        build it stopped on -- it changes under you, silently, and
        without this there is nothing to compare against."""
        try:
            from webview2_runtime import installed_evergreen_version, version_from_path
        except Exception:
            installed_evergreen_version = lambda: ""
            version_from_path = lambda path: ""

        system_version = installed_evergreen_version()
        system_line = (
            lang.t('dialogs', 'set_rt_system_ver', default="System-wide runtime: version %s.") % system_version
            if system_version
            else lang.t('dialogs', 'set_rt_system_unknown', default="System-wide runtime: version could not be determined.")
        )

        paths = getattr(parent, "paths", None)
        root = getattr(paths, "webview2_runtime_dir", None)
        if not root:
            return (lang.t('dialogs', 'set_rt_bundled_unknown', default='Bundled runtime: could not be determined.') + ' ') + system_line

        if os.path.isfile(os.path.join(root, "msedgewebview2.exe")):
            version = version_from_path(root)
            bundled = (
                lang.t('dialogs', 'set_rt_bundled_ver', default="Bundled runtime: version %s installed.") % version
                if version
                else lang.t('dialogs', 'set_rt_bundled_installed', default="Bundled runtime: installed.")
            )
            return bundled + " " + system_line

        try:
            versions = sorted(
                name for name in os.listdir(root)
                if os.path.isfile(os.path.join(root, name, "msedgewebview2.exe"))
            )
        except OSError:
            versions = []

        if versions:
            named = [version_from_path(name) or name for name in versions]
            return (
                (lang.t('dialogs', 'set_rt_bundled_many', default='Bundled runtime: %s installed.') + ' ') % ", ".join(named)
            ) + system_line

        return (
            (lang.t('dialogs', 'set_rt_bundled_none', default='Bundled runtime: none installed, so ZBox uses the system-wide one.') + ' ') + system_line
        )

    def _on_reading_font_override_checkbox(self, _event):
        self.reading_font_size_spin.Enable(
            self.reading_font_override_checkbox.GetValue()
        )

    def _update_theme_choice_name(self):
        """Why this choice may have nothing to offer, said in its own
        name rather than by greying it out. Rebuilt whenever the sounds
        checkbox changes, so what is read is current."""
        name = self._theme_label_text.replace("&", "")
        if not self.sounds_enabled_checkbox.GetValue():
            name += ", sounds are switched off"
        self.theme_choice.SetName(name)

    def _on_sounds_enabled_checkbox(self, _event):
        self._update_theme_choice_name()

    def _on_browse_attachment_dir(self, _event):
        current = self.attachment_dir_field.GetValue().strip()
        with wx.DirDialog(
            self,
            lang.t(
                "dialogs", "set_choose_attachment_dir",
                default="Choose the attachment save folder",
            ),
            defaultPath=current if current else "",
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.attachment_dir_field.SetValue(dialog.GetPath())

    def apply_to_settings(self):
        self.settings.debug_logging = self.debug_checkbox.GetValue()
        language_selection = self.language_choice.GetSelection()
        if language_selection != wx.NOT_FOUND and language_selection < len(self._languages):
            self.settings.language = self._languages[language_selection][0]
        theme_selection = self.ui_theme_choice.GetSelection()
        if theme_selection != wx.NOT_FOUND and theme_selection < len(self._ui_themes):
            self.settings.ui_theme = self._ui_themes[theme_selection]
        if self.reading_font_override_checkbox.GetValue():
            self.settings.reading_font_size = self.reading_font_size_spin.GetValue()
        else:
            self.settings.reading_font_size = None
        self.settings.reading_font_family = self.reading_font_family_field.GetValue().strip()
        self.settings.default_message_view = (
            "html" if self.html_view_radio.GetValue() else "text"
        )
        self.settings.html_silence_hold_ms = _chosen_value(
            self.silence_choice, self.settings.html_silence_hold_ms
        )
        self.settings.webview_focus_delay_level = _chosen_value(
            self.focus_delay_choice, self.settings.webview_focus_delay_level
        )
        self.settings.announcement_hold_ms = _chosen_value(
            self.announce_choice, self.settings.announcement_hold_ms
        )
        speech = self.announce_actions_checkbox.GetValue()
        if speech != self.__dict__.get("_speech_shown", speech):
            self.settings.announce_actions = speech
        self.settings.show_message_headers = self.headers_checkbox.GetValue()
        self.settings.attachment_save_dir = self.attachment_dir_field.GetValue().strip()
        self.settings.bare_key_shortcuts_enabled = self.bare_key_shortcuts_checkbox.GetValue()
        headers_box = getattr(self, "list_headers_checkbox", None)
        if headers_box is not None:
            self.settings.show_list_headers = headers_box.GetValue()
        self.settings.clear_cache_leaving_inbox = self.clear_inbox_cache_checkbox.GetValue()
        self.settings.draft_autosave_seconds = _chosen_value(
            self.autosave_choice, self.settings.draft_autosave_seconds
        )
        self.settings.webview_backend = (
            "ie" if self.engine_ie_radio.GetValue() else "edge"
        )
        self.settings.sounds_enabled = self.sounds_enabled_checkbox.GetValue()
        self.settings.desktop_notifications_enabled = (
            self.desktop_notifications_checkbox.GetValue()
        )
        theme_selection = self.theme_choice.GetSelection()
        if theme_selection != wx.NOT_FOUND and theme_selection < len(self._sound_themes):
            self.settings.sound_theme = self._sound_themes[theme_selection]
        if self.runtime_fixed_radio.GetValue():
            self.settings.webview2_runtime = "fixed"
        elif self.runtime_evergreen_radio.GetValue():
            self.settings.webview2_runtime = "evergreen"
        else:
            self.settings.webview2_runtime = "auto"
        self.settings.minimize_to_tray = self.minimize_to_tray_checkbox.GetValue()
        self.settings.close_to_tray = self.close_to_tray_checkbox.GetValue()
        self.settings.stay_maximized = self.stay_maximized_checkbox.GetValue()
        self.settings.run_at_windows_startup = self.run_at_startup_checkbox.GetValue()
        self.settings.lock_on_full_quit = self.lock_on_quit_checkbox.GetValue()
        # Never on without the master password and the warning
        # (_on_private_mode); off needs neither.
        private = self.private_mode_checkbox.GetValue()
        if (private and not getattr(self.settings, "private_mode", False)
                and not self.__dict__.get("_private_confirmed")):
            private = False
        self.settings.private_mode = private
        self.settings.auto_purge_days = _chosen_value(
            self.auto_purge_choice, self.settings.auto_purge_days
        )
        self.settings.update_auto_check = self.update_auto_checkbox.GetValue()
        self.settings.update_mode = (
            "silent" if self.update_silent_radio.GetValue() else "ask"
        )
        self.settings.cache_max_message_age_days = self.__dict__.get(
            "_cache_age_days", self.settings.cache_max_message_age_days
        )
        return self.settings
