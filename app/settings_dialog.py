"""
Settings dialog, reached from Tools > Settings. Options:
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

from accessible import fit_dialog, wrap_text
import lang
import sound_manager
from settings_manager import UI_THEMES
from message_view_panel import DEFAULT_READING_FONT_POINT_SIZE, clamp_reading_font_size


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, settings):
        super().__init__(parent,
                         title=lang.t("dialogs", "title_settings", default="Settings"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.settings = settings

        # The controls live in a scrolled panel rather than directly
        # on the dialog. There are six setting groups here, each with
        # an explanatory paragraph, and at 150 or 200 percent Windows
        # text scaling that is taller than the screen -- which used to
        # put OK and Cancel below the bottom edge, unreachable by
        # keyboard as well as by mouse. Scrolling keeps the buttons
        # where they belong and costs a screen reader nothing:
        # everything stays in the tab order, and wx scrolls whatever
        # takes focus into view.
        panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
        panel.SetScrollRate(0, 12)

        sizer = wx.BoxSizer(wx.VERTICAL)
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

        self.silence_slider = wx.Slider(
            sr_box,
            value=min(max(settings.html_silence_hold_ms, 0), 2000),
            minValue=0,
            maxValue=2000,
            # No SL_LABELS. On Windows that style draws wx's own
            # min/max/value captions as extra children of the
            # trackbar, and NVDA/JAWS read those instead of (and
            # before) the real value -- reported live as the slider
            # announcing "1" and then "3" on focus without anything
            # having moved. The value is spoken by the reader anyway,
            # and the *_value_label StaticText below each slider
            # already shows it in real units for sighted readers.
            style=wx.SL_HORIZONTAL,
        )
        self.silence_slider.SetTickFreq(500)
        # Step sizes matter for more than convenience here. A screen
        # reader announces a trackbar as a percentage, so with the
        # default one-unit arrow step this slider moved 1 ms of 2000
        # -- 0.05 percent -- and every arrow press re-announced the
        # same "0 percent", which is indistinguishable from a slider
        # that is not moving at all (reported live). 100 ms per arrow
        # is 5 percent, which the reader actually reports as a change.
        self.silence_slider.SetLineSize(100)
        self.silence_slider.SetPageSize(500)
        # Named explicitly: a native trackbar takes no accessible name
        # from the StaticText sitting above it, so without this the
        # reader announced a bare number with no idea what it set.
        self.silence_slider.SetName(
            lang.t(
                "dialogs", "set_silence_name",
                default="HTML screen-reader silence hold in milliseconds",
            )
        )
        self.silence_value_label = wx.StaticText(
            sr_box, label=self._silence_value_text(settings.html_silence_hold_ms)
        )
        self.silence_slider.Bind(wx.EVT_SLIDER, self._on_silence_slider)

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
        sr_sizer.Add(self.silence_slider, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 16)
        sr_sizer.Add(self.silence_value_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
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

        self.focus_delay_slider = wx.Slider(
            sr_box,
            value=min(max(settings.webview_focus_delay_level, 1), 10),
            minValue=1,
            maxValue=10,
            style=wx.SL_HORIZONTAL,
        )
        self.focus_delay_slider.SetTickFreq(1)
        # Already a coarse 1-10 scale, so one arrow is a tenth of the
        # range and the reader reports the change. Set explicitly all
        # the same, so the three sliders in this group are obviously
        # governed by the same rule rather than one of them happening
        # to work.
        self.focus_delay_slider.SetLineSize(1)
        self.focus_delay_slider.SetPageSize(2)
        self.focus_delay_slider.SetName(
            lang.t(
                "dialogs", "set_focus_delay_name",
                default="WebView accessibility focus delay, level 1 to 10",
            )
        )
        self.focus_delay_value_label = wx.StaticText(
            sr_box, label=self._focus_delay_value_text(settings.webview_focus_delay_level)
        )
        self.focus_delay_slider.Bind(wx.EVT_SLIDER, self._on_focus_delay_slider)

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
        sr_sizer.Add(self.focus_delay_slider, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 16)
        sr_sizer.Add(self.focus_delay_value_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
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

        self.announce_slider = wx.Slider(
            sr_box,
            value=min(max(getattr(settings, "announcement_hold_ms", 1800), 0), 6000),
            minValue=0,
            maxValue=6000,
            style=wx.SL_HORIZONTAL,
        )
        self.announce_slider.SetTickFreq(1000)
        # 300 ms per arrow of a 6000 ms range is 5 percent, the same
        # audible step as the silence hold above; the old one-unit
        # step was 0.017 percent and re-announced "30 percent" forever.
        self.announce_slider.SetLineSize(300)
        self.announce_slider.SetPageSize(1000)
        self.announce_slider.SetName(
            lang.t(
                "dialogs", "set_announce_name",
                default="Announcement hold in milliseconds",
            )
        )
        self.announce_value_label = wx.StaticText(
            sr_box,
            label=self._announce_value_text(
                getattr(settings, "announcement_hold_ms", 1800)
            ),
        )
        self.announce_slider.Bind(wx.EVT_SLIDER, self._on_announce_slider)

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

        self.announce_actions_checkbox = wx.CheckBox(
            sr_box,
            label=lang.control_label(
                "set_announce_actions", "Announce message &actions"
            ),
        )
        self.announce_actions_checkbox.SetValue(
            getattr(settings, "announce_actions", True)
        )

        sr_sizer.Add(announce_label, 0, wx.ALL, 10)
        sr_sizer.Add(self.announce_slider, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 16)
        sr_sizer.Add(self.announce_value_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sr_sizer.Add(announce_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sr_sizer.Add(
            self.announce_actions_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16
        )

        sizer.Add(sr_sizer, 0, wx.EXPAND | wx.ALL, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

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

        self.autosave_slider = wx.Slider(
            panel,
            value=min(max(getattr(settings, "draft_autosave_seconds", 60), 0), 600),
            minValue=0,
            maxValue=600,
            style=wx.SL_HORIZONTAL,
        )
        self.autosave_slider.SetTickFreq(60)
        # 30 seconds per arrow of a 600 second range is 5 percent --
        # the same audible step as the sliders in the screen reader
        # group above, and for the same reason: a screen reader
        # announces a trackbar as a percentage, so a one-unit step
        # here re-announced the same figure however long you held the
        # arrow key.
        self.autosave_slider.SetLineSize(30)
        self.autosave_slider.SetPageSize(60)
        self.autosave_slider.SetName(
            lang.t(
                "dialogs", "set_autosave_name",
                default="Draft autosave interval in seconds",
            )
        )
        self.autosave_value_label = wx.StaticText(
            panel,
            label=self._autosave_value_text(
                getattr(settings, "draft_autosave_seconds", 60)
            ),
        )
        self.autosave_slider.Bind(wx.EVT_SLIDER, self._on_autosave_slider)

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
        sizer.Add(self.autosave_slider, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 16)
        sizer.Add(self.autosave_value_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(autosave_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        auto_purge_label = wx.StaticText(
            panel,
            label=lang.t(
                "dialogs", "set_auto_purge_label",
                default="Auto-purge old Trash and Junk:",
            ),
        )
        auto_purge_label.SetFont(auto_purge_label.GetFont().Bold())

        self.auto_purge_slider = wx.Slider(
            panel,
            value=min(max(getattr(settings, "auto_purge_days", 30), 0), 365),
            minValue=0,
            maxValue=365,
            style=wx.SL_HORIZONTAL,
        )
        self.auto_purge_slider.SetTickFreq(30)
        self.auto_purge_slider.SetLineSize(15)
        self.auto_purge_slider.SetPageSize(30)
        self.auto_purge_slider.SetName(
            lang.t(
                "dialogs", "set_auto_purge_name",
                default="Auto-purge Trash and Junk after this many days",
            )
        )
        self.auto_purge_value_label = wx.StaticText(
            panel,
            label=self._auto_purge_value_text(
                getattr(settings, "auto_purge_days", 30)
            ),
        )
        self.auto_purge_slider.Bind(wx.EVT_SLIDER, self._on_auto_purge_slider)

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
        sizer.Add(self.auto_purge_slider, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 16)
        sizer.Add(self.auto_purge_value_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(auto_purge_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.bare_key_shortcuts_checkbox = wx.CheckBox(
            panel,
            label=lang.control_label(
                "set_bare_keys",
                "Enable single-key &S (flag), A (archive), J "
                "(mark junk)/Shift+J (mark not junk), W (watch "
                "thread) and K (ignore thread) shortcuts in the "
                "message list",
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

        sizer.Add(self.bare_key_shortcuts_checkbox, 0, wx.ALL, 10)
        sizer.Add(
            bare_key_shortcuts_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10
        )

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
                "muted on its own in Account Settings.",
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

        self._refresh_security_status()
        sizer.Add(security_sizer, 0, wx.EXPAND | wx.ALL, 10)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

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

        panel.SetSizer(sizer)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        outer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, outer)

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
        wx.MessageBox(
            lang.t(
                "dialogs", "shortcut_created_settings",
                default="Shortcut created:\n%s\n\nIt starts ZBox from:\n%s"
                % (path, base),
                path=path, base=base,
            ),
            lang.t("dialogs", "title_desktop_shortcut", default="Desktop Shortcut"),
            wx.OK | wx.ICON_INFORMATION, self,
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

    def _silence_value_text(self, ms):
        ms = max(0, min(2000, int(ms)))
        if ms == 0:
            return "0 ms — off (recommended)"
        return f"{ms} ms ({ms / 1000:.1f} seconds)"

    def _on_silence_slider(self, _event):
        self.silence_value_label.SetLabel(
            self._silence_value_text(self.silence_slider.GetValue())
        )

    def _announce_value_text(self, ms):
        ms = max(0, min(6000, int(ms)))
        if ms == 0:
            return "0 \u2014 stays until you press a key"
        return "%d ms (%.1f seconds)" % (ms, ms / 1000)

    def _on_announce_slider(self, _event):
        self.announce_value_label.SetLabel(
            self._announce_value_text(self.announce_slider.GetValue())
        )

    def _focus_delay_value_text(self, level):
        level = max(1, min(10, int(level)))
        ms = level * 100
        return f"Level {level} \u2014 {ms} ms ({ms / 1000:.1f} seconds)"

    def _on_focus_delay_slider(self, _event):
        self.focus_delay_value_label.SetLabel(
            self._focus_delay_value_text(self.focus_delay_slider.GetValue())
        )

    def _autosave_value_text(self, seconds):
        seconds = max(0, min(600, int(seconds)))
        if seconds == 0:
            return lang.t('dialogs', 'set_autosave_off', default="Off \u2014 use Save Draft (Ctrl+S) to save manually")
        if seconds < 60:
            return lang.t('dialogs', 'set_autosave_seconds', default='Every {seconds} seconds', seconds=seconds)
        minutes = seconds / 60
        return f"Every {seconds} seconds ({minutes:.1f} minutes)"

    def _on_autosave_slider(self, _event):
        raw = self.autosave_slider.GetValue()
        # 1-14 isn't a real choice (settings_manager clamps it up to
        # 15 on save) -- snapping the label to what will actually be
        # stored, live, avoids the slider claiming "every 8 seconds"
        # for a value that becomes 15 the moment you click OK.
        effective = 0 if raw == 0 else max(15, raw)
        self.autosave_value_label.SetLabel(self._autosave_value_text(effective))

    def _auto_purge_value_text(self, days):
        days = max(0, min(365, int(days)))
        if days == 0:
            return lang.t('dialogs', 'set_purge_off', default="Off \u2014 Trash and Junk are never auto-purged")
        if days == 1:
            return lang.t('dialogs', 'set_purge_one_day', default="After 1 day")
        return lang.t('dialogs', 'set_purge_days', default='After {days} days', days=days)

    def _on_auto_purge_slider(self, _event):
        self.auto_purge_value_label.SetLabel(
            self._auto_purge_value_text(self.auto_purge_slider.GetValue())
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
        self.settings.html_silence_hold_ms = self.silence_slider.GetValue()
        self.settings.webview_focus_delay_level = self.focus_delay_slider.GetValue()
        self.settings.announcement_hold_ms = self.announce_slider.GetValue()
        self.settings.announce_actions = self.announce_actions_checkbox.GetValue()
        self.settings.show_message_headers = self.headers_checkbox.GetValue()
        self.settings.attachment_save_dir = self.attachment_dir_field.GetValue().strip()
        self.settings.bare_key_shortcuts_enabled = self.bare_key_shortcuts_checkbox.GetValue()
        self.settings.draft_autosave_seconds = self.autosave_slider.GetValue()
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
        self.settings.auto_purge_days = self.auto_purge_slider.GetValue()
        return self.settings
