"""
The compose message body, and the two engines that can be it.

app/compose_panel.py talks to one object and does not care which
engine is behind it. That is what keeps the composer's own code close
to what it was when the body was a plain edit box.

Trix in a WebView is the body. The markup engine in a plain
wx.TextCtrl is the automatic fallback, chosen when no WebView backend
can be created on this machine -- decided here in code rather than
offered as a setting, because a writer should never have to know which
one they are on before they can write.

The one real difference from a plain edit box: a WebView body cannot
be read synchronously. The page pushes its content back on every
change and this module keeps the latest copy, so the dirty check and
the autosave tick read it as cheaply as GetValue ever did. Send and a
manual Save Draft call flush() first, because those two are the places
where one stale keystroke would actually leave the building.
"""

import json
import logging
import os
import sys

import wx

import body_html
import lang
import trix_html
import trix_page
from body_webview import BodyWebView

logger = logging.getLogger("zbox.composebody")

# How long a flush waits for the page before giving up and answering
# from the mirror. A send that silently never happens is far worse
# than a send carrying one keystroke less than the screen shows.
FLUSH_TIMEOUT_MS = 1500

# Which formatting the markup fallback can actually express, given
# body_html.ALLOWED. Anything absent is reported rather than silently
# ignored.
MARKUP_TAGS = {
    "bold": "b",
    "italic": "i",
    "quote": "blockquote",
    "heading1": "h1",
}
MARKUP_LISTS = {"bullet": "ul", "number": "ol"}


def _attribute_name(attribute):
    """What a piece of formatting is called out loud. The attribute
    name itself is the code's and is never translated."""
    default = trix_page.ATTRIBUTE_LABELS.get(attribute, attribute)
    return lang.t(
        "actions_announcements", "format_word_" + attribute, default=default
    )


def vendor_dir():
    """
    Where the vendored Trix lives, from source and frozen.

    From source it is app/vendor/trix, beside this file. In a build
    PyInstaller carries it as data into the contents folder, which the
    spec names apps_files, so it lands at apps_files/vendor/trix. The
    same two-place fallback Paths already uses for the documentation
    and licence files, kept here rather than added to app/paths.py so
    this work touches one fewer file.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "vendor", "trix")]
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", here)
        candidates.insert(0, os.path.join(base, "vendor", "trix"))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "trix.umd.min.js")):
            return candidate
    return None


def read_vendor_asset(name):
    folder = vendor_dir()
    if not folder:
        return ""
    try:
        with open(os.path.join(folder, name), "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        logger.warning("Could not read the vendored Trix asset %s.", name)
        return ""


def make_body(parent, on_command=None, on_announce=None, on_link=None,
              initial_html=""):
    """
    Builds the message body. Trix when a WebView can be created and
    the vendored Trix is actually on disk, the markup engine
    otherwise. Never raises: a composer that will not open is worse
    than one whose body is plainer than intended.
    """
    trix_js = read_vendor_asset("trix.umd.min.js")
    if trix_js:
        body = TrixBody(
            parent, on_command=on_command, on_announce=on_announce,
            on_link=on_link, initial_html=initial_html, trix_js=trix_js,
        )
        if body.using_webview:
            return body
        body.teardown()
        logger.info("No WebView for the compose body; using the markup engine.")
    else:
        logger.warning(
            "The vendored Trix files were not found; using the markup engine."
        )
    return MarkupBody(parent, initial_html=initial_html)


class MarkupBody:
    """
    The fallback: a plain multiline wx.TextCtrl, a real Win32 edit
    control, holding markup the writer types as text. Nothing here can
    fail to be read by a screen reader, which is the entire point of
    it being the fallback.
    """

    rich = False

    def __init__(self, parent, initial_html=""):
        # TE_PROCESS_TAB stays off, so Tab and Shift+Tab navigate
        # instead of being swallowed as an indent.
        self.control = wx.TextCtrl(parent, style=wx.TE_MULTILINE)
        self.control.SetName(
            lang.t("dialogs", "name_message_body", default="Message body")
        )
        if initial_html:
            self.control.SetValue(body_html.html_to_text(initial_html))
            self.control.SetInsertionPoint(0)

    @property
    def using_webview(self):
        return False

    def get_text(self):
        try:
            return self.control.GetValue()
        except RuntimeError:
            return ""

    def get_html(self):
        """A fragment, not a document. compose_panel wraps it at send
        time, and both engines have to hand back the same shape or one
        of them ends up with an html element inside a body."""
        text = self.get_text()
        if not body_html.has_formatting(text):
            return ""
        return body_html.markup_to_html(text)

    def has_content(self):
        return bool(self.get_text().strip())

    def set_html(self, html):
        self.control.SetValue(body_html.html_to_text(html))
        self.control.SetInsertionPoint(0)

    def insert_html(self, html):
        self.control.WriteText(body_html.html_to_text(html))

    def insert_text(self, text):
        self.control.WriteText(text)

    def request_document(self, callback):
        """An edit box answers immediately. The callback shape matches
        the WebView engine so the composer has one code path."""
        callback()

    @property
    def document_text(self):
        return self.get_text()

    def replace_range(self, start, end, text):
        self.control.Replace(start, end, text)

    def replace_ranges(self, changes):
        """Applied in order, each against the text as it stood after
        the previous one. An edit box changes nothing underneath them,
        so a plain loop is correct here. Returns how many landed, the
        same shape the Trix body answers with, so the composer can say
        what actually happened on either engine."""
        for start, end, text in changes:
            self.control.Replace(start, end, text)
        return len(changes)

    def select_range(self, start, end):
        self.control.SetFocus()
        self.control.SetSelection(start, end)

    def apply_format(self, attribute):
        """Wraps the selection, or the insertion point, in the markup
        the engine understands. Returns what to say out loud."""
        tag = MARKUP_TAGS.get(attribute)
        name = _attribute_name(attribute)
        if tag:
            start, end = self.control.GetSelection()
            selected = self.control.GetStringSelection()
            self.control.Replace(start, end, "<%s>%s</%s>" % (tag, selected, tag))
            return lang.t(
                "actions_announcements", "format_applied",
                default="%s applied." % name, formatting=name,
            )
        list_tag = MARKUP_LISTS.get(attribute)
        if list_tag:
            self.control.WriteText("<%s>\n<li></li>\n</%s>" % (list_tag, list_tag))
            return lang.t(
                "actions_announcements", "format_inserted",
                default="%s inserted." % name, formatting=name,
            )
        return lang.t(
            "actions_announcements", "format_not_available",
            default="%s is not available in the plain message body." % name,
            formatting=name,
        )

    def apply_link(self, url):
        start, end = self.control.GetSelection()
        selected = self.control.GetStringSelection() or url
        self.control.Replace(start, end, '<a href="%s">%s</a>' % (url, selected))
        return lang.t(
            "actions_announcements", "link_applied", default="Link applied."
        )

    def describe_formatting(self):
        return lang.t(
            "actions_announcements", "plain_body_formatting",
            default="The plain message body shows its formatting as tags.",
        )

    def diagnostics(self):
        """A plain-text report on the page, gathered without using the
        bridge. Returns a list of lines."""
        return ["Engine: markup fallback, a plain edit control.",
                "There is no page, so there is nothing to probe.",
                "Characters in the body: %d" % len(self.get_text())]

    def set_hotkey_logging(self, on):
        """Nothing to do here. This engine is a real edit control, so
        the panel's own key hook already sees every combination
        pressed in it."""
        return bool(on)

    def flush(self, callback):
        callback()

    def focus(self):
        try:
            self.control.SetFocus()
        except RuntimeError:
            pass

    def enable(self, enabled):
        try:
            self.control.Enable(bool(enabled))
        except RuntimeError:
            pass

    def teardown(self):
        pass


class TrixBody:
    """
    Trix in ZBox's own WebView.

    Everything the page needs is inlined into one document, because a
    page set with no base URL cannot link to a stylesheet or a script.
    """

    rich = True

    def __init__(self, parent, on_command=None, on_announce=None, on_link=None,
                 initial_html="", trix_js=""):
        self._on_command = on_command
        self._on_announce = on_announce
        self._ready = False
        self._html = ""
        self._text = ""
        self._document = ""
        self._attributes = set()
        self._flush_waiting = []
        self._flush_timer = None
        self._pending_html = initial_html or ""
        self._want_focus = False
        self._hotkeys = False

        self.web = BodyWebView(
            parent,
            on_message=self._on_message,
            on_link=on_link,
            on_ready=self._on_ready,
            name="Message body",
        )
        self.control = self.web.control
        if self.web.using_webview:
            self.web.set_document(
                trix_page.document_html(
                    read_vendor_asset("trix.css"), trix_js, is_rtl=lang.is_rtl(),
                )
            )

    @property
    def using_webview(self):
        return self.web.using_webview

    # --- what the composer asks it ------------------------------------

    def get_text(self):
        return self._text

    def get_html(self):
        """The message-ready HTML, converted from what Trix wrote.
        Empty when nothing was actually typed, so the caller can tell
        an empty body from a formatted one."""
        return trix_html.to_email_html(self._html)

    def has_content(self):
        return trix_html.has_content(self._html)

    def set_html(self, html):
        if not self._ready:
            self._pending_html = html or ""
            return
        self._run(
            trix_page.SET_HTML.replace(trix_page.HTML_ARG, json.dumps(html or "")), "set_html"
        )

    def insert_html(self, html):
        self._run(
            trix_page.INSERT_HTML.replace(trix_page.HTML_ARG, json.dumps(html or "")), "insert_html"
        )

    def insert_text(self, text):
        self._run(
            trix_page.INSERT_TEXT.replace("TEXT", json.dumps(text or "")), "insert_text"
        )

    def request_document(self, callback):
        """Reads the editor's own document string and calls back at
        once, on the caller's own stack.

        This is not the same text as the mirror holds. The mirror
        carries what the body reads as; this carries what Trix counts
        its positions against, and a correction applied at a position
        from the wrong one lands on the wrong word.

        Read through RunScript's return value, never through the
        message bridge. The bridge stays one way, page to Python,
        exactly as the reading view's hotkey bridge is: nothing asks
        it a question and nothing waits for an answer. That is what
        removes the parked callback which, when a reply never came,
        sat in front of every later request and made the next chord do
        nothing at all, silently. The callback shape is kept so the
        markup fallback and every caller stay as they were.
        """
        if not self.web.using_webview or not self._ready:
            callback()
            return
        ok, text = self.web.run_script_result(
            trix_page.GET_DOCUMENT, "get_document"
        )
        if ok and text is not None:
            self._document = text
        callback()

    @property
    def document_text(self):
        return self._document

    def replace_range(self, start, end, text):
        """One correction, in place, leaving the formatting around it
        untouched."""
        self.replace_ranges([(start, end, text)])

    def replace_ranges(self, changes):
        """A whole run of corrections, in one script.

        Not a loop over replace_range, and that matters. Sent as
        separate scripts, Trix re-renders in the gaps between them and
        every offset after the first is counted against a document that
        has already moved, which puts the corrections in the wrong
        places. One script keeps the editor still for the whole run.
        """
        edits = [
            [int(start), int(end), text or ""] for start, end, text in changes
        ]
        if not edits:
            return 0
        script = trix_page.REPLACE_MANY.replace("EDITS", json.dumps(edits))
        ok, raw = self.web.run_script_result(script, "replace_many")
        if not ok:
            logger.warning("The corrections script did not run.")
            return 0
        try:
            result = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            logger.warning("The corrections script returned %r.", raw)
            return 0
        for failure in result.get("failed") or []:
            logger.warning("Trix refused a correction: %s", failure)
        text = result.get("text")
        if text is not None:
            self._document = text
        return int(result.get("applied") or 0)

    def select_range(self, start, end):
        """Puts the caret on a range and leaves focus in the editor.
        Spell check uses this to walk from one misspelling to the next;
        Chromium's own context menu then supplies the suggestions."""
        script = (
            trix_page.SELECT_RANGE
            .replace("START", str(int(start)))
            .replace("END", str(int(end)))
        )
        self._run(script, "select_range")

    def apply_format(self, attribute):
        self._run(
            trix_page.FORMAT.replace("NAME", json.dumps(attribute)), "format"
        )
        # The page answers with an attribute change, which is what
        # actually gets announced, so there is nothing to say here.
        return None

    def apply_link(self, url):
        self._run(trix_page.LINK.replace("URL", json.dumps(url or "")), "link")
        return lang.t(
            "actions_announcements", "link_applied_to_selection",
            default="Link applied to the selection.",
        )

    def describe_formatting(self):
        self._run(trix_page.ASK_ATTRS, "ask_attrs")
        return None

    def diagnostics(self):
        """
        What the page actually is, read back through RunScript rather
        than through the bridge.

        That distinction is the entire value of this: when chords stop
        working the bridge is the prime suspect, and anything that
        reports through it can only ever say nothing. This says whether
        wx injected its handler, whether the chord table loaded,
        whether the key listener installed, whether Trix started, and
        what the page last threw.
        """
        if not self.web.using_webview:
            return ["No WebView was created for this body."]
        lines = ["WebView: created. Page loaded: %s." % (
            "yes" if self._ready else "NO -- nothing below is reliable"
        )]
        ok, raw = self.web.run_script_result(trix_page.PROBE, "probe")
        if not ok:
            lines.append("The probe script did not run at all.")
            lines.append("That means RunScript itself is failing, not the bridge.")
            return lines
        try:
            state = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            lines.append("The probe returned something undecodable: %r" % (raw,))
            return lines
        labels = [
            ("handler", "window.zbox, injected by wx"),
            ("handler_post", "  its post method"),
            ("handler_postMessage", "  its postMessage method"),
            ("send_defined", "zbox_send, the page's own sender"),
            ("chords_defined", "the chord table"),
            ("chord_count", "  chords in it"),
            ("listener", "the keydown listener installed"),
            ("editor", "the editor element"),
            ("editor_api", "  its editor API"),
            ("trix_version", "Trix version"),
            ("toolbar_buttons", "toolbar buttons"),
            ("document_length", "characters in the document"),
            ("hotkeys", "hotkeys diagnostic"),
            ("last_error", "last error on the page"),
        ]
        for key, label in labels:
            lines.append("%-36s %s" % (label + ":", state.get(key, "?")))
        ok, used = self.web.run_script_result(trix_page.BRIDGE_TEST, "bridge_test")
        lines.append("%-36s %s" % (
            "bridge would use:", used if ok else "the test did not run"
        ))
        lines.append("")
        if state.get("handler") == "undefined":
            lines.append(
                "window.zbox is missing, so wx never injected its script "
                "message handler into this page. Everything the page sends "
                "is going through the navigation fallback."
            )
        if state.get("listener") != "yes":
            lines.append(
                "The keydown listener did not install, so no chord pressed "
                "inside the body can ever reach ZBox. Check the last error "
                "above: a script that threw before this point takes the "
                "router down with it."
            )
        if state.get("trix_version") == "not loaded":
            lines.append(
                "Trix did not load, so the vendored script is missing or "
                "failed. The body will not format anything."
            )
        return lines

    def set_hotkey_logging(self, on):
        """Turns the hotkeys diagnostic on or off in the page.

        Remembered as well as applied. The page is rebuilt for every
        compose tab, and a tab whose page has not finished loading has
        to come up already switched on rather than silently off, which
        is the failure that would make the diagnostic lie.
        """
        self._hotkeys = bool(on)
        if not self.web.using_webview or not self._ready:
            return self._hotkeys
        script = trix_page.HOTKEYS_ON if self._hotkeys else trix_page.HOTKEYS_OFF
        self.web.run_script_result(script, "set_hotkey_logging")
        return self._hotkeys

    def flush(self, callback):
        """
        Asks the page for its content and calls back once it arrives.

        The mirror is usually already right; this exists for the last
        keystroke before Send, which may not have raised a change
        event yet. If the page does not answer within
        FLUSH_TIMEOUT_MS the callback runs anyway against the mirror,
        because a send that never happens is the worse failure.
        """
        if not self.web.using_webview or not self._ready:
            callback()
            return
        self._flush_waiting.append(callback)
        if self._flush_timer is None:
            self._flush_timer = self.web.later(FLUSH_TIMEOUT_MS, self._flush_timed_out)
        self._run(
            trix_page.GET_CONTENT.replace("REASON", json.dumps("flush")), "flush"
        )

    def focus(self):
        """Native focus first, then focus the editor element inside
        the page. Before the page is ready the request is remembered
        and applied on load, because focusing a WebView with no
        document is what makes a reader announce an empty control."""
        if not self._ready:
            self._want_focus = True
            return
        if self.web.focus():
            self._run(trix_page.FOCUS, "focus")

    def enable(self, enabled):
        self.web.enable(enabled)

    def teardown(self):
        self._flush_waiting = []
        self._flush_timer = None
        self._ready = False
        self.web.teardown()

    # --- what the page tells it ---------------------------------------

    def _on_ready(self):
        if self._ready:
            return
        self._ready = True
        if self._hotkeys:
            self.set_hotkey_logging(True)
        if self._pending_html:
            html, self._pending_html = self._pending_html, ""
            self.set_html(html)
        if self._want_focus:
            self._want_focus = False
            self.focus()

    def _on_message(self, payload):
        kind = payload.get("kind")
        if kind == "content":
            self._content_arrived(payload)
        elif kind == "chord":
            if self._on_command is not None:
                self._on_command(payload.get("name", ""))
        elif kind == "attrs":
            self._attrs_arrived(payload)
        elif kind == "file_rejected":
            self._say(lang.t(
                "actions_announcements", "attach_file_instead",
                default="Dropped files are not put in the message text. "
                        "Use Attach File to send one.",
            ))
        elif kind == "keylog":
            logger.info("hotkeys | body | %s", payload.get("text") or "")
        elif kind == "error":
            logger.debug("compose body page error: %s", payload.get("text"))
        elif kind == "ready":
            logger.debug(
                "compose body ready: Trix %s, editor api %s, toolbar buttons %s",
                payload.get("version"), payload.get("api"),
                payload.get("toolbar_buttons"),
            )

    def _content_arrived(self, payload):
        self._html = payload.get("html") or ""
        self._text = payload.get("text") or ""
        if payload.get("reason") != "flush":
            return
        waiting, self._flush_waiting = self._flush_waiting, []
        if self._flush_timer is not None:
            try:
                self._flush_timer.Stop()
            except RuntimeError:
                pass
            self._flush_timer = None
        for callback in waiting:
            callback()

    def _flush_timed_out(self):
        logger.warning(
            "The compose body did not answer a content request in time; "
            "using the last content it sent."
        )
        self._flush_timer = None
        waiting, self._flush_waiting = self._flush_waiting, []
        for callback in waiting:
            callback()

    def _attrs_arrived(self, payload):
        names = set(payload.get("names") or [])
        reason = payload.get("reason", "change")
        if reason == "ask":
            self._attributes = names
            if names:
                here = ", ".join(
                    _attribute_name(name) for name in sorted(names)
                )
                self._say(lang.t(
                    "actions_announcements", "formatting_here",
                    default="Formatting here: {formatting}.",
                    formatting=here,
                ))
            else:
                self._say(lang.t(
                    "actions_announcements", "no_formatting_here",
                    default="No formatting here, plain text.",
                ))
            return
        turned_on = names - self._attributes
        turned_off = self._attributes - names
        self._attributes = names
        spoken = []
        for name in sorted(turned_on):
            spoken.append(lang.t(
                "actions_announcements", "format_on",
                default="%s on" % _attribute_name(name),
                formatting=_attribute_name(name),
            ))
        for name in sorted(turned_off):
            spoken.append(lang.t(
                "actions_announcements", "format_off",
                default="%s off" % _attribute_name(name),
                formatting=_attribute_name(name),
            ))
        if spoken:
            self._say(", ".join(spoken))

    def _say(self, text):
        if self._on_announce is not None and text:
            self._on_announce(text)

    def _run(self, script, why):
        return self.web.run_script(script, why)
