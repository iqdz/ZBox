"""
Message view panel. Opens as its own tab when a message is activated
(Enter or double-click) from the message list -- a single dedicated
block of message content, no header fields, so the screen reader's
cursor lands directly on the actual message with nothing to tab past
first.

Two view modes, switched with Ctrl+B or the View > Toggle Message
View menu item (the view a message opens in is chosen in Settings):

1. Text view. A plain, read-only wx.TextCtrl -- the
   same proven-accessible pattern already used for the compose body
   and the inline reader pane. It is a single ordinary wx control
   with zero internal focus stops, doesn't intercept anything at the
   OS/browser level, and is immediately, synchronously focusable with
   no async page-load timing to account for. The trade-off is no
   rendered visual HTML (images inline, exact layout, colors).
   This was the default until 9 September 2026, when the HTML view
   had been worked on long enough that opening every message in the
   plainer of the two stopped paying for itself. It stays fully
   supported and one Ctrl+B away, and it is still the mode that
   depends on nothing but wx -- no browser engine, no page load, no
   async focus timing.

2. HTML view (the default since 9 September 2026). A wx.html2.WebView
   (Edge/Chromium via WebView2) renders the message's original HTML:
   inline images, layout, colors. Whether a message opens in Text or
   HTML is chosen in Settings ("Message body view when opening a
   message"); Ctrl+B inside an open message switches either way. The
   browser is created lazily -- only when an HTML view is actually
   needed -- and destroyed again on the way back to Text, so a
   session that stays in Text view never even owns a browser
   window. Before
   focus lands, ZBox first waits out a short delay (Settings:
   "WebView accessibility focus delay", a level 1-10 worth 100-1000
   ms, default level 6 / 600 ms) after the page reports itself
   loaded -- WebView2 firing its loaded event and Windows'
   accessibility layer (UIA) finishing that page's own accessible
   tree are two different moments, and focusing in the gap between
   them is what makes NVDA/JAWS announce a generic "Web content
   page" instead of the real message, or report the control
   unavailable for a stretch right after it opens. The instant the
   (now-settled) loaded page is focused, ZBox may also press and
   hold Ctrl for the duration chosen in Settings ("HTML
   screen-reader silence hold", 0-2000 ms, off by default) via
   wx.UIActionSimulator; a Ctrl keypress interrupts NVDA/JAWS speech,
   so the browser's own first announcement ("wxWebView ...") is cut
   off as it starts, and holding the key keeps speech suppressed
   across the whole announcement window. The loaded HTML is also
   always given a
   human-readable <title> first (see _ensure_html_document_title),
   so UI Automation resolves the document's name to the message
   subject instead of the internal base64 data: URL -- the actual
   root cause of the garbled announcement. The wxWebView wrapper
   itself is exposed to accessibility as a plain unlabeled client
   pane (_QuietWebViewAccessible), so the screen reader does not
   announce the wrapper's class name on focus either. Feedback then
   begins from the rendered message itself. WebView2 still carries
   two structural accessibility
   costs:

   a. Escape, Ctrl+W and Ctrl+Tab are browser-reserved shortcuts
      that Chromium can intercept internally before they ever reach
      wx's own event system; there is no supported way from Python
      to hook the underlying
      ICoreWebView2Controller.AcceleratorKeyPressed event. That is a
      limitation of wxWidgets' Edge backend rather than an open bug
      here: the JS bridge described below carries all three, and the
      user confirmed the hotkeys working in HTML view on
      9 September 2026. Do not re-derive the old "no Python-level
      fix" verdict from this paragraph -- it describes what wx does
      not give you, not what ZBox does.
   b. Every link inside the loaded HTML becomes its own independent
      Tab stop inside the DOM, making the rest of the app a long
      Tab-trip away.

   While the WebView has focus, ZBox's normal wx accelerators and
   EVT_CHAR_HOOK cannot be relied on for those keys, so the page
   injects a small JavaScript keydown listener that posts the pressed
   hotkey back through the script message bridge
   (AddScriptMessageHandler / window.hotkey.postMessage), with a
   zbox://hotkey/ navigation as a last-resort channel when the
   script-message bridge is unavailable. That is what makes Escape /
   Ctrl+W / Ctrl+F4 / Ctrl+B / Ctrl+L / Ctrl+R / Ctrl+Shift+R / F5 /
   Shift+F5 / Ctrl+S / Ctrl+P / Ctrl+Tab / F6 work even with keyboard
   focus inside the rendered HTML. F6 is the one that matters most
   for b: it is what moves focus back out of the document, and with
   focus on an ordinary wx control again the menu bar (Alt / F10) is
   reachable normally, so those two need no bridging of their own.
"""

import html
import logging
import os
import re
from email import policy as email_policy
from email.parser import BytesParser

import wx

import himalaya_client
import lang
from accessible import make_read_only_viewer, make_silent_focus_target
from content_blocker import filter_message_html
from envelope_format import _envelope_sender_email_only, _message_header_block
from message_body import extract_message_body, extract_message_html, html_to_text, clean_body_text

logger = logging.getLogger("zbox.msgview")

try:
    import wx.html2
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False


# Injected into the WebView on every page load (hk.txt Method 3: the
# JS bridge). wxPython 4.3.1 exposes AddScriptMessageHandler("hotkey"),
# which makes window.hotkey.postMessage(message) available to the
# page; ZBox receives it as EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED and
# reads the payload with event.GetString(). The bridge also carries a
# shim that defines window.hotkey itself when wxWidgets' handler
# script has not been injected yet (the handler registration can race
# the first page load), and a last-resort navigation channel
# (zbox://hotkey/<KEY>), which EVT_WEBVIEW_NAVIGATING vetoes and
# dispatches -- so the hotkeys keep working even if the script-message
# channel is unavailable.
_HOTKEY_BRIDGE_JS = r"""
(function () {
  if (window.__zboxHotkeyBridge) { return; }
  window.__zboxHotkeyBridge = true;

  // wxWidgets normally defines window.hotkey via
  // AddScriptMessageHandler("hotkey"). If that script has not been
  // injected yet, shim it over the native chrome.webview channel with
  // the same {"id": ..., "message": ...} payload wxWidgets parses.
  if (!window.hotkey) {
    try {
      if (window.chrome && window.chrome.webview) {
        window.hotkey = {
          postMessage: function (message) {
            window.chrome.webview.postMessage(
              JSON.stringify({ id: 'hotkey', message: message })
            );
          }
        };
      }
    } catch (e) {}
  }

  window.addEventListener('keydown', function (event) {
    var ctrl = event.ctrlKey;
    var key = event.key;
    var message = null;
    // Delete detection falls back to event.code / event.keyCode, not
    // just event.key -- reported (2026-09-05) as "does nothing" in
    // HTML view even though the equivalent works via the Text view's
    // accelerator table, so this is defensive insurance in case JAWS
    // or WebView2 ever reports the key differently than a plain
    // browser would.
    var isDeleteKey = (key === 'Delete') || (event.code === 'Delete') || (event.keyCode === 46);
    if (ctrl && (key === 'w' || key === 'W')) { message = 'CTRL_W'; }
    else if (ctrl && key === 'F4') { message = 'CTRL_F4'; }
    else if (key === 'Escape') { message = 'ESCAPE'; }
    else if (ctrl && (key === 'b' || key === 'B')) { message = 'CTRL_B'; }
    else if (ctrl && (key === 'l' || key === 'L')) { message = 'CTRL_L'; }
    else if (ctrl && event.shiftKey && (key === 'r' || key === 'R')) { message = 'CTRL_SHIFT_R'; }
    else if (ctrl && (key === 'r' || key === 'R')) { message = 'CTRL_R'; }
    else if (event.shiftKey && key === 'F5') { message = 'SHIFT_F5'; }
    else if (key === 'F5') { message = 'F5'; }
    // F6 (cycle panes) is the way OUT of the rendered document. It is
    // a frame accelerator, so like every other key here it never
    // reaches wx while the WebView holds native focus -- which made
    // the HTML view, the default reading mode, a keyboard trap: the
    // only exits were closing the message (Escape/Ctrl+W) or Ctrl+Tab
    // to another tab, and the menu bar was unreachable too, since Alt
    // and F10 also never left the document (the same symptom already
    // recorded for Ctrl+Shift+F). Alt/F10 deliberately are NOT
    // bridged: once F6 has moved real keyboard focus onto an ordinary
    // wx control, they work natively again, so one key is the whole
    // fix and the menu keeps its normal behaviour inside the page.
    else if (key === 'F6') { message = 'F6'; }
    else if (ctrl && (key === 's' || key === 'S')) { message = 'CTRL_S'; }
    else if (ctrl && event.shiftKey && (key === 'p' || key === 'P')) { message = 'CTRL_SHIFT_P'; }
    else if (ctrl && (key === 'p' || key === 'P')) { message = 'CTRL_P'; }
    else if (ctrl && event.shiftKey && (key === 'i' || key === 'I')) { message = 'CTRL_SHIFT_I'; }
    else if (ctrl && event.shiftKey && (key === 'u' || key === 'U')) { message = 'CTRL_SHIFT_U'; }
    else if (ctrl && (key === 'u' || key === 'U')) { message = 'CTRL_U'; }
    else if (ctrl && event.shiftKey && (key === 'f' || key === 'F')) { message = 'CTRL_SHIFT_F'; }
    // Quit (Ctrl+Shift+Q, File > Exit's own accelerator). Same class
    // of problem as every other key here: with focus inside the
    // rendered document the frame's accelerator table never sees it,
    // so quitting from an open HTML message did nothing at all and
    // looked like the quit path itself was broken. Text view was
    // unaffected, which is what made it look intermittent.
    else if (ctrl && event.shiftKey && (key === 'q' || key === 'Q')) { message = 'CTRL_SHIFT_Q'; }
    // Thread flipping. Ctrl+Page Up/Down are left alone on purpose
    // (wx.Notebook's own tab switching); these Shift variants are
    // not a Chromium shortcut either, so the page sees them.
    else if (ctrl && event.shiftKey && key === 'PageDown') { message = 'CTRL_SHIFT_PAGEDOWN'; }
    else if (ctrl && event.shiftKey && key === 'PageUp') { message = 'CTRL_SHIFT_PAGEUP'; }
    else if (ctrl && event.shiftKey && key === 'Tab') { message = 'CTRL_SHIFT_TAB'; }
    else if (ctrl && key === 'Tab') { message = 'CTRL_TAB'; }
    // Delete / Shift+Delete (move to Trash / delete permanently) --
    // same keys the message list itself already handles
    // (envelope_list_panel.py), now reaching an open message too.
    // Ctrl+D is a second, reliable route to the same "move to Trash"
    // action (never SHIFT_DELETE -- Ctrl+Shift+D is not wired to
    // anything, on purpose, so there is no permanent-delete-by-
    // accident version of this shortcut): added 2026-09-05 after the
    // user's live JAWS testing found bare Delete alone still did not
    // reach ZBox in HTML view even after the isDeleteKey broadening
    // above. Bare A (Archive) used to live here too but was removed
    // the same day -- JAWS claims unmodified letter keys for its own
    // browse-mode quicknav before any page JS runs at all (confirmed
    // by the user's own screen reader; not fixable from this side),
    // so Archive-from-an-open-message was dropped rather than ship a
    // shortcut that reliably fights the screen reader. Archive still
    // works from the message list (a plain wx.ListCtrl, not document
    // content, so browse-mode quicknav never engages there).
    else if (isDeleteKey) { message = event.shiftKey ? 'SHIFT_DELETE' : 'DELETE'; }
    else if (ctrl && (key === 'd' || key === 'D')) { message = 'DELETE'; }
    // Alt+A (Archive), added 2026-09-06 at the user's request, is
    // NOT the same thing as the bare "A" tried and removed above:
    // JAWS's browse-mode quicknav only claims UNMODIFIED single
    // letters, not Alt combinations, so this should reach the page's
    // own keydown listener the way bare A never could. Still worth a
    // live screen-reader check for the same reason bare A was --
    // this file cannot run JAWS to confirm it.
    else if (event.altKey && (key === 'a' || key === 'A')) { message = 'ALT_A'; }
    if (message) {
      event.preventDefault();
      event.stopPropagation();
      var sent = false;
      try {
        if (window.hotkey) {
          window.hotkey.postMessage(message);
          sent = true;
        }
      } catch (e) { sent = false; }
      if (!sent) {
        // Last-resort channel: navigating to a zbox://hotkey/ URL.
        // The app vetoes it in EVT_WEBVIEW_NAVIGATING and dispatches
        // the embedded key, so the page never actually navigates.
        try { window.location.href = 'zbox://hotkey/' + message; } catch (e2) {}
      }
    }
  }, true);
})();
"""


# --- Accessibility JS (Edge/WebView2) --------------------------------
#
# Anchors the screen reader's virtual cursor to the first real
# text-bearing element inside the loaded message, rather than to the
# wx wrapper control around the page -- and rather than to <body>.
# NVDA/JAWS announce the wrapper's window class ("wxWebView") when
# SetFocus() lands on the wx.html2.WebView itself, before they notice
# the document inside it; moving focus on into the page makes them
# announce the document -- whose UIA name is the message subject --
# instead. tabindex=-1 makes any element focusable in engines that
# otherwise refuse non-interactive focus.
#
# Focusing <body> alone proved insufficient (user-reported: the
# browse/document virtual cursor "hangs" at the document edge and a
# single Tab press is needed to land inside the message before arrow
# keys read normally). Screen readers anchor their virtual cursor to
# the node that received focus -- but only when that node owns text
# in the virtual buffer. <body> usually owns none: an email's text
# lives in descendant divs, tables and paragraphs, so the cursor ends
# up parked on a textless document node (JAWS: "unavailable"). Tab
# "works" only because it happens to move focus to the first real
# content node. Focusing the first element with direct text instead
# places the virtual cursor at the very start of the message.
_CONTENT_ANCHOR_FOCUS_JS = r"""
(function () {
  if (window.__zboxContentAnchor) {
    // Already installed (the script is re-injected on every load);
    // re-injection runs the placement once more. place() is a no-op
    // when focus already sits inside the content, so this can never
    // yank the cursor away from where the user is reading.
    window.__zboxContentAnchor();
    return;
  }

  function hasOwnText(el) {
    var node = el.firstChild;
    while (node) {
      if (node.nodeType === 3 && (node.nodeValue || '').replace(/\s+/g, '') !== '') {
        return true;
      }
      node = node.nextSibling;
    }
    return false;
  }

  var IGNORED = {
    SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TITLE: 1, HEAD: 1,
    BR: 1, IMG: 1, INPUT: 1, TEXTAREA: 1, SELECT: 1, BUTTON: 1,
    IFRAME: 1, OBJECT: 1, EMBED: 1, VIDEO: 1, AUDIO: 1, CANVAS: 1,
    SVG: 1, MATH: 1
  };

  function firstTextElement(node) {
    if (!node || node.nodeType !== 1) { return null; }
    var tag = node.tagName || '';
    if (IGNORED[tag]) { return null; }
    if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') { return null; }
    if (hasOwnText(node)) { return node; }
    var child = node.firstElementChild;
    while (child) {
      var hit = firstTextElement(child);
      if (hit) { return hit; }
      child = child.nextElementSibling;
    }
    return null;
  }

  var anchor = null;

  function place() {
    if (!document.body) { return; }
    var active = document.activeElement;
    if (active && active !== document.body && active !== document.documentElement) {
      // Focus already sits inside the page (our anchor from an
      // earlier run, or a Tab the user pressed). Never move it.
      return;
    }
    if (!anchor) { anchor = firstTextElement(document.body); }
    var el = anchor || document.body;
    try { if (!el.hasAttribute('tabindex')) { el.setAttribute('tabindex', '-1'); } } catch (e) {}
    try { el.style.outline = 'none'; } catch (e) {}
    try { el.focus({ preventScroll: true }); } catch (e) { try { el.focus(); } catch (e2) {} }
    try { window.focus(); } catch (e) {}
  }

  window.__zboxContentAnchor = place;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', place, false);
  } else {
    place();
  }
  if (window.addEventListener) {
    window.addEventListener('load', function () { setTimeout(place, 60); }, false);
  }
  // WebView2 keeps finalizing its accessibility tree for a while
  // after the load event; retry a few times so the anchor still lands
  // even if the first attempt raced the tree. Each retry is guarded
  // by the activeElement check above, so it never steals focus from
  // the user mid-read.
  setTimeout(place, 200);
  setTimeout(place, 700);
})();
"""

# Hides whitespace-only spacer elements from the screen reader's view
# of the loaded HTML, so arrowing through a message no longer makes
# NVDA/JAWS recite "blank, blank, blank" for every empty spacer
# paragraph, indentation div or table row HTML mail is full of. Only
# the accessibility tree is touched (aria-hidden): the visual layout
# stays unchanged for sighted review. Inline elements are never
# hidden -- hiding an inline &nbsp; separator would weld two words
# together in the virtual buffer -- and block-level elements are
# hidden only when their entire subtree is blank. The script runs
# once on injection and re-runs on DOMContentLoaded, on load, and on
# a couple of delayed passes, because WebView2 keeps re-laying-out
# for a while after the load event.
_HTML_BLANK_CLEANUP_JS = r"""
(function () {
  if (window.__zboxBlankCleanup) { return; }
  window.__zboxBlankCleanup = true;

  var BLOCK = {
    p: 1, div: 1, li: 1, tr: 1, td: 1, th: 1, table: 1,
    section: 1, article: 1, aside: 1, header: 1, footer: 1,
    main: 1, ul: 1, ol: 1, blockquote: 1, h1: 1, h2: 1, h3: 1,
    h4: 1, h5: 1, h6: 1, pre: 1, form: 1, fieldset: 1, dl: 1,
    dt: 1, dd: 1, caption: 1, tbody: 1, thead: 1, tfoot: 1,
    address: 1, figure: 1, figcaption: 1
  };

  function hasRealText(s) {
    return (s || '').replace(/[\s\u00a0]+/g, '') !== '';
  }

  function blankify(el) {
    if (!el || el.nodeType !== 1) { return true; }
    var tag = (el.tagName || '').toLowerCase();
    if (tag === 'html' || tag === 'body') { return false; }
    if (el.getAttribute('aria-hidden') === 'true') { return true; }
    var role = el.getAttribute('role') || '';
    if (role && role !== 'presentation' && role !== 'none') { return false; }
    if (hasRealText(el.getAttribute('alt')) ||
        hasRealText(el.getAttribute('aria-label')) ||
        hasRealText(el.getAttribute('title'))) {
      return false;
    }
    // Direct text nodes only; textContent would double-count the
    // descendants we are about to walk.
    var text = '';
    var node = el.firstChild;
    while (node) {
      if (node.nodeType === 3) { text += node.nodeValue; }
      node = node.nextSibling;
    }
    var ownMeaningful = hasRealText(text);
    if (tag === 'br' || tag === 'wbr' || tag === 'script' ||
        tag === 'style' || tag === 'meta' || tag === 'link' ||
        tag === 'title') {
      return true;
    }
    if (tag === 'img') { return true; } // no meaningful alt => decorative
    if (tag === 'input') {
      var type = (el.getAttribute('type') || '').toLowerCase();
      return type === 'hidden';
    }
    if (tag === 'a' || tag === 'button' || tag === 'select' ||
        tag === 'textarea' || tag === 'iframe' || tag === 'object' ||
        tag === 'video' || tag === 'audio' || tag === 'canvas' ||
        tag === 'svg') {
      return false; // interactive / replaced content: leave alone
    }
    var meaningful = ownMeaningful;
    var child = el.firstElementChild;
    while (child) {
      if (!blankify(child)) { meaningful = true; }
      child = child.nextElementSibling;
    }
    if (meaningful) { return false; }
    if (!BLOCK[tag]) { return true; } // inline whitespace: inert
    el.setAttribute('aria-hidden', 'true');
    return true;
  }

  function run() {
    if (!document.body) { return; }
    var child = document.body.firstElementChild;
    while (child) {
      var next = child.nextElementSibling;
      blankify(child);
      child = next;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run, false);
  } else {
    run();
  }
  if (window.addEventListener) {
    window.addEventListener('load', function () { setTimeout(run, 50); }, false);
  }
  setTimeout(run, 250);
  setTimeout(run, 1000);
})();
"""


def _ensure_ie_compat_meta(content):
    """Inserts <meta http-equiv="X-UA-Compatible" content="IE=edge">
    so the MSHTML backend renders in its newest available mode
    instead of the IE7 quirks mode embedded hosts default to. Must
    be the first thing in <head> to take effect. No-op if already
    present."""
    if not content or re.search(r"X-UA-Compatible", content, re.IGNORECASE):
        return content
    meta = '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
    match = re.search(r"<head[^>]*>", content, re.IGNORECASE)
    if match:
        return content[: match.end()] + meta + content[match.end():]
    return meta + content


# --- Message font size/zoom and reading font (audit finding 53) ---
#
# Cheap, high-value for low-vision readers: a way to make the open
# message bigger (or pick a specific reading face) without touching
# Windows' own display scaling, which affects the whole desktop.
# reading_font_size is None until Settings or the View > Message Zoom
# commands (Ctrl+Shift+=/-/0) explicitly set one -- an untouched
# install keeps using the Text view's normal system font exactly as
# before this finding. DEFAULT_READING_FONT_POINT_SIZE is only a
# reference point for the first zoom step and the Settings dialog's
# initial spinner value; it is never applied as a font size on its
# own.
DEFAULT_READING_FONT_POINT_SIZE = 10


def clamp_reading_font_size(size):
    return max(6, min(72, int(size)))


def _effective_reading_font_family(settings):
    return (getattr(settings, "reading_font_family", "") or "").strip()


def _apply_reading_font(control, settings):
    """Applies the current message font size/zoom and optional
    reading font face to a message body TextCtrl (self.body in
    MessageViewPanel and SavedMessageViewPanel). Safe to call
    repeatedly -- e.g. once at panel construction and again on every
    View > Message Zoom command."""
    size = getattr(settings, "reading_font_size", None)
    face = _effective_reading_font_family(settings)
    if size is None and not face:
        return
    font = control.GetFont()
    if size is not None:
        font.SetPointSize(clamp_reading_font_size(size))
    if face:
        font.SetFaceName(face)
    control.SetFont(font)


def _reading_zoom_percent(settings):
    """The message font size expressed as a percentage of
    DEFAULT_READING_FONT_POINT_SIZE, for the HTML view's CSS zoom
    rule. 100 (no-op) until reading_font_size is explicitly set."""
    size = getattr(settings, "reading_font_size", None)
    if size is None:
        return 100
    size = clamp_reading_font_size(size)
    return max(50, min(400, round(size * 100 / DEFAULT_READING_FONT_POINT_SIZE)))


def _reading_style_block(settings):
    """A <style> block enforcing the current message zoom (and, if
    set, a reading font face) on an HTML message body. zoom rather
    than font-size: most HTML mail hardcodes its own font-size in
    px/pt on nested elements, which a font-size rule on <body> would
    not reach through the cascade -- zoom scales the whole rendered
    box regardless of what the message's own CSS says. Both the Edge/
    WebView2 and legacy MSHTML backends support the zoom property (it
    originated in Trident, and Chromium kept it)."""
    face = _effective_reading_font_family(settings)
    pct = _reading_zoom_percent(settings)
    rules = ""
    if pct != 100:
        rules += "body{zoom:%d%%;}" % pct
    if face:
        escaped = face.replace('"', '\\"')
        rules += 'body,body *{font-family:"%s",sans-serif !important;}' % escaped
    if not rules:
        return ""
    return "<style>%s</style>" % rules


def _inject_reading_style(content, settings):
    """Inserts _reading_style_block into the HTML head, same
    insertion strategy as _ensure_ie_compat_meta above."""
    if not content:
        return content
    style = _reading_style_block(settings)
    if not style:
        return content
    match = re.search(r"<head[^>]*>", content, re.IGNORECASE)
    if match:
        return content[: match.end()] + style + content[match.end():]
    match = re.search(r"<html[^>]*>", content, re.IGNORECASE)
    if match:
        return content[: match.end()] + "<head>" + style + "</head>" + content[match.end():]
    return style + content


def _reading_style_js(settings):
    """Live equivalent of _reading_style_block, run via
    webview.RunScript on an already-loaded message when the message
    zoom is changed while that message is open in HTML view -- baking
    the style into the page's own HTML (as _inject_reading_style does)
    only takes effect on the next load."""
    pct = _reading_zoom_percent(settings)
    face = _effective_reading_font_family(settings)
    parts = ["(function(){try{"]
    parts.append("document.body.style.zoom='%d%%';" % pct)
    if face:
        escaped = face.replace("'", "\\'")
        parts.append("document.body.style.fontFamily=\"'%s', sans-serif\";" % escaped)
    else:
        parts.append("document.body.style.fontFamily='';")
    parts.append("}catch(e){}})();")
    return "".join(parts)


def _ensure_html_document_title(content, title="Message Body"):
    """Guarantees the HTML handed to the WebView carries a
    human-readable <title>, so UI Automation's name resolution for
    the document node hits the title before falling back to the
    internal data:text/html;charset=utf-8;base64,... URL that
    WebView2 materializes from SetPage(). Without a title, NVDA/JAWS
    read the entire base64 URL aloud when the message opens.

    The email's own title is kept when present; otherwise the given
    title (the message subject, HTML-escaped) is inserted, or a
    minimal document is wrapped around a bare fragment. <title> never
    renders, so the visual layout is untouched.
    """
    if not content:
        return content
    safe_title = html.escape(title) if title else lang.t('dialogs', 'mv_body_title', default="Message Body")
    # A non-empty title already present: leave the document alone.
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL
    )
    if title_match and title_match.group(1).strip():
        return content
    # <head> present: insert the title right after the opening tag.
    match = re.search(r"<head[^>]*>", content, re.IGNORECASE)
    if match:
        insert_at = match.end()
        return (
            content[:insert_at]
            + "<title>"
            + safe_title
            + "</title>"
            + content[insert_at:]
        )
    # <html> present but no head: add a head block after the tag.
    match = re.search(r"<html[^>]*>", content, re.IGNORECASE)
    if match:
        insert_at = match.end()
        return (
            content[:insert_at]
            + "<head><title>"
            + safe_title
            + "</title></head>"
            + content[insert_at:]
        )
    # Bare fragment: wrap it in a minimal document.
    return (
        "<!DOCTYPE html><html><head>"
        "<title>"
        + safe_title
        + "</title>"
        '<meta http-equiv="content-type" content="text/html; charset=utf-8">'
        "</head><body>"
        + content
        + "</body></html>"
    )


def _ensure_forced_webview_accessibility_args():
    """
    Idempotent guard that makes sure WebView2 launches its browser
    process with --force-renderer-accessibility. main.py normally
    sets this before the app starts (ensure_webview2_accessibility_args);
    this second call covers any code path that creates a WebView
    without going through main.py. Re-running it is harmless: it only
    appends the switch when it is not already present in
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS.
    """
    flag = "--force-renderer-accessibility"
    var = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    args = os.environ.get(var, "").split()
    if flag not in args:
        args.append(flag)
        os.environ[var] = " ".join(args)


class RemoteContentBar(wx.Panel):
    """Thunderbird's blocked-content notification, as a real wx panel
    rather than something injected into the page.

    It lives outside the WebView on purpose. Anything drawn inside
    the message document is at the mercy of whichever engine is
    rendering it and of the email's own CSS, and on the Edge backend
    it would sit behind the same accessibility tree that took months
    to make readable. A native panel is in the normal tab order, is
    announced by NVDA and JAWS the same way on both backends, and
    cannot be styled out of existence by the sender.
    """

    def __init__(self, parent, summary, sender, on_show, on_always_allow, on_close=None):
        super().__init__(parent)
        self._on_close = on_close
        # Escape, Ctrl+W and Ctrl+F4 close the message from the bar
        # and its buttons too. Caught here with EVT_CHAR_HOOK, which
        # reaches this panel from whichever of its children has focus
        # before any button or accelerator handling, so closing never
        # depends on focus passing through the message panel.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.SetName(
            lang.t(
                "dialogs", "mv_blocked_notice",
                default="Blocked content notice",
            )
        )

        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.label = wx.StaticText(self, label=summary)
        sizer.Add(self.label, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 6)

        show_button = wx.Button(
            self,
            label=lang.control_label("mv_show_remote", "&Show remote content"),
        )
        show_button.SetToolTip(
            lang.t(
                "dialogs", "mv_show_remote_tip",
                default="Reload this message with its remote images and "
                        "styles. Known trackers stay blocked.",
            )
        )
        show_button.Bind(wx.EVT_BUTTON, lambda _event: on_show())
        sizer.Add(show_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)

        if sender:
            always_button = wx.Button(
                self,
                label=lang.control_label(
                    "mv_always_allow", "Always &allow this sender",
                ),
            )
            always_button.SetToolTip(
                lang.t(
                    "dialogs", "mv_always_allow_tip",
                    default="Load remote content in every message from %s."
                            % sender,
                    sender=sender,
                )
            )
            always_button.Bind(wx.EVT_BUTTON, lambda _event: on_always_allow())
            sizer.Add(always_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)

        self.SetSizer(sizer)

    @staticmethod
    def is_close_key(key, ctrl):
        """Escape, Ctrl+W or Ctrl+F4."""
        if key == wx.WXK_ESCAPE:
            return True
        return bool(ctrl) and key in (ord("W"), ord("w"), wx.WXK_F4)

    def _on_char_hook(self, event):
        if self._on_close is not None and self.is_close_key(
            event.GetKeyCode(), event.ControlDown()
        ):
            self._on_close()
            return
        event.Skip()


class MessageViewPanel(wx.Panel):
    def __init__(self, parent, main_frame, account, folder, envelope, message, thread_envelopes=None):
        super().__init__(parent)
        # This panel takes focus itself while an HTML message is still
        # loading (_park_focus_while_loading). Announced as a pane it
        # says "panel" in that gap; announced as unnamed static text
        # it says nothing, which is the whole point of parking there.
        make_silent_focus_target(self)
        self.main_frame = main_frame
        self.account = account
        self.folder = folder
        self.envelope = envelope
        self.message = message
        # Every message of the thread this one belongs to, in list
        # order, or None when it was opened outside a thread context.
        # Ctrl+Shift+Page Down / Page Up flip through it, replacing
        # THIS tab rather than adding one -- see
        # ZBoxMainFrame.open_thread_neighbour. One message view alive
        # at a time is the whole point: opening a thread's messages
        # all at once is what jammed the screen reader.
        self.thread_envelopes = list(thread_envelopes) if thread_envelopes else []
        self._view_mode = None
        self._webview_ok = False
        self.webview = None  # created lazily on first HTML switch; see class docstring
        self._webview_accessible = None
        self._webview_loaded = False
        self._pending_html_focus = False
        # Set True the instant teardown or destruction begins, so a
        # late completed-load event WebView2 fires while its controller
        # is closing is ignored rather than run against it (see
        # _on_webview_loaded and _teardown_webview).
        self._webview_closing = False
        # Every wx.CallLater this panel schedules against the CURRENT
        # WebView, so _teardown_webview can stop them. _ensure_webview
        # clears _webview_closing for the replacement WebView, so a
        # timer left pending from the old one passes every guard and
        # runs script against a new controller that has no document
        # yet -- "Error running JavaScript: failed to evaluate".
        self._pending_timers = []
        self._webview_shown = False
        # Set when the reader presses "Show remote content" for this
        # message. Deliberately per-tab and not persisted: allowing
        # one newsletter through should not quietly allow the next.
        self._remote_content_allowed = False
        # Blocked for THIS open message, whatever the sender rule says.
        # Ctrl+Shift+I's second press sets it. Deliberately in memory
        # and nowhere else: it dies with the tab, it is never written
        # to settings, and it has nothing to do with the junk rules'
        # blocked senders, which block people rather than content.
        self._remote_content_blocked_here = False
        self._block_report = None
        self.remote_bar = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        self._main_sizer = sizer

        # --- Message headers ---
        # Above the body and outside it, so the same block is there in
        # both Text and HTML view. Injecting it into the HTML would
        # mean it vanished on Ctrl+B and could be restyled by the
        # sender's own CSS.
        #
        # Focus deliberately does not land here. Getting the screen
        # reader to land in the message body took a long time to get
        # right, and putting a control in front of it would undo that;
        # the header block is one Shift+Tab away instead, and reads as
        # static text rather than an edit field.
        self.headers = None
        if getattr(main_frame.settings_manager.settings,
                   "show_message_headers", True):
            header_text = _message_header_block(envelope)
            self.headers = wx.TextCtrl(
                self,
                value=header_text,
                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL
                | wx.BORDER_SIMPLE,
            )
            self.headers.SetName(
                lang.t(
                    "dialogs", "mv_message_headers",
                    default="Message headers",
                )
            )
            make_read_only_viewer(self.headers)
            line_height = self.headers.GetCharHeight() or 16
            self.headers.SetMinSize(
                (-1, line_height * (header_text.count("\n") + 1) + line_height)
            )
            sizer.Add(self.headers, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # --- Plain text view ---
        # Built unconditionally even when the panel opens in HTML
        # view: _set_view_mode("html") hides it rather than skipping
        # it, it is the fallback whenever the WebView cannot be
        # created, and printable_text reads from the same message
        # either way.
        self.body = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY
        )
        self.body.SetName(
            lang.t("dialogs", "name_message_body", default="Message body")
        )
        make_read_only_viewer(self.body)
        body_text = extract_message_body(message)
        self.body.SetValue(body_text if body_text else "(no message body)")
        _apply_reading_font(self.body, main_frame.settings_manager.settings)
        # Delete / Shift+Delete work from inside the message body too,
        # acting on the message shown in this tab.
        sizer.Add(self.body, 1, wx.EXPAND | wx.ALL, 8)

        # --- Attachments ---
        # One attachment: a single "Save: <filename>" button, asking
        # where to save each time -- unchanged from before. More than
        # one: a read-only list (name, format, size) so the screen
        # reader announces "1 of N" and arrow keys navigate it like
        # any other list, plus one "Save All" button, since Himalaya's
        # "attachment download" has no per-attachment selector -- it
        # always downloads every attachment of a message in one call.
        # Save All writes straight to the configured folder (Settings
        # > Attachments saving folder; default %USERPROFILE%\Downloads\
        # attachments), creating it if needed, with no folder prompt.
        attachment_indexes = (message.get("attachments") or []) if isinstance(message, dict) else []
        if len(attachment_indexes) == 1:
            attachments_label = wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "mv_attachments", default="Attachments",
                ),
            )
            attachments_label.SetFont(attachments_label.GetFont().Bold())
            sizer.Add(attachments_label, 0, wx.LEFT | wx.RIGHT, 8)

            parts = message.get("parts") or []
            filename = _attachment_filename(parts, attachment_indexes[0])
            button = wx.Button(
                self,
                label=lang.t(
                    "dialogs", "mv_save_attachment",
                    default=f"Save: {filename}", filename=filename,
                ),
            )
            button.Bind(wx.EVT_BUTTON, self._make_save_handler(filename))
            sizer.Add(button, 0, wx.ALL, 8)
        elif attachment_indexes:
            parts = message.get("parts") or []

            attachments_label = wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "mv_attachments_count",
                    default=f"Attachments ({len(attachment_indexes)})",
                    count=len(attachment_indexes),
                ),
            )
            attachments_label.SetFont(attachments_label.GetFont().Bold())
            sizer.Add(attachments_label, 0, wx.LEFT | wx.RIGHT, 8)

            attachments_list = wx.ListCtrl(
                self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
            )
            attachments_list.SetName(
                lang.t("dialogs", "mv_attachments", default="Attachments")
            )
            attachments_list.InsertColumn(0, lang.t('dialogs', 'mv_col_name', default="Name"), width=220)
            attachments_list.InsertColumn(1, lang.t('dialogs', 'mv_col_format', default="Format"), width=110)
            attachments_list.InsertColumn(2, lang.t('dialogs', 'mv_col_size', default="Size"), width=90)
            for row, index in enumerate(attachment_indexes):
                filename = _attachment_filename(parts, index)
                part = parts[index] if 0 <= index < len(parts) else {}
                format_label = _attachment_format_label(part)
                size_label = _format_attachment_size(_attachment_size_bytes(part))
                attachments_list.InsertItem(row, filename)
                attachments_list.SetItem(row, 1, format_label)
                attachments_list.SetItem(row, 2, size_label)
            line_height = attachments_list.GetCharHeight() or 16
            attachments_list.SetMinSize(
                (-1, line_height * (len(attachment_indexes) + 2) + line_height)
            )
            sizer.Add(
                attachments_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8
            )

            save_all_button = wx.Button(
                self, label=lang.t("dialogs", "mv_save_all", default="Save All")
            )
            save_all_button.Bind(
                wx.EVT_BUTTON, self._make_save_all_handler(attachment_indexes)
            )
            sizer.Add(save_all_button, 0, wx.ALL, 8)

        self.SetSizer(sizer)
        self._bind_close_accelerators()

        default_mode = getattr(
            self.main_frame.settings_manager.settings, "default_message_view", "html"
        )
        self._set_view_mode("text" if default_mode == "text" else "html")
        logger.debug(
            "MessageViewPanel: default_message_view=%r -> view_mode=%r",
            default_mode,
            self._view_mode,
        )
        # The TextCtrl content is set synchronously above, so focus
        # can be given immediately with no load-event timing to
        # account for. (The WebView, when active, is focused too; the
        # frame re-applies focus_view after the notebook selects the
        # new page, which is what lands the screen reader on content.)
        self.focus_view()

    def apply_reading_settings(self):
        """Re-applies the current message font size/zoom and reading
        font (Settings, or the View > Message Zoom commands /
        Ctrl+Shift+=/-/0) to whichever view is active -- audit
        finding 53. Called once at construction (above) and again on
        every open message tab whenever the setting changes, so
        already-open messages update immediately rather than only
        the next one opened."""
        settings = self.main_frame.settings_manager.settings
        _apply_reading_font(self.body, settings)
        if self._webview_loaded:
            self._run_script(_reading_style_js(settings), "reading_style")

    # --- Printing (File > Print...) -----------------------------------

    def printable_text(self):
        """The From/To/Cc/Date/Subject header block, then the plain-
        text body, regardless of the current view mode (Text or
        HTML) -- see _PlainTextPrintout's docstring for why the body
        itself is always plain text. Headers are always included in
        a printout (audit finding 51) even when the on-screen
        "Show message headers above an open message" setting is off
        -- that setting is about screen clutter in the live view, not
        about a printed/PDF copy someone may file or hand to someone
        else, which should always be identifiable without the app
        open."""
        body_text = extract_message_body(self.message)
        body_text = body_text if body_text else "(no message body)"
        header_text = _message_header_block(self.envelope)
        return header_text + "\n\n" + body_text

    def printable_title(self):
        subject = self.envelope.get("subject") if isinstance(self.envelope, dict) else None
        return subject or "Message"

    # --- View-mode switching -----------------------------------------

    def toggle_view_mode(self):
        """Swaps Text <-> HTML. Called by Ctrl+B and the View menu."""
        new_mode = "text" if self._view_mode == "html" else "html"
        self._set_view_mode(new_mode)
        # Report the mode actually applied (falls back to Text when
        # the WebView could not be created).
        actual = self._view_mode
        spoken = lang.t(
            "actions_announcements",
            "message_view_html" if actual == "html" else "message_view_text",
            default=("Message view: HTML (rendered)." if actual == "html"
                     else "Message view: Text."),
        )
        self.main_frame.GetStatusBar().SetStatusText(spoken)
        # The mode actually applied, not the one asked for: creating
        # the WebView can fail, and this falls back to Text when it
        # does. Announcing the request rather than the result would
        # say HTML while Text is on screen.
        self.main_frame.announce_action(
            spoken,
            title=lang.t("dialogs", "title_message_view", default="Message View"),
        )

    def focus_default(self):
        """
        main_frame._close_tab_at's generic "give this tab the focus
        it wants" entry point, called when closing one tab lands the
        notebook on this one -- every other tab-page type
        (ComposePanel, SearchTabPanel, RelatedMessagesPanel) already
        implements this; this one didn't, so landing here after a
        close fell through to a bare SetFocus() on the whole panel,
        which put focus on the panel's first child in tab order (the
        read-only header block from finding 16) instead of the
        message content. Delegating to focus_view() makes landing
        here after a close behave exactly like opening the message
        fresh.
        """
        self.focus_view()

    def focus_view(self):
        """Puts keyboard focus on whichever view is active, so the
        screen reader lands directly on the message content. The
        WebView is only focused once its page has finished loading:
        focusing it earlier makes NVDA announce the still-unready
        browser ("Unavailable") and read the loading URL aloud."""
        logger.debug(
            "focus_view: mode=%r webview=%s webview_loaded=%s",
            self._view_mode,
            self.webview is not None,
            getattr(self, "_webview_loaded", False),
        )
        if self.main_frame.notebook.FindPage(self) == wx.NOT_FOUND:
            # __init__ ends with its own focus_view() call, which runs
            # before main_frame.open_tab has added this panel to the
            # notebook at all. Focusing anything now lands on a window
            # that is not on screen, Windows walks up and hands focus
            # to the notebook itself, and a focused notebook announces
            # its current selection -- "Mail tab selected", reported
            # live under JAWS on every message open.
            logger.debug("focus_view: skipped (panel not in the notebook yet)")
            return
        if self._view_mode == "html" and self.webview is not None:
            if self._webview_loaded:
                self._show_own_tab()
                self._focus_webview_document()
                logger.debug("focus_view: SetFocus on WebView")
            else:
                self._pending_html_focus = True
                self._park_focus_while_loading()
                logger.debug("focus_view: html focus deferred (page not loaded)")
        else:
            self._show_own_tab()
            self.body.SetFocus()
            logger.debug("focus_view: SetFocus on Text body")

    def _show_own_tab(self):
        """
        Brings this panel's own notebook page to the front, at the
        moment it is actually ready to be read.

        A message tab is added unselected (main_frame._open_message_tab
        passes select=False) and only comes forward from here. Adding
        it selected meant the notebook switched pages, moved focus and
        announced the switch while the HTML document was still several
        hundred milliseconds from existing -- announcing the tab the
        reader was leaving rather than the message they asked for.
        Deferring the switch to the one moment there is something to
        read is what makes an open silent until it opens.

        ChangeSelection rather than SetSelection: it changes pages
        without sending the page-changing/changed events, so wx does
        not do its own focus hand-off into the page on top of the one
        the caller is about to make deliberately.
        """
        notebook = self.main_frame.notebook
        index = notebook.FindPage(self)
        if index == wx.NOT_FOUND or notebook.GetSelection() == index:
            return
        try:
            notebook.ChangeSelection(index)
        except RuntimeError:
            pass

    def _park_focus_while_loading(self):
        """
        Holds keyboard focus on this panel itself for the few hundred
        milliseconds between the tab opening and the HTML document
        being ready to focus.

        Without it, focus is left wherever wx put it when the notebook
        selected the page: the first focusable visible child, which in
        HTML view is the message headers box (the Text body is hidden
        and the WebView is deliberately created hidden). That is a
        read-only wx.TextCtrl, and NVDA/JAWS identify it by its native
        EDIT window class regardless of the static-text accessible
        role accessible.make_read_only_viewer gives it -- so opening a
        message in HTML view announced "read only edit" and started
        reading the header block, all of which was then interrupted a
        moment later when focus jumped into the document. Focusing the
        panel itself has nothing to announce -- given the unnamed
        static-text accessible role installed in __init__ (see
        accessible.SilentPane); without it a wx.Panel announces its
        own role and the reader says a bare "panel" instead -- so the
        reader hears the message and nothing before it.

        The headers box is unaffected as a destination: it is still
        one Shift+Tab away, and going there deliberately still reads
        it out.
        """
        if not self.IsShownOnScreen():
            # Not on screen yet means this panel is not the
            # notebook's current page: MessageViewPanel.__init__ ends
            # with its own focus_view() call, which runs before
            # main_frame.open_tab has added the panel to the notebook
            # at all. Parking there focuses a window that is not
            # shown, so Windows walks up and hands focus to the
            # notebook itself -- and a focused notebook announces its
            # current selection, which at that moment is still the
            # Mail tab ("Mail tab selected", reported live under
            # JAWS). main_frame._open_message_tab re-applies
            # focus_view once the page is added and selected, which
            # is the call that should actually park.
            return
        park = getattr(self, "SetFocusIgnoringChildren", None)
        if park is None:
            # Not a wx.Panel-derived build with this method: leaving
            # focus where it is only costs the old announcement, so
            # never let this be the thing that raises.
            return
        try:
            park()
        except RuntimeError:
            pass

    def _set_view_mode(self, mode):
        if mode != "html":
            mode = "text"
        if mode == "html":
            if not HAS_WEBVIEW:
                mode = "text"
            else:
                self._ensure_webview()
                if not self._webview_ok or self.webview is None:
                    mode = "text"
        if self._view_mode == mode:
            return
        self._view_mode = mode
        if mode == "html":
            self.body.Show(False)
            if self.webview is not None and self._webview_loaded:
                self._reveal_webview()
        else:
            # Returning to Text destroys the browser window entirely:
            # keeping it around (even hidden) lets WebView2's child
            # HWND steal focus and re-announce itself to screen
            # readers.
            self._teardown_webview()
            self.body.Show(True)
            if self._block_report is None:
                # Text readers never build a WebView, so nothing has
                # run the filter yet. Run it for its report alone:
                # "12 trackers were in this message" is worth knowing
                # even when none of them could have loaded.
                _, self._block_report = self._prepare_html(
                    extract_message_html(self.message)
                )
                self._refresh_remote_bar()
        self.Layout()
        self.focus_view()

    def _ensure_webview(self):
        """Creates the wx.html2.WebView on first use. Deliberately
        lazy -- creating it in __init__ made WebView2's child HWND
        announce itself (and the message HTML as a base64 data: URL)
        to screen readers on every message open, even while hidden.
        The loaded HTML is always given a human-readable <title>
        first (see _ensure_html_document_title) -- the message
        subject -- so the document's UIA name resolves to that
        instead of the internal base64 data: URL. The loaded page is
        focused in _on_webview_loaded, which then optionally presses
        and holds Ctrl (Settings: HTML screen-reader silence hold)
        to silence NVDA's browser announcement."""
        if self.webview is not None:
            return
        if not HAS_WEBVIEW:
            return
        self._webview_loaded = False
        self._pending_html_focus = False
        # A fresh WebView is not closing. _teardown_webview leaves this
        # True when it destroys the previous one (Ctrl+B back to HTML, or
        # a reload), so it must be cleared here or the new WebView would
        # ignore every load event and never come forward/focus.
        self._webview_closing = False
        # Belt and braces: _teardown_webview already empties this, but
        # a WebView created without a preceding teardown must not
        # inherit timer handles from anywhere.
        self._pending_timers = []
        try:
            # Force WebView2's accessibility tree on before the first
            # WebView is created (see the helper above; main.py also
            # sets this at startup).
            _ensure_forced_webview_accessibility_args()
            backend = self._webview_backend()
            if backend:
                self.webview = wx.html2.WebView.New(self, backend=backend)
            else:
                self.webview = wx.html2.WebView.New(self)
            logger.debug("_ensure_webview: backend=%r", backend or "default")
            self.webview.SetName(
                lang.t(
                    "dialogs", "mv_html_view", default="Message HTML view",
                )
            )
            # No SetAccessible() override here. One used to present
            # the wrapper as an unlabeled client pane to stop screen
            # readers announcing its "wxWebView" window class. It
            # never actually ran (its methods returned bare values
            # where Phoenix wants (status, value) tuples), and once
            # that was fixed it did real damage: wx installs its own
            # wxAccessible as the HWND's IAccessible, and wxAccessible
            # enumerates *wx* children -- so the rendered document,
            # which is not a wx window, disappeared from the tree
            # entirely and the message became unreadable. The window
            # class in the announcement is cosmetic; the document is
            # not. Leave the native tree alone.
            self._webview_accessible = None
            self.webview.Bind(wx.html2.EVT_WEBVIEW_LOADED, self._on_webview_loaded)
            self.webview.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_webview_navigating)
            # NEWWINDOW as well as NAVIGATING, and this is not
            # belt-and-braces: a link carrying target="_blank" -- which
            # is most links in real mail, since senders write for
            # webmail -- never raises a navigation event at all.
            # WebView2 treats it as a new-window request, wx delivers
            # it as EVT_WEBVIEW_NEWWINDOW, and with nothing bound to
            # that the click did precisely nothing: no browser, no
            # veto, no error. Reported live against a groups.io link.
            # getattr, not a direct reference, for the same reason
            # RESERVE_SPACE_EVEN_IF_HIDDEN below uses one: an
            # AttributeError raised here would be caught as "the
            # WebView could not be created" and drop the reader into
            # Text view, which is far worse than target="_blank" links
            # staying inert on a build that lacks this event.
            new_window_event = getattr(wx.html2, "EVT_WEBVIEW_NEWWINDOW", None)
            if new_window_event is not None:
                self.webview.Bind(new_window_event, self._on_webview_new_window)
            self.webview.Bind(
                wx.html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED,
                self._on_webview_script_message,
            )
            self.webview.AddScriptMessageHandler("hotkey")
            html_content, self._block_report = self._prepare_html(
                extract_message_html(self.message)
            )
            subject = None
            for source in (self.envelope, self.message):
                if isinstance(source, dict):
                    subject = source.get("subject")
                    if subject:
                        break
            subject = (subject or "").strip() or lang.t('dialogs', 'mv_body_title', default="Message Body")
            html_to_load = _ensure_html_document_title(
                html_content if html_content else "<p>(no message body)</p>",
                subject,
            )
            html_to_load = _inject_reading_style(
                html_to_load, self.main_frame.settings_manager.settings
            )
            if backend == getattr(wx.html2, "WebViewBackendIE", None):
                # MSHTML defaults an embedded host to IE7 quirks mode,
                # which mangles anything written this decade. The meta
                # tag opts the document into the newest engine the
                # machine has, with no registry changes and no effect
                # under any other backend.
                html_to_load = _ensure_ie_compat_meta(html_to_load)
            self.webview.SetPage(html_to_load, "")
            # Created hidden, revealed in _reveal_webview once the
            # page has loaded. A WebView that is visible while its
            # document is still loading is the first focusable child
            # of this panel, so wx.Notebook hands it focus the moment
            # the tab is selected -- and at that point it has no
            # document and no accessible name of its own, which is
            # exactly what NVDA/JAWS read out as "wxWebView
            # unavailable" before the real message arrives ~800ms
            # later. Hidden, it is not in the tab order and there is
            # nothing to announce.
            #
            # RESERVE_SPACE_EVEN_IF_HIDDEN matters: sizers normally
            # give a hidden window no space at all, and WebView2
            # laying its page out at zero size would reflow the
            # message the instant it became visible.
            self.webview.Show(False)
            self._webview_shown = False
            # getattr rather than a direct reference: if a wxPython
            # build ever lacks this flag, the layout is slightly worse
            # for a few hundred milliseconds. A raised AttributeError
            # here would be caught below as "the WebView could not be
            # created" and drop the reader into the Text view, which
            # is not an acceptable outcome for a cosmetic flag.
            reserve_space = getattr(wx, "RESERVE_SPACE_EVEN_IF_HIDDEN", 0)
            # The HTML view takes exactly the Text body's place, so the
            # message headers stay above it and the attachments below.
            # Looked up rather than hard-coded to 0: the header block
            # and the blocked-content bar both sit above the body and
            # either may be absent, so the index is not a constant. It
            # was 0 before those existed, which would now render the
            # message above its own headers.
            self._main_sizer.Insert(
                self._body_slot(),
                self.webview,
                1,
                wx.EXPAND | wx.ALL | reserve_space,
                8,
            )
            self._webview_ok = True
            self._refresh_remote_bar()
            # Safety net: if the load event never arrives, show it
            # anyway rather than leaving the reader with an empty tab
            # -- and bring the tab forward and focus it, since with
            # deferred selection (see _show_own_tab) a page that never
            # loads would otherwise leave the reader sitting on the
            # message list with nothing having visibly happened.
            self._later(4000, self._reveal_webview)
            self._later(4000, self._force_open_if_still_pending)
        except Exception:
            self._webview_ok = False
            self._teardown_webview()

    def _webview_backend(self):
        """Backend id for the engine chosen in Settings, or None to
        let wx pick its default. Falls back to the default whenever
        the requested engine is unavailable on this machine."""
        try:
            pref = getattr(
                self.main_frame.settings_manager.settings, "webview_backend", "edge"
            )
        except Exception:
            pref = "edge"
        if pref != "ie":
            return None
        backend = getattr(wx.html2, "WebViewBackendIE", None)
        if not backend:
            logger.debug("_webview_backend: IE backend not present in this wxPython")
            return None
        try:
            if not wx.html2.WebView.IsBackendAvailable(backend):
                logger.debug("_webview_backend: IE backend unavailable, using default")
                return None
        except Exception:
            return None
        return backend

    # --- Remote content blocking --------------------------------------

    def _prepare_html(self, html_content):
        """Runs the message HTML through the content filter and
        returns (html, report).

        The filter runs even when nothing is being blocked wholesale:
        scripts, frames and inline event handlers come out in every
        mode, and with the blocklist on, known tracker hosts stay out
        of a message the reader has explicitly chosen to show.
        """
        settings = self.main_frame.settings_manager.settings
        blocklist = getattr(self.main_frame, "blocklist", None)
        use_list = (
            blocklist is not None
            and bool(getattr(settings, "blocklist_enabled", True))
        )
        sender = _envelope_sender_email_only(self.envelope)
        block_all = bool(self._remote_content_blocked_here) or (
            bool(getattr(settings, "block_remote_content", True))
            and not self._remote_content_allowed
            and not settings.sender_allows_remote_content(sender)
        )
        html_out, report = filter_message_html(
            html_content,
            block_all_remote=block_all,
            is_blocked_host=blocklist.is_blocked if use_list else None,
        )
        logger.debug(
            "_prepare_html: block_all=%s blocklist=%s blocked=%d trackers=%d",
            block_all, use_list, report.blocked, report.trackers,
        )
        return html_out, report

    def _refresh_remote_bar(self):
        """Shows or hides the notification bar to match the last
        filter pass. Rebuilt rather than relabelled so its buttons
        always match the current state."""
        if self.remote_bar is not None:
            try:
                self._main_sizer.Detach(self.remote_bar)
            except Exception:
                pass
            self.remote_bar.Destroy()
            self.remote_bar = None

        report = self._block_report
        if report is None or not report.any_blocked:
            self.Layout()
            return

        sender = _envelope_sender_email_only(self.envelope)
        settings = self.main_frame.settings_manager.settings
        offer_sender = (
            sender
            and not self._remote_content_allowed
            and not settings.sender_allows_remote_content(sender)
        )
        self.remote_bar = RemoteContentBar(
            self,
            report.summary(),
            sender if offer_sender else "",
            self.show_remote_content,
            self._always_allow_sender,
            on_close=self._close_from_bar,
        )
        self._main_sizer.Insert(0, self.remote_bar, 0, wx.EXPAND | wx.ALL, 4)
        self.Layout()
        self.main_frame.GetStatusBar().SetStatusText(report.summary())

    def _close_from_bar(self):
        """Escape, Ctrl+W or Ctrl+F4 on the remote content bar. After
        the key event, not inside it: closing destroys the bar whose
        handler is running."""
        if getattr(self, "_closing_from_bar", False):
            return
        self._closing_from_bar = True
        logger.debug("Close key on the remote content bar.")

        def close():
            try:
                self.close_tab()
            except RuntimeError:
                pass

        wx.CallAfter(close)

    def _remote_content_showing(self):
        """Whether remote content is actually loading in this message
        right now, by the same test _prepare_html uses.

        Asked of the state rather than remembered, because four things
        decide it: the per-message block below, this message's own
        allow, the global setting, and the permanent per-sender allow.
        A toggle that trusted its own last instruction would announce
        the opposite of what is on screen the first time any of the
        other three disagreed with it.
        """
        if self._remote_content_blocked_here:
            return False
        if self._remote_content_allowed:
            return True
        settings = self.main_frame.settings_manager.settings
        if not bool(getattr(settings, "block_remote_content", True)):
            return True
        sender = _envelope_sender_email_only(self.envelope)
        return bool(settings.sender_allows_remote_content(sender))

    def toggle_remote_content(self):
        """Ctrl+Shift+I: unblock remote content in this message, or
        block it again.

        Per message and per tab. It never touches the permanent
        per-sender allow list, and it has nothing to do with the junk
        rules: those block a sender's future mail, this blocks images
        and trackers inside the message on screen.

        Kept separate from show_remote_content, which the notification
        bar's own "Show remote content" button calls. A button that
        says Show must never hide.
        """
        if self._remote_content_showing():
            self._remote_content_blocked_here = True
            self._remote_content_allowed = False
        else:
            self._remote_content_blocked_here = False
            self._remote_content_allowed = True
        self._reload_html_view()
        spoken = lang.t(
            "actions_announcements",
            "remote_content_unblocked" if self._remote_content_showing()
            else "remote_content_blocked",
            default=("Remote content unblocked." if self._remote_content_showing()
                     else "Remote content blocked."),
        )
        self.main_frame.GetStatusBar().SetStatusText(spoken)
        self.main_frame.announce_action(
            spoken,
            title=lang.t(
                "dialogs", "title_remote_content", default="Remote Content",
            ),
        )

    def show_remote_content(self):
        """Re-renders this message with its remote references intact.
        Known tracker domains stay blocked if the blocklist is on --
        the reader asked to see the pictures, not to be counted."""
        if self._remote_content_showing():
            spoken = lang.t(
                "actions_announcements", "remote_content_already_showing",
                default="Remote content is already showing in this message.",
            )
            self.main_frame.GetStatusBar().SetStatusText(spoken)
            self.main_frame.announce_action(
                spoken,
                title=lang.t(
                    "dialogs", "title_remote_content", default="Remote Content",
                ),
            )
            return
        self._remote_content_blocked_here = False
        self._remote_content_allowed = True
        self._reload_html_view()
        spoken = lang.t(
            "actions_announcements", "remote_content_unblocked",
            default="Remote content unblocked.",
        )
        self.main_frame.GetStatusBar().SetStatusText(spoken)
        self.main_frame.announce_action(
            spoken,
            title=lang.t(
                "dialogs", "title_remote_content", default="Remote Content",
            ),
        )

    def _always_allow_sender(self):
        sender = _envelope_sender_email_only(self.envelope)
        settings = self.main_frame.settings_manager.settings
        if sender and settings.allow_remote_content_from(sender):
            try:
                self.main_frame.settings_manager.save()
            except Exception:
                logger.exception("Could not save the remote content allow list.")
        self.show_remote_content()
        if sender:
            self.main_frame.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "remote_allowed_for_sender",
                default="Remote content will now load for messages from %s." % sender,
                sender=sender,
            ))

    def _reload_html_view(self):
        """Rebuilds the HTML view from the original message with the
        current policy. Switching to Text and back is what forces a
        clean WebView -- SetPage on a live WebView2 would race the
        focus handling that took so long to get right."""
        if self._view_mode == "html":
            self._set_view_mode("text")
            self._set_view_mode("html")
        else:
            self._set_view_mode("html")
        if self._view_mode != "html":
            # No WebView available on this machine: at least report
            # what was in the message.
            self._refresh_remote_bar()

    def _body_slot(self):
        """Sizer index the message body currently occupies."""
        for index, item in enumerate(self._main_sizer.GetChildren()):
            if item.GetWindow() is self.body:
                return index
        return 0

    def _reveal_webview(self):
        """Makes the loaded WebView visible. Deliberately separate
        from focusing it: revealing puts it back in the tab order,
        and doing that only once it has a document is what stops the
        empty-control announcement."""
        if self.webview is None or self._webview_shown:
            return
        try:
            self.webview.Show(True)
            self._webview_shown = True
            self.Layout()
            logger.debug("_reveal_webview: shown")
        except RuntimeError as exc:
            logger.debug("_reveal_webview: RuntimeError %r", exc)

    def _teardown_webview(self):
        # Set before Destroy(): destroying the WebView can fire a late
        # completed-load event (see _on_webview_loaded), and this flag is
        # what keeps that event from running script against a half-closed
        # WebView2 controller -- the "Error running JavaScript" dialog.
        # It must also run here (not only in Destroy) because this is the
        # Ctrl+B / reload path, where the WebView is torn down but the
        # panel lives on; _ensure_webview clears it for the next one.
        # Stop every timer scheduled against the WebView being torn
        # down, BEFORE the flag goes up: _ensure_webview clears the
        # flag again for the replacement, so a timer surviving this
        # point would fire against the new WebView with the guard
        # satisfied and no document loaded.
        for timer in self._pending_timers:
            try:
                timer.Stop()
            except RuntimeError:
                pass
        self._pending_timers = []
        self._webview_closing = True
        if self.webview is not None:
            try:
                self._main_sizer.Detach(self.webview)
            except Exception:
                pass
            self.webview.Destroy()
            self.webview = None
        self._webview_accessible = None
        self._webview_loaded = False
        self._pending_html_focus = False

    def prepare_close(self):
        """Deterministic teardown hook, called by main_frame._close_tab_at
        right before wx.Notebook.DeletePage.

        A Destroy() override alone is not enough here: DeletePage destroys
        the page from C++ (wxBookCtrlBase -> DeleteWindow -> Destroy),
        which never dispatches to the Python Destroy() override, so the
        only reliable moment to stop the WebView is here, before
        DeletePage runs. _teardown_webview sets _webview_closing first,
        so the completed-load event WebView2 fires while its controller
        shuts down is ignored in _on_webview_loaded instead of running
        script against a half-closed controller -- the "Error running
        JavaScript" dialog (0x8007139f, ERROR_INVALID_STATE). That dialog
        is raised inside wxWidgets, not as a Python exception, so no
        try/except around RunScript can suppress it; the call must simply
        never happen.
        """
        self._teardown_webview()

    def Destroy(self):
        """Fallback for any path that calls the panel's Destroy() from
        Python directly (none currently do -- tab close is DeletePage,
        which does NOT dispatch here). Kept so an idempotent teardown
        always precedes destruction if such a path is ever added."""
        self._teardown_webview()
        return super().Destroy()

    # --- Hotkeys: accelerator table (focus outside WebView) -----------

    def _bind_close_accelerators(self):
        """
        Escape closes this message tab and returns to the message
        list. Ctrl+W and Ctrl+F4 are deliberately not bound here: the
        main frame already maps both to closing the current tab, and
        having two accelerator tables handle the same key produced
        inconsistent behavior depending on which window had focus.
        The frame-level binding is reached through the parent chain
        no matter where focus is, so those keys now behave the same
        everywhere. Ctrl+B also lives at the frame level (it toggles
        this panel's view mode via toggle_view_mode).

        Forward (Ctrl+L) is the one exception that needs its own
        local entry here too: unlike Ctrl+W/Ctrl+B, it never reached
        the frame's own accelerator table while focus was inside
        self.body -- most likely because Windows' native multi-line
        Edit/RichEdit control has its own built-in Ctrl+L
        "align left" paragraph shortcut, which can claim the key
        before it gets to the frame. A local accelerator on this
        panel gets first look at the keystroke (accelerator
        translation happens before the message is ever dispatched to
        the control), the same fix shape as Escape above, so it wins
        that race instead of the native control.

        Delete, Shift+Delete and (at the time) bare A used to be
        handled the OTHER way -- EVT_KEY_DOWN bound directly on
        self.body -- and that was itself the bug: reported live, only
        Shift+Delete actually worked, plain Delete did nothing, and
        bare A never reached ZBox at all (the screen reader's OWN
        single-letter document navigation claimed it first, audibly
        announcing "no radio buttons" instead). Moved Delete/Shift+
        Delete to this same accelerator-table mechanism as Ctrl+L/
        Ctrl+R above so they win the race against RichEdit's native
        key handling, exactly like Escape already does -- accelerator
        translation happens before Windows ever turns the keystroke
        into an ordinary key event the control could otherwise
        intercept.

        Bare A (Archive) was tried here too but then REMOVED
        entirely, not just re-wired: the user's own JAWS testing
        confirmed bare letter keys are claimed by the screen reader's
        browse-mode quicknav before ZBox -- or ANY app-level
        mechanism, accelerator table included -- ever sees them, so
        there was no wiring fix available. Archive stays a message-
        list-only shortcut (a plain wx.ListCtrl, not document content,
        so quicknav never engages there).

        Plain Delete alone still did not work live even via the
        accelerator table (unlike Escape/Ctrl+L in the very same
        table), cause unconfirmed -- Ctrl+D is added below as a
        second, reliable route to the same "move to Trash" action
        while that mystery is unresolved, mirroring the same addition
        in the HTML view's _HOTKEY_BRIDGE_JS. Ctrl+Delete is added
        too since it costs nothing, though a hypothesis worth noting
        is that VK_DELETE is being claimed at a level accelerator
        tables can't win against (e.g. a screen-reader keyboard hook),
        in which case Ctrl+Delete would fail for the same reason bare
        Delete does and only Ctrl+D would actually help. Confirmed
        live 2026-09-05: Ctrl+D works.

        Shift+Delete then turned out to have the SAME problem in this
        same accelerator table, confirmed by a fresh log: pressing it
        with focus on the Text body produced NO log output at all,
        not even the permanent logger.debug line at the top of
        _on_delete_permanent_shortcut -- so the accelerator genuinely
        never fires there, matching plain Delete's own unexplained
        failure rather than being a separate bug. Applied the same
        fix: Ctrl+Shift+D reaches _on_delete_permanent_shortcut as a
        second route, not yet confirmed live.
        """
        close_id = wx.NewIdRef()
        forward_id = wx.NewIdRef()
        reply_id = wx.NewIdRef()
        reply_all_id = wx.NewIdRef()
        refresh_id = wx.NewIdRef()
        refresh_all_id = wx.NewIdRef()
        save_as_id = wx.NewIdRef()
        print_id = wx.NewIdRef()
        print_preview_id = wx.NewIdRef()
        show_remote_id = wx.NewIdRef()
        announce_sender_id = wx.NewIdRef()
        announce_copy_id = wx.NewIdRef()
        delete_id = wx.NewIdRef()
        delete_permanent_id = wx.NewIdRef()
        thread_next_id = wx.NewIdRef()
        thread_prev_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self._on_thread_next_shortcut, id=thread_next_id)
        self.Bind(wx.EVT_MENU, self._on_thread_prev_shortcut, id=thread_prev_id)
        self.Bind(wx.EVT_MENU, self._on_close_and_return, id=close_id)
        self.Bind(wx.EVT_MENU, self._on_forward_shortcut, id=forward_id)
        self.Bind(wx.EVT_MENU, self._on_reply_shortcut, id=reply_id)
        self.Bind(wx.EVT_MENU, self._on_reply_all_shortcut, id=reply_all_id)
        self.Bind(wx.EVT_MENU, self._on_refresh_shortcut, id=refresh_id)
        self.Bind(wx.EVT_MENU, self._on_refresh_all_shortcut, id=refresh_all_id)
        self.Bind(wx.EVT_MENU, self._on_save_as_shortcut, id=save_as_id)
        self.Bind(wx.EVT_MENU, self._on_print_shortcut, id=print_id)
        self.Bind(wx.EVT_MENU, self._on_print_preview_shortcut, id=print_preview_id)
        self.Bind(wx.EVT_MENU, self._on_show_remote_shortcut, id=show_remote_id)
        self.Bind(
            wx.EVT_MENU, self._on_announce_sender_shortcut,
            id=announce_sender_id,
        )
        self.Bind(
            wx.EVT_MENU, self._on_announce_copy_shortcut, id=announce_copy_id
        )
        self.Bind(wx.EVT_MENU, self._on_delete_shortcut, id=delete_id)
        self.Bind(
            wx.EVT_MENU, self._on_delete_permanent_shortcut,
            id=delete_permanent_id,
        )
        accel_table = wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, close_id),
            (wx.ACCEL_CTRL, ord("L"), forward_id),
            # Ctrl+R/Ctrl+Shift+R need the same local-accelerator fix
            # as Ctrl+L above: Windows' native RichEdit control has a
            # built-in Ctrl+R "align right" paragraph shortcut that
            # can claim the key before it reaches the frame.
            (wx.ACCEL_CTRL, ord("R"), reply_id),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("R"), reply_all_id),
            (wx.ACCEL_NORMAL, wx.WXK_F5, refresh_id),
            (wx.ACCEL_SHIFT, wx.WXK_F5, refresh_all_id),
            (wx.ACCEL_CTRL, ord("S"), save_as_id),
            (wx.ACCEL_CTRL, ord("P"), print_id),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("P"), print_preview_id),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("I"), show_remote_id),
            (wx.ACCEL_CTRL, ord("U"), announce_sender_id),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("U"), announce_copy_id),
            (wx.ACCEL_NORMAL, wx.WXK_DELETE, delete_id),
            (wx.ACCEL_SHIFT, wx.WXK_DELETE, delete_permanent_id),
            # See _bind_close_accelerators' own docstring: bare
            # Delete alone was still reported not to work live, so
            # Ctrl+D (and, for good measure, Ctrl+Delete) reach the
            # same "move to Trash" handler as a second route --
            # confirmed live 2026-09-05, this is the one that
            # actually works. Ctrl+Shift+D is the same fix applied to
            # Shift+Delete, added the same day once a fresh log with
            # the new permanent logging (see _on_delete_permanent_
            # shortcut) proved that accelerator entry produces
            # NO log output at all in Text view -- not an exception,
            # not a wrong-arguments bug, just never fires.
            (wx.ACCEL_CTRL, ord("D"), delete_id),
            (wx.ACCEL_CTRL, wx.WXK_DELETE, delete_id),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("D"), delete_permanent_id),
            # Flip through the thread this message belongs to,
            # replacing this tab. Ctrl+Page Up/Down themselves are NOT
            # available: they are wx.Notebook's own built-in tab
            # switching on Windows, and the screen reader announces
            # them as such ("To switch pages, press Control+PageDown")
            # when focus lands on the tab strip, so claiming them
            # would break the way back to the Mail tab. Ctrl+Shift+
            # Page Up/Down are unclaimed by the notebook and by every
            # other binding in ZBox.
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_PAGEDOWN, thread_next_id),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_PAGEUP, thread_prev_id),
        ])
        self.SetAcceleratorTable(accel_table)

    def _on_delete_shortcut(self, event):
        """Delete/Ctrl+D/Ctrl+Delete from inside an open message
        (Text view): move to Trash. Permanent debug line, matching
        _dispatch_hotkey's own -- added 2026-09-05 because the Text
        view side of this accelerator table has NO logging of its
        own, unlike the HTML view's _dispatch_hotkey, which made it
        impossible to tell from a log alone whether a reported "does
        nothing" meant the accelerator never fired at all, or fired
        and _run_delete itself did nothing."""
        logger.debug("_on_delete_shortcut fired (Text view move-to-Trash)")
        self.main_frame._run_delete(
            self.account, self.folder, self.envelope, False,
            after_success=self.close_tab,
        )

    def _on_delete_permanent_shortcut(self, event):
        """Shift+Delete from inside an open message (Text view):
        delete permanently, after the usual confirmation. See
        _on_delete_shortcut's docstring for why this logs."""
        logger.debug("_on_delete_permanent_shortcut fired (Text view permanent delete)")
        self.main_frame._run_delete(
            self.account, self.folder, self.envelope, True,
            after_success=self.close_tab,
        )

    def _on_announce_sender_shortcut(self, event):
        """Ctrl+U from inside an open message."""
        self.main_frame._announce_sender(copy_address=False)

    def _on_announce_copy_shortcut(self, event):
        """Ctrl+Shift+U from inside an open message."""
        self.main_frame._announce_sender(copy_address=True)

    def _on_show_remote_shortcut(self, event):
        """Ctrl+Shift+I from inside an open message tab: load this
        message's blocked images and styles."""
        self.show_remote_content()

    def _on_forward_shortcut(self, event):
        """Ctrl+L from inside an open message tab -- delegates to the
        frame's own Forward handler, which already resolves the
        message to forward from the active tab when one is open
        (see _current_message_context), so this forwards the message
        actually being viewed rather than whatever is selected back
        in the message list."""
        self.main_frame._on_forward(None)

    def _on_reply_shortcut(self, event):
        """Ctrl+R from inside an open message tab -- same delegation
        as _on_forward_shortcut, targeting the message in this tab."""
        self.main_frame._on_reply(None)

    def _on_reply_all_shortcut(self, event):
        """Ctrl+Shift+R from inside an open message tab."""
        self.main_frame._on_reply_all(None)

    def _on_refresh_shortcut(self, event):
        """F5 from inside an open message tab -- refreshes this
        message's own account/folder, same as Accounts > Refresh."""
        # Said on the press, not on completion. A refresh is a round
        # trip to the server and the key has to acknowledge itself
        # now; what it finds is reported by the refresh itself.
        self.main_frame.announce_action(
            lang.t("actions_announcements", "refreshing_this_folder",
                   default="Refreshing this folder."),
            title=lang.t("dialogs", "title_refresh", default="Refresh"),
        )
        self.main_frame._on_refresh_folder(None)

    def _on_refresh_all_shortcut(self, event):
        """Shift+F5 from inside an open message tab -- refreshes
        every account, same as Accounts > Refresh All Accounts."""
        self.main_frame.announce_action(
            lang.t("actions_announcements", "refreshing_all_accounts",
                   default="Refreshing all accounts."),
            title=lang.t("dialogs", "title_refresh_all", default="Refresh All"),
        )
        self.main_frame._on_refresh_all_accounts(None)

    def _on_save_as_shortcut(self, event):
        """Ctrl+S from inside an open message tab -- saves this
        message. (Ctrl+S inside a compose tab still saves a draft;
        that's ComposePanel's own local accelerator and never reaches
        here.)"""
        self.main_frame._on_save_message_as(None)

    def _on_print_shortcut(self, event):
        """Ctrl+P from inside an open message tab."""
        self.main_frame._on_print_message(None)

    def _on_print_preview_shortcut(self, event):
        """Ctrl+Shift+P from inside an open message tab."""
        self.main_frame._on_print_preview_message(None)

    # --- Hotkeys: JS bridge (focus inside WebView) --------------------

    def _on_webview_loaded(self, event):
        # Re-inject the hotkey bridge on every completed page load,
        # including any intermediate blank page (the script is
        # idempotent, so extra injections are harmless). There is no
        # URL check on this injection: a too-strict check here has
        # previously left the bridge un-injected and killed every
        # hotkey while focus sat inside the WebView.
        #
        # This is the one RunScript that used to run unguarded, and it
        # surfaced as a native "Error running JavaScript" message box
        # (0x8007139f / ERROR_INVALID_STATE) when a message tab was
        # closed while its WebView was still settling: WebView2 fires a
        # completed-load event as its controller shuts down, and running
        # script against a half-closed controller makes wxWebViewEdge
        # report that error in a dialog no Python try/except can
        # suppress (it is shown inside wxWidgets, not raised). The only
        # fix is to not call RunScript once teardown has begun --
        # _teardown_webview and Destroy set _webview_closing first
        # thing -- and to guard the call for the race that flag cannot
        # see (the C++ control already gone, which raises RuntimeError).
        #
        # That guard is necessary but NOT sufficient, and the missing
        # half was a second, separate dialog -- "Error running
        # JavaScript: failed to evaluate" while simply reading HTML
        # mail. _ensure_webview clears _webview_closing and rebinds
        # self.webview for the replacement, so any wx.CallLater still
        # pending from the old WebView passes both tests and runs
        # script against a brand-new controller with no document yet.
        # Those timers are now cancelled in _teardown_webview, and
        # every RunScript goes through _run_script, which suspends
        # wxLogGui for the duration of the call (so a wxLogError can
        # no longer be collected into a message box) and logs the
        # success flag the API returns and this code used to discard.
        if not self._run_script(_HOTKEY_BRIDGE_JS, "hotkey_bridge_load"):
            return
        # The content anchor is deliberately NOT placed here. Tried it
        # (an attempt at JAWS's "<subject> - Web content page" suffix,
        # on the theory that a document with an already-focused
        # element announces that element instead) and it broke the
        # thing the anchor exists for: placing DOM focus before the
        # WebView has Win32 focus does not move the screen reader's
        # virtual cursor, and place()'s own "focus is already inside
        # the content" guard then made the real placement -- the one
        # in _focus_webview_document, after SetFocus -- a no-op. The
        # cursor was left at the document edge again, needing a Tab
        # press before the arrow keys read anything, which is the
        # exact bug _CONTENT_ANCHOR_FOCUS_JS was written to fix.
        # Placement must happen after Win32 focus arrives, not before.
        #
        # RunScript is asynchronous; re-inject once more once the
        # document has definitely settled (idempotent insurance
        # against the first script landing too early).
        self._later(250, self._inject_hotkey_bridge)
        # Only the page ZBox itself loaded counts as "loaded" for
        # focus purposes -- a real navigation (which
        # _on_webview_navigating vetoes anyway) must not trigger the
        # accessibility focus handoff.
        #
        # The URL this reports depends entirely on the backend, and
        # an earlier "must start with data:" test here was wrong on
        # the one that actually ships on Windows: wxWebViewEdge
        # (WebView2) implements SetPage() via NavigateToString(),
        # whose document URL is "about:blank", not a data: URL. That
        # test therefore discarded every load event, so
        # _webview_loaded never became True, _pending_html_focus was
        # never consumed, and focus never left the subject line --
        # the screen reader sat on a WebView that was fully rendered
        # but never focused. The IE backend's data: URL and an empty
        # URL are both accepted too, so this works whichever backend
        # wx picks.
        url = event.GetURL() or ""
        logger.debug("_on_webview_loaded: url=%r", url)
        internal = (not url) or url.startswith("data:") or url == "about:blank"
        if not internal:
            logger.debug("_on_webview_loaded: IGNORED (url not internal)")
            return
        self._webview_loaded = True
        self._apply_html_blank_cleanup()
        if not self._pending_html_focus:
            # Nothing is waiting to focus it (the reader switched
            # away, or focus already landed elsewhere), so there is
            # no announcement to avoid -- just show it.
            self._reveal_webview()
        if self._pending_html_focus:
            self._pending_html_focus = False
            # Settings: WebView accessibility focus delay (level
            # 1-10, 100-1000ms). WebView2 reporting the page loaded
            # and Windows' accessibility layer (UIA) finishing that
            # page's own accessible tree are two different moments --
            # focusing in the gap between them is what produces a
            # generic placeholder announcement ("Web content page")
            # instead of the real message, or leaves the control
            # reporting unavailable for a stretch after opening.
            # Waiting this long first gives UIA time to settle before
            # focus lands on it.
            delay_ms = self._webview_focus_delay_ms()
            self._later(delay_ms, self._focus_loaded_webview)

    def _force_open_if_still_pending(self):
        """Last resort for an HTML page whose load event never
        arrived: open and focus the tab anyway. No-op once the normal
        path has already run, and safe after teardown."""
        try:
            if not self._pending_html_focus:
                return
            self._pending_html_focus = False
            logger.debug("_force_open_if_still_pending: load event never arrived")
            if not self._webview_loaded:
                # A WebView with no loaded page has no ZBox keys in it
                # (the hotkey bridge is injected on load). Live 24
                # September 2026: a newsletter never loaded and the
                # reader could not close it. Reload the HTML once; the
                # panel keeps focus meanwhile, and its own accelerators
                # (Escape, Ctrl+W, Ctrl+F4) always work there.
                self._retry_html_load()
                return
            self._focus_loaded_webview()
        except RuntimeError:
            pass

    def _retry_html_load(self):
        """Rebuilds the WebView and loads the page again, once per
        tab. Never switches to Text view: HTML is the reading mode,
        and Ctrl+B remains the reader's own choice."""
        if getattr(self, "_html_retry_done", False):
            logger.debug("_retry_html_load: second load did not arrive either.")
            self._park_focus_while_loading()
            spoken = lang.t(
                "actions_announcements", "html_view_still_loading",
                default="The message is still loading. Escape closes it.",
            )
            self.main_frame.GetStatusBar().SetStatusText(spoken)
            self.main_frame.announce_action(
                spoken,
                title=lang.t("dialogs", "title_message_view", default="Message View"),
            )
            return
        self._html_retry_done = True
        logger.debug("_retry_html_load: load event never arrived; reloading once.")
        self._teardown_webview()
        self._view_mode = None
        self._set_view_mode("html")
        self._pending_html_focus = True
        self._park_focus_while_loading()

    def _focus_loaded_webview(self):
        """Actually focuses the loaded WebView, once
        _webview_focus_delay_ms has elapsed since it reported
        itself loaded. Guarded: the tab may have closed (Escape /
        Ctrl+W / Ctrl+F4 while the delay was still pending -- the
        panel and its webview are destroyed C++-side at that point,
        so touching them raises RuntimeError, not just returning
        None), or the view may have been toggled back to Text, in
        the meantime."""
        logger.debug(
            "_focus_loaded_webview: mode=%r webview=%s",
            getattr(self, "_view_mode", None),
            self.webview is not None,
        )
        try:
            if self.webview is None or self._view_mode != "html":
                logger.debug("_focus_loaded_webview: skip (no webview or not html)")
                return
            # Reveal, bring this tab forward, and focus in one step.
            # The control enters the tab order already carrying its
            # document, so the first thing a screen reader sees of it
            # is the message -- and the tab switch happens here rather
            # than back when the tab was created, so nothing is
            # announced until there is a message to announce.
            self._reveal_webview()
            self._show_own_tab()
            if not self._focus_webview_document():
                logger.debug("_focus_loaded_webview: SetFocus failed")
                return
            webview = self.webview
            logger.debug(
                "_focus_loaded_webview: SetFocus() ok, hwnd=%s",
                webview.GetHandle() if webview is not None else None,
            )
            if webview is None:
                return
        except (RuntimeError, AttributeError) as exc:
            # AttributeError: the tab closed while this was pending and
            # its WebView is already gone (self.webview is None).
            logger.debug("_focus_loaded_webview: RuntimeError %r", exc)
            return
        # Optional Ctrl press-and-hold (Settings: HTML
        # screen-reader silence hold, 0-2000 ms; 0 = off). Pressed
        # the instant the loaded page is focused, right as
        # NVDA/JAWS starts the browser's first announcement
        # ("wxWebView ..."), the hold suppresses speech across
        # the whole announcement window.
        hold_ms = self._silence_hold_ms()
        if hold_ms > 0:
            self._silence_screen_reader(hold_ms)

    def _run_script(self, script, why):
        """The single entry point for every webview.RunScript in this
        panel. Three things it does that the scattered call sites did
        not:

        1. wx.LogNull() around the call. The "Error running
           JavaScript" box is wxWidgets' own wxLogGui collecting a
           wxLogError raised inside wxWebViewEdge -- not a Python
           exception, which is why no try/except ever suppressed it.
           wx.LogNull suspends that log target for its lifetime, so
           the error is discarded instead of shown.
        2. The success flag RunScript returns is read and logged
           rather than discarded, so a failure leaves a trace in
           zbox_debug.log. WebView script failures were not wired to
           the logger at all before this.
        3. The closing/None guard lives in one place.

        wxPython returns either a bare bool or a (success, output)
        tuple from RunScript depending on the build, so both shapes
        are accepted. Returns True only when the script actually ran.
        """
        if self._webview_closing or self.webview is None:
            logger.debug("_run_script: %s skipped (closing or no webview)", why)
            return False
        try:
            with wx.LogNull():
                result = self.webview.RunScript(script)
        except RuntimeError as exc:
            logger.debug("_run_script: %s RuntimeError %r", why, exc)
            return False
        except Exception as exc:
            logger.debug("_run_script: %s failed %r", why, exc)
            return False
        output = None
        if isinstance(result, tuple):
            ok = bool(result[0]) if result else False
            if len(result) > 1:
                output = result[1]
        else:
            ok = bool(result)
        logger.debug("_run_script: %s ok=%r out=%r", why, ok, output)
        return ok

    def _later(self, delay_ms, func, *args):
        """wx.CallLater whose handle is kept, so _teardown_webview can
        stop it. Every timer that ends up touching the WebView must be
        scheduled through here; the Ctrl-release timer in
        _silence_screen_reader deliberately is not, because a release
        that never fires would leave Ctrl held down."""
        timer = wx.CallLater(delay_ms, func, *args)
        self._pending_timers.append(timer)
        return timer

    def _inject_hotkey_bridge(self):
        """(Re)injects the JS hotkey bridge; safe to call repeatedly
        and after teardown (no-ops then)."""
        self._run_script(_HOTKEY_BRIDGE_JS, "hotkey_bridge_retry")

    def _apply_html_blank_cleanup(self):
        """(Re)runs the blank-spacer cleanup over the loaded HTML;
        safe to call repeatedly and after teardown (no-ops then).
        The injected script schedules its own later passes, so one
        injection at load is enough."""
        self._run_script(_HTML_BLANK_CLEANUP_JS, "blank_cleanup")

    def _focus_webview_document(self):
        """Focuses the rendered page itself rather than the wx wrapper
        around it. SetFocus() first (Win32 focus must reach the
        control), then a script moves focus on into the page's first
        real text-bearing element, so NVDA/JAWS announce the message
        content -- and, crucially, anchor their browse/document
        virtual cursor at the start of the message text rather than
        at the document edge, where the cursor hangs until the user
        presses Tab once (see _CONTENT_ANCHOR_FOCUS_JS). Safe after
        teardown; returns True when SetFocus() succeeded."""
        if self._webview_closing or self.webview is None:
            return False
        try:
            self.webview.SetFocus()
        except RuntimeError:
            return False
        self._run_script(_CONTENT_ANCHOR_FOCUS_JS, "content_anchor")
        return True

    def _silence_hold_ms(self):
        """Milliseconds to hold the synthetic Ctrl press after an
        HTML page loads, taken from Settings (0 = disabled)."""
        try:
            return int(
                getattr(
                    self.main_frame.settings_manager.settings,
                    "html_silence_hold_ms",
                    0,
                )
            )
        except (TypeError, ValueError):
            return 0

    def _webview_focus_delay_ms(self):
        """Milliseconds to wait after a loaded HTML page before
        focusing it, taken from Settings (level 1-10, 100-1000ms;
        see Settings: WebView accessibility focus delay). Defaults
        to level 3 / 300ms if the setting is somehow missing."""
        try:
            return int(
                getattr(
                    self.main_frame.settings_manager.settings,
                    "webview_focus_delay_ms",
                    300,
                )
            )
        except (TypeError, ValueError):
            return 300

    def _silence_screen_reader(self, hold_ms=0):
        """Sends a synthetic Ctrl keypress and holds it down for
        hold_ms milliseconds (0 disables the emulation entirely).
        Any keypress pauses NVDA/JAWS speech, and holding the key
        keeps speech suppressed for the whole announcement window;
        Ctrl alone has no side effect in the WebView or in ZBox's
        accelerators. The release is scheduled with wx.CallLater so
        the UI thread is never blocked while the key is held."""
        if hold_ms <= 0:
            return
        try:
            simulator = wx.UIActionSimulator()
            simulator.KeyDown(wx.WXK_CONTROL)
            wx.CallLater(hold_ms, simulator.KeyUp, wx.WXK_CONTROL)
        except Exception:
            pass

    def _on_webview_navigating(self, event):
        url = event.GetURL() or ""
        # Internal hotkey dispatch (see _HOTKEY_BRIDGE_JS): the page
        # navigates to zbox://hotkey/<KEY> when the script-message
        # bridge is unavailable. Veto it and act like a script message
        # so the page never actually navigates away from the email.
        if url.startswith("zbox://hotkey/"):
            event.Veto()
            self._queue_hotkey(url[len("zbox://hotkey/"):])
            return
        # Anything else that is a real destination is handled the same
        # way a new-window request is, and vetoed so the tab never
        # navigates away from the email.
        if self._open_link_elsewhere(url):
            event.Veto()

    def _on_webview_new_window(self, event):
        """
        A link the page wants opened in a new window -- overwhelmingly
        target="_blank", which is how most links in real mail are
        written, because senders write them for webmail.

        WebView2 raises these as new-window requests rather than
        navigations, so EVT_WEBVIEW_NAVIGATING never sees them. With
        nothing bound here, clicking such a link did nothing at all:
        no browser, no error, no log line. Nothing to veto either --
        the request is simply ignored unless something handles it,
        which is why the symptom was silence rather than a stray
        window.
        """
        self._open_link_elsewhere(event.GetURL() or "")

    def _open_link_elsewhere(self, url):
        """
        Sends one clicked link where it belongs, without ever
        navigating this tab away from the message. Returns True when
        the url was a real destination that was handled.

        A mailto: link is the one kind that must not leave ZBox: it is
        a request to write mail, and this is the mail client. It used
        to go to LaunchDefaultBrowser with everything else, which
        handed it to whatever the system registered as its mail
        program -- another app, or nothing at all.
        """
        if not url or url == "about:blank" or url.startswith("data:"):
            return False
        if url[:7].lower() == "mailto:":
            self.main_frame.open_compose_from_mailto(url)
            return True
        logger.debug("Opening link in the default browser: %s", url)
        wx.LaunchDefaultBrowser(url)
        return True

    def _on_webview_script_message(self, event):
        self._queue_hotkey(event.GetString())

    def _queue_hotkey(self, message):
        """Runs a key reported by the page after the WebView's own
        event has returned. Live 24 September 2026: Shift+Delete
        opened its confirmation and then closed the tab, WebView
        included, inside the WebView2 callback, and the process died
        on the way back into the destroyed control. No dialog and no
        tab close may run inside a WebView event."""
        def run():
            try:
                if self:
                    self._dispatch_hotkey(message)
            except RuntimeError:
                pass

        wx.CallAfter(run)

    def _dispatch_hotkey(self, message):
        """Runs the action for a hotkey reported by the WebView (via
        the script-message bridge or the zbox://hotkey/ navigation
        fallback)."""
        logger.debug("_dispatch_hotkey: message=%r", message)
        if message in ("CTRL_W", "CTRL_F4", "ESCAPE"):
            self.close_tab()
        elif message == "CTRL_B":
            self.toggle_view_mode()
        elif message == "CTRL_L":
            self._on_forward_shortcut(None)
        elif message == "CTRL_R":
            self._on_reply_shortcut(None)
        elif message == "CTRL_SHIFT_R":
            self._on_reply_all_shortcut(None)
        elif message == "CTRL_SHIFT_PAGEDOWN":
            self._on_thread_next_shortcut(None)
        elif message == "CTRL_SHIFT_PAGEUP":
            self._on_thread_prev_shortcut(None)
        elif message == "F5":
            self._on_refresh_shortcut(None)
        elif message == "SHIFT_F5":
            self._on_refresh_all_shortcut(None)
        elif message == "F6":
            # The way out of the WebView's keyboard trap: hands focus
            # back to the Mail tab's panes. main_frame._on_cycle_panes
            # switches to the Mail tab itself when focus is not
            # already on one of them, which is always the case coming
            # from here.
            self.main_frame._on_cycle_panes(None)
        elif message == "CTRL_S":
            self._on_save_as_shortcut(None)
        elif message == "CTRL_P":
            self._on_print_shortcut(None)
        elif message == "CTRL_SHIFT_P":
            self._on_print_preview_shortcut(None)
        elif message == "CTRL_SHIFT_I":
            self.toggle_remote_content()
        elif message == "CTRL_U":
            self.main_frame._announce_sender(copy_address=False)
        elif message == "CTRL_SHIFT_U":
            self.main_frame._announce_sender(copy_address=True)
        elif message == "CTRL_TAB":
            self.main_frame._next_tab()
        elif message == "CTRL_SHIFT_TAB":
            self.main_frame._previous_tab()
        elif message == "DELETE":
            self.main_frame._run_delete(
                self.account, self.folder, self.envelope, False,
                after_success=self.close_tab,
            )
        elif message == "SHIFT_DELETE":
            self.main_frame._run_delete(
                self.account, self.folder, self.envelope, True,
                after_success=self.close_tab,
            )
        elif message == "CTRL_SHIFT_F":
            # Search Messages (Ctrl+Shift+F) is a menu accelerator on
            # the frame, which never sees this keystroke while the
            # WebView has native OS keyboard focus -- same class of
            # bug as every other key in this bridge (see
            # _HOTKEY_BRIDGE_JS's own docstring). Confirmed by a user
            # report: opening a search result focuses its WebView,
            # and from then on neither the Ctrl+Shift+F hotkey nor
            # keyboard menu access reached the frame at all until the
            # message tab was closed.
            self.main_frame._on_search(None)
        elif message == "CTRL_SHIFT_Q":
            # Quit. Deliberately the same handler wx.ID_EXIT is bound
            # to, so File > Exit, the tray's Quit item and this hotkey
            # are one path: _on_exit sets _quitting and falls through
            # to _on_close's real teardown.
            self.main_frame._on_exit(None)
        elif message == "ALT_A":
            # Archive the message currently open in THIS tab -- see
            # _HOTKEY_BRIDGE_JS for why Alt+A rather than bare A.
            # Closes the tab afterwards, same as Delete/Shift+Delete
            # above, since the message just left this folder.
            self.main_frame._run_archive(
                self.account, self.folder, self.envelope,
                after_success=self.close_tab,
            )

    # --- Message actions ----------------------------------------------

    def _on_thread_next_shortcut(self, event):
        logger.debug("Thread flip: next (Ctrl+Shift+Page Down).")
        self.main_frame.open_thread_neighbour(self, 1)

    def _on_thread_prev_shortcut(self, event):
        logger.debug("Thread flip: previous (Ctrl+Shift+Page Up).")
        self.main_frame.open_thread_neighbour(self, -1)

    def _on_close_and_return(self, event):
        self.close_tab()

    def close_tab(self):
        """
        Closes this message tab, handing off to the frame so that
        every close route -- this one, the Close button, the WebView
        hotkey bridge, Delete's after-success, and the frame's own
        Escape/Ctrl+W/Ctrl+F4 -- lands the reader in the same place.
        See main_frame._close_tab_at: it returns focus to the message
        list when the notebook lands back on the Mail tab, and to
        whatever tab it actually lands on otherwise (a search tab the
        message was opened from, for instance).
        """
        index = self.main_frame.notebook.FindPage(self)
        if index == wx.NOT_FOUND:
            return
        self.main_frame._close_tab_at(index)

    # --- Attachments ---------------------------------------------------

    def _make_save_handler(self, filename):
        def handler(event):
            self._on_save_attachment(filename)
        return handler

    def _on_save_attachment(self, filename):
        with wx.DirDialog(
            self,
            lang.t(
                "dialogs", "mv_choose_folder",
                default="Choose a folder to save the attachment",
            ),
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            dest_dir = dialog.GetPath()

        message_id = self.envelope.get("id")
        self.main_frame.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "downloading_attachment",
            default=f"Downloading {filename}...", filename=filename,
        ))

        def work():
            himalaya_client.download_attachments(
                self.main_frame.paths, self.account, message_id, dest_dir, folder=self.folder
            )

        def on_success(_result):
            self.main_frame.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "saved_to",
                default=f"Saved to {dest_dir}", folder=dest_dir,
            ))
            wx.MessageBox(
                lang.t(
                    "dialogs", "attachment_saved",
                    default=f"Attachment saved to:\n{dest_dir}", path=dest_dir,
                ),
                lang.t("dialogs", "title_attachment_saved", default="Attachment Saved"),
                wx.OK | wx.ICON_INFORMATION,
            )

        def on_error(exc):
            self.main_frame.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "attachment_download_failed",
                default="Attachment download failed.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "attachment_save_failed",
                    default=f"Could not save attachment.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_download_failed", default="Download Failed"),
                wx.OK | wx.ICON_ERROR,
            )

        self.main_frame.lanes.for_account(self.account.account_id).submit(
            work, on_success, on_error,
        )

    def _make_save_all_handler(self, attachment_indexes):
        def handler(event):
            self._on_save_all_attachments(attachment_indexes)
        return handler

    def _on_save_all_attachments(self, attachment_indexes):
        """Saves every attachment of this message straight to the
        configured folder (Settings > Attachments saving folder), no
        prompt. Himalaya's "attachment download" has no per-file
        selector -- it always writes every attachment of a message in
        one call -- so this is the same download _on_save_attachment
        uses, just pointed at a fixed destination instead of asking
        each time."""
        dest_dir = self.main_frame.settings_manager.settings.resolved_attachment_save_dir()
        message_id = self.envelope.get("id")
        count = len(attachment_indexes)
        self.main_frame.GetStatusBar().SetStatusText(
            lang.t(
                "main_ui", "downloading_attachments_one",
                default="Downloading 1 attachment...",
            )
            if count == 1
            else lang.t(
                "main_ui", "downloading_attachments_many",
                default=f"Downloading {count} attachments...", count=count,
            )
        )

        def work():
            os.makedirs(dest_dir, exist_ok=True)
            himalaya_client.download_attachments(
                self.main_frame.paths, self.account, message_id, dest_dir, folder=self.folder
            )

        def on_success(_result):
            self.main_frame.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "saved_to",
                default=f"Saved to {dest_dir}", folder=dest_dir,
            ))
            wx.MessageBox(
                lang.t(
                    "dialogs", "attachments_saved",
                    default=f"{count} attachments saved to:\n{dest_dir}",
                    count=count, path=dest_dir,
                ),
                lang.t(
                    "dialogs", "title_attachments_saved",
                    default="Attachments Saved",
                ),
                wx.OK | wx.ICON_INFORMATION,
            )

        def on_error(exc):
            self.main_frame.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "attachment_download_failed",
                default="Attachment download failed.",
            ))
            wx.MessageBox(
                lang.t(
                    "errors", "attachments_save_failed",
                    default=f"Could not save attachments.\n\n{exc}", error=exc,
                ),
                lang.t("dialogs", "title_download_failed", default="Download Failed"),
                wx.OK | wx.ICON_ERROR,
            )

        self.main_frame.lanes.for_account(self.account.account_id).submit(
            work, on_success, on_error,
        )


_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/octet-stream": ".bin",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "video/x-msvideo": ".avi",
}

# Format column labels for the attachments list, for the same MIME
# types _MIME_EXTENSIONS covers. A type not listed here falls back to
# showing the raw MIME string instead (see _attachment_format_label).
_MIME_FORMAT_LABELS = {
    "application/pdf": "PDF",
    "application/zip": "ZIP archive",
    "application/msword": "Word document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
    "application/vnd.ms-excel": "Excel spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel spreadsheet",
    "application/vnd.ms-powerpoint": "PowerPoint presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint presentation",
    "text/plain": "Text file",
    "text/html": "HTML file",
    "text/csv": "CSV file",
    "application/json": "JSON file",
    "application/xml": "XML file",
    "application/octet-stream": "Binary file",
    "image/jpeg": "JPEG image",
    "image/png": "PNG image",
    "image/gif": "GIF image",
    "image/bmp": "BMP image",
    "image/svg+xml": "SVG image",
    "image/webp": "WebP image",
    "audio/mpeg": "MP3 audio",
    "audio/wav": "WAV audio",
    "audio/ogg": "OGG audio",
    "video/mp4": "MP4 video",
    "video/x-msvideo": "AVI video",
}


def _attachment_filename(parts, index):
    """
    Returns a readable label for an attachment that exposes the file
    type, e.g. "Save: report.pdf" instead of "Save: attachment_4".
    The real filename is looked up first; when it is missing or has
    no extension, the extension is derived from the part's MIME
    content type (application/pdf -> .pdf, audio/mpeg -> .mp3, ...).
    """
    if not (0 <= index < len(parts)):
        return f"attachment_{index}"
    part = parts[index]

    filename = _raw_attachment_filename(part)
    extension = _mime_extension(part)

    if filename:
        _root, existing_ext = os.path.splitext(filename)
        if existing_ext:
            return filename
        if extension:
            return filename + extension
        return filename

    if extension:
        return f"attachment_{index}{extension}"
    return f"attachment_{index}"


def _raw_attachment_filename(part):
    """Best-effort filename lookup against the fields/headers Himalaya
    exposes on a part; returns None when nothing is found."""
    for key in ("filename", "name"):
        value = part.get(key)
        if value:
            return _clean_filename(value)
    headers = part.get("headers") or []
    for header in headers:
        name = header.get("name")
        value = header.get("value")
        if not isinstance(value, dict):
            continue
        if name == "content_disposition":
            # {"Attachment": {"filename": "song.mp3", ...}}
            for sub in value.values():
                if isinstance(sub, dict):
                    filename = sub.get("filename") or sub.get("name")
                    if filename:
                        return _clean_filename(filename)
        elif name == "content_type":
            # Attachment filenames often only live in the Content-Type
            # name parameter, e.g.
            # {"ContentType": {"c_type": "audio", "c_subtype": "mpeg",
            #   "attributes": [{"name": "name", "value": "song.mp3"}]}}
            content_type = value.get("ContentType")
            if isinstance(content_type, dict):
                for attribute in content_type.get("attributes") or []:
                    if isinstance(attribute, dict) and attribute.get("name") == "name":
                        filename = attribute.get("value")
                        if filename:
                            return _clean_filename(filename)
    return None


_UNSAFE_FILENAME_CHARS = '<>:"/\\|?*'
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % n for n in range(1, 10)]
    + ["LPT%d" % n for n in range(1, 10)]
)


def _clean_filename(filename):
    """Strips RFC2231-style quoting, then reduces the result to a bare
    filename that cannot address anywhere but itself.

    The name comes from the message, which means it comes from whoever
    sent it. A Content-Disposition filename of "..\\..\\startup.exe"
    is a perfectly legal thing for a hostile sender to write.

    ZBox does not itself join this onto a directory today -- the
    actual attachment write is done by Himalaya's own
    "attachment download --dir", so what it does with a hostile name
    is Himalaya's behaviour and not reachable from here. This is
    hardened anyway for two reasons: the name is announced to a screen
    reader and displayed on a button, where a path is at best
    confusing and at worst misleading about what is being saved; and
    the first person to build a save path out of it should inherit
    something safe rather than a trap.
    """
    filename = str(filename or "").strip()
    if len(filename) >= 2 and filename[0] == filename[-1] and filename[0] in ('"', "'"):
        filename = filename[1:-1].strip()

    # Drive letters and UNC prefixes first: basename alone would keep
    # "C:evil.txt", which is a relative path on drive C, not a name.
    filename = filename.replace("\\", "/")
    if len(filename) >= 2 and filename[1] == ":":
        filename = filename[2:]
    filename = filename.rsplit("/", 1)[-1]

    filename = "".join(
        "_" if (char in _UNSAFE_FILENAME_CHARS or ord(char) < 32) else char
        for char in filename
    )
    # A name that is only dots addresses a directory, not a file.
    filename = filename.strip().strip(".").strip()

    stem = filename.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        filename = "_" + filename

    return filename or None


def _attachment_mime_type(part):
    """Returns the part's MIME type as "type/subtype", lowercase, or
    None -- matching Himalaya's parsed shape, e.g. {"ContentType":
    {"c_type": "audio", "c_subtype": "mpeg"}}. Shared by
    _mime_extension (the file-extension guess) and
    _attachment_format_label (the Format column in the attachments
    list)."""
    headers = part.get("headers") or []
    for header in headers:
        if header.get("name") != "content_type":
            continue
        value = header.get("value")
        if not isinstance(value, dict):
            continue
        content_type = value.get("ContentType")
        if isinstance(content_type, dict):
            c_type = content_type.get("c_type")
            c_subtype = content_type.get("c_subtype")
            if c_type and c_subtype:
                return f"{c_type}/{c_subtype}".lower()
        # Some builds expose the full type string directly.
        for key in ("mime", "media_type", "type"):
            mime = value.get(key)
            if isinstance(mime, str) and mime.strip():
                return mime.strip().lower()
    return None


def _mime_extension(part):
    """Derives a file extension from the part's MIME type."""
    mime = _attachment_mime_type(part)
    return _MIME_EXTENSIONS.get(mime) if mime else None


def _attachment_format_label(part):
    """Human-readable Format column value for the attachments list,
    e.g. "PDF", "JPEG image", "MP3 audio". Falls back to the raw MIME
    type when it isn't one of the common ones below, and to
    "Unknown" when the part carries no usable Content-Type at all."""
    mime = _attachment_mime_type(part)
    if not mime:
        return lang.t('dialogs', 'mv_format_unknown', default="Unknown")
    label = _MIME_FORMAT_LABELS.get(mime)
    if label is None:
        return mime
    key = "mime_" + re.sub(r"[^a-z0-9]+", "_", mime.lower()).strip("_")
    return lang.t("dialogs", key, default=label)


def _attachment_size_bytes(part):
    """The attachment's decoded size in bytes, read directly from the
    part's own body -- ZBox already has the full content in memory to
    render it, so this counts it rather than asking Himalaya for a
    size it doesn't report. "Binary" bodies are a list of byte values;
    "Text" bodies are counted as UTF-8, which is what Save/Save All
    write to disk. Returns None when neither shape is present.

    A part fetched over the pooled IMAP connection
    (imap_body_fetch.build_message_dict) carries an explicit "size"
    instead of a decoded payload -- that path deliberately does not
    hold attachment bytes in memory, since Save and Save All go
    through Himalaya's own 'attachment download' rather than this
    dict."""
    size = part.get("size")
    if isinstance(size, int) and not isinstance(size, bool):
        return size
    body = part.get("body")
    if not isinstance(body, dict):
        return None
    binary = body.get("Binary")
    if isinstance(binary, list):
        return len(binary)
    text = body.get("Text")
    if isinstance(text, str):
        return len(text.encode("utf-8", errors="replace"))
    return None


def _format_attachment_size(num_bytes):
    """Human-readable Size column value, e.g. "482 KB". "Unknown
    size" when _attachment_size_bytes couldn't read one."""
    if num_bytes is None:
        return lang.t('dialogs', 'mv_size_unknown', default="Unknown size")
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    return f"{mb:.1f} MB"


# --- Opening a saved .eml file (File > Open > Saved Message...) -------

def read_and_format_eml_file(path):
    """
    Parses a .eml file from disk with Python's stdlib email module
    (there is no live account or folder behind a file on disk, so
    this never goes through Himalaya) and renders it as the same
    kind of plain, accessible text ZBox's Text view shows for synced
    messages: a few key headers, then the body. Reuses the existing
    html_to_text/clean_body_text helpers so HTML-only saved messages
    are cleaned up the same way live HTML mail already is.
    Returns (text, subject). Raises OSError if the file can't be
    read, or another exception if it can't be parsed as an email.
    """
    with open(path, "rb") as handle:
        msg = BytesParser(policy=email_policy.default).parse(handle)

    subject = str(msg.get("subject", "") or "")
    header_lines = []
    for name in ("From", "To", "Cc", "Date", "Subject"):
        value = msg.get(name)
        if value:
            header_lines.append(f"{name}: {value}")

    body = ""
    text_part = msg.get_body(preferencelist=("plain",))
    if text_part is not None:
        body = clean_body_text(text_part.get_content())
    else:
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            body = clean_body_text(html_to_text(html_part.get_content()))

    text = "\n".join(header_lines) + "\n\n" + (body or "(no message body)")
    return text, subject


class SavedMessageViewPanel(wx.Panel):
    """
    Read-only display for a message opened from a local .eml file
    (File > Open > Saved Message...) -- deliberately a plain
    wx.TextCtrl, the same accessible pattern as the Text view and the
    inline reader pane, rather than reusing MessageViewPanel, since
    there is no live account/folder/envelope behind a file on disk
    for MessageViewPanel's reply/forward/flag machinery to act on.
    Closes the same way every other tab does (Ctrl+W, Ctrl+F4,
    Escape), via the frame-level accelerator table plus a local
    Escape entry for when this panel's own text control has focus.
    """

    def __init__(self, parent, main_frame, text, title="Saved Message"):
        super().__init__(parent)
        self.main_frame = main_frame
        self._text = text
        self._title = title

        self.body = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.body.SetName(
            lang.t("dialogs", "mv_saved_message", default="Saved message")
        )
        make_read_only_viewer(self.body)
        self.body.SetValue(text)
        _apply_reading_font(self.body, main_frame.settings_manager.settings)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.body, 1, wx.EXPAND)
        self.SetSizer(sizer)

        close_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self._on_close_and_return, id=close_id)
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, close_id),
        ]))

        wx.CallAfter(self.body.SetFocus)

    def _on_close_and_return(self, event):
        self.close_tab()

    def apply_reading_settings(self):
        """Same as MessageViewPanel.apply_reading_settings (audit
        finding 53) -- a saved .eml file is still a message someone
        is reading. No WebView here: SavedMessageViewPanel only ever
        shows the plain-text rendering."""
        _apply_reading_font(self.body, self.main_frame.settings_manager.settings)

    def close_tab(self):
        """Same hand-off as MessageViewPanel.close_tab above -- one
        close path, one focus outcome."""
        index = self.main_frame.notebook.FindPage(self)
        if index == wx.NOT_FOUND:
            return
        self.main_frame._close_tab_at(index)

    def printable_text(self):
        return self._text

    def printable_title(self):
        return self._title


# --- Printing (File > Print...) ----------------------------------------

class _PlainTextPrintout(wx.Printout):
    """
    Paginates a plain-text string across pages for wx's print
    framework. Deliberately plain text only, matching what ZBox's
    Text view already shows -- printing the rendered HTML layout of
    the WebView mode is out of scope, and plain text is what a
    screen-reader user actually wants from a printout anyway.
    """

    MARGIN = 15  # millimetres

    def __init__(self, text, title):
        super().__init__(title)
        self._lines = text.splitlines() or [""]
        self._lines_per_page = 1
        self._page_count = 1

    def _print_font(self, dc):
        """Builds the printout's font sized in PIXELS against the DC's own
        resolution, not in points. wx sizes a point-based font against
        screen resolution, so on a 600 dpi printer DC an 11-point font
        came out 18 device pixels tall -- 0.76 mm on paper, and 350
        lines to an A4 page. Pixel sizing makes 11 points mean 11
        points on whatever DC is actually being drawn to, printer or
        preview."""
        ppi_y = 0
        try:
            ppi_y = dc.GetPPI()[1]
        except Exception:
            ppi_y = 0
        if not ppi_y:
            return wx.Font(11, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                           wx.FONTWEIGHT_NORMAL, faceName="Consolas")
        pixel_height = max(1, round(11 / 72 * ppi_y))
        return wx.Font(wx.Size(0, pixel_height), wx.FONTFAMILY_TELETYPE,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
                       faceName="Consolas")

    def OnPreparePrinting(self):
        dc = self.GetDC()
        dc.SetFont(self._print_font(dc))
        page_w, page_h = self.GetPageSizeMM()
        dc_w, dc_h = dc.GetSize()
        mm_to_dc_y = dc_h / page_h if page_h else 1
        margin_px = self.MARGIN * mm_to_dc_y
        _, line_height = dc.GetTextExtent("Xg")
        usable_height = max(dc_h - 2 * margin_px, line_height)
        self._lines_per_page = max(1, int(usable_height // line_height))
        self._page_count = max(1, -(-len(self._lines) // self._lines_per_page))  # ceil div

    def HasPage(self, page):
        return 1 <= page <= self._page_count

    def GetPageInfo(self):
        return (1, self._page_count, 1, self._page_count)

    def OnPrintPage(self, page):
        dc = self.GetDC()
        dc.SetFont(self._print_font(dc))
        page_w, page_h = self.GetPageSizeMM()
        dc_w, dc_h = dc.GetSize()
        mm_to_dc_x = dc_w / page_w if page_w else 1
        mm_to_dc_y = dc_h / page_h if page_h else 1
        margin_x = self.MARGIN * mm_to_dc_x
        margin_y = self.MARGIN * mm_to_dc_y
        _, line_height = dc.GetTextExtent("Xg")

        start = (page - 1) * self._lines_per_page
        end = min(start + self._lines_per_page, len(self._lines))
        y = int(margin_y)
        x = int(margin_x)
        for line in self._lines[start:end]:
            dc.DrawText(line, x, y)
            y += int(line_height)
        return True


def print_text(parent, text, title):
    """Runs the standard wx print dialog/flow for a plain-text
    string. Errors (no printer configured, user cancels) are handled
    by wx's own dialog; this never raises."""
    printout = _PlainTextPrintout(text, title or "Message")
    printer = wx.Printer()
    if not printer.Print(parent, printout, prompt=True):
        error = printer.GetLastError()
        if error != wx.PRINTER_CANCELLED:
            wx.MessageBox(
                lang.t(
                    "errors", "print_failed",
                    default="Could not print the message. Check that a printer is configured.",
                ),
                lang.t("dialogs", "title_print", default="Print"),
                wx.OK | wx.ICON_ERROR,
            )
    printout.Destroy()


def print_preview(parent, text, title):
    """Runs File > Print Preview...: wx's on-screen preview frame,
    over the same paginated content print_text sends straight to the
    OS print dialog (audit finding 51 -- there was previously no way
    to check page breaks/margins before committing to paper, or for
    a screen reader user to at least confirm the page count without
    printing a throwaway page first).

    wx.PrintPreview needs two SEPARATE _PlainTextPrintout instances --
    one it renders into the preview window, a second it hands to the
    OS print dialog if Print is clicked from inside the preview frame
    -- reusing a single Printout for both is a documented wx footgun
    (the first pass consumes that object's own DC/pagination state).
    Errors (no printer configured to preview against) are handled by
    wx's own dialog; this never raises."""
    preview_printout = _PlainTextPrintout(text, title or "Message")
    printer_printout = _PlainTextPrintout(text, title or "Message")
    preview = wx.PrintPreview(preview_printout, printer_printout)
    if not preview.IsOk():
        wx.MessageBox(
            lang.t(
                "errors", "print_preview_failed",
                default="Could not open print preview. Check that a printer is configured.",
            ),
            lang.t("dialogs", "title_print_preview", default="Print Preview"),
            wx.OK | wx.ICON_ERROR,
        )
        return
    preview_frame = wx.PreviewFrame(
        preview, parent, f"Print Preview - {title or 'Message'}"
    )
    preview_frame.Initialize()
    preview_frame.SetSize(parent.GetSize() if parent else wx.Size(800, 600))
    preview_frame.Show(True)
