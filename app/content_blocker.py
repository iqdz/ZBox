"""
Strips remote content out of a message's HTML before it ever reaches
the renderer.

Why this exists
---------------
The HTML message view hands the email's own markup to
wx.html2.WebView with SetPage(). Top-level navigation is vetoed
elsewhere (a clicked link opens in the default browser), but
subresource loads are not navigations: every <img>, stylesheet, font
and script in a message used to fetch normally. That is how tracking
pixels work -- a 1x1 image whose URL encodes who you are tells the
sender the moment you opened their mail, and from which IP.

So the filtering happens on the markup, before the engine sees it,
rather than at the network layer. That choice matters here: wxPython
exposes no request interception for either backend, and doing it in
the markup means the same protection applies whether the message is
rendered by WebView2 or by legacy MSHTML.

What counts as remote
---------------------
cid: references (inline attachments that arrived with the message)
and data: URIs are local -- they cost no network request and reveal
nothing -- so they are always kept. Everything else with a host,
including protocol-relative //host/path and bare relative paths
(which resolve against the document and can still escape via <base>),
is remote.

Two modes
---------
block_all_remote=True is the default and matches Thunderbird: nothing
remote loads until the reader asks for it. With it off, only hosts
the blocklist knows about are stripped, which is weaker -- a
first-party pixel on the sender's own domain sails through -- but
leaves ordinary mail looking as sent.

Scripts, frames, objects and event handlers are removed in both
modes, and are not restored by "show remote content". Mail has no
business scripting, and no reader asked for it.
"""

import re
from html.parser import HTMLParser
import lang


# Attributes that make the renderer fetch something, per tag. "*"
# applies to every tag.
URL_ATTRS = {
    "*": ("background", "lowsrc", "dynsrc", "xlink:href"),
    "img": ("src", "srcset", "longdesc"),
    "image": ("src", "href", "xlink:href"),
    "source": ("src", "srcset"),
    "input": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "link": ("href",),
    "table": ("background",),
    "td": ("background",),
    "th": ("background",),
    "tr": ("background",),
    "body": ("background",),
}

# Elements dropped outright, content and all. Not restored by
# "show remote content".
DROP_ELEMENTS = frozenset(
    ("script", "iframe", "frame", "frameset", "embed", "object",
     "applet", "noscript", "base")
)

# Elements whose content is kept but scanned for url() references.
CSS_ELEMENTS = frozenset(("style",))

# Anything that is a URL but costs no request and leaks nothing.
LOCAL_SCHEMES = ("cid:", "data:", "about:blank", "#")

CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]*)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*['\"]?([^)'\"]*)['\"]?\s*\)|['\"]([^'\"]*)['\"])[^;]*;",
    re.IGNORECASE,
)
HOST_RE = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//([^/?#\s]+)", re.IGNORECASE)

# URL shapes that are almost always an open-tracking beacon rather
# than a picture someone meant you to see.
PIXEL_HINTS = (
    "/open", "open.", "/pixel", "pixel.", "/track", "track.", "/beacon",
    "beacon.", "/wf/open", "utm_", "/imp", "/o/", "email_open",
)


def _is_local_url(url):
    value = (url or "").strip()
    if not value:
        return True
    lowered = value.lower()
    for prefix in LOCAL_SCHEMES:
        if lowered.startswith(prefix):
            return True
    # mailto:, tel: and friends never fetch anything.
    if re.match(r"^(mailto|tel|sms|callto|zbox):", lowered):
        return True
    return False


def url_host(url):
    """Host a URL would be fetched from, or "" for relative paths and
    anything without one."""
    match = HOST_RE.match(url or "")
    if not match:
        return ""
    host = match.group(1)
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):          # IPv6 literal
        return host.split("]", 1)[0].lstrip("[").lower()
    return host.split(":", 1)[0].strip().lower()


class BlockReport:
    """What a single filter pass removed, in terms a person can be
    told out loud."""

    def __init__(self):
        self.blocked = 0
        self.trackers = 0
        self.hosts = set()
        self.scripts = 0

    @property
    def any_blocked(self):
        return self.blocked > 0 or self.scripts > 0

    def summary(self):
        """One sentence for the notification bar and the screen
        reader. Deliberately says how many and from where, because
        'content was blocked' tells the reader nothing they can act
        on."""
        if not self.any_blocked:
            return lang.t('actions_announcements', 'remote_none', default="No remote content in this message.")
        parts = []
        if self.blocked:
            item = "item" if self.blocked == 1 else "items"
            parts.append(f"{self.blocked} remote {item} blocked")
        if self.trackers:
            pixel = "tracking pixel" if self.trackers == 1 else "tracking pixels"
            parts.append(f"{self.trackers} {pixel}")
        if self.scripts:
            script = "script" if self.scripts == 1 else "scripts"
            parts.append(f"{self.scripts} {script} removed")
        if self.hosts:
            names = sorted(self.hosts)
            shown = ", ".join(names[:3])
            if len(names) > 3:
                shown += f" and {len(names) - 3} more"
            parts.append(f"from {shown}")
        return "; ".join(parts) + "."


class _RemoteContentFilter(HTMLParser):
    def __init__(self, block_all_remote=True, is_blocked_host=None):
        # convert_charrefs must stay off: with it on, entities are
        # decoded into the data stream and re-emitting that data
        # verbatim would corrupt any literal & or < in the message.
        super().__init__(convert_charrefs=False)
        self.block_all_remote = block_all_remote
        self.is_blocked_host = is_blocked_host or (lambda host: False)
        self.out = []
        self.report = BlockReport()
        self._skip_stack = []
        self._style_depth = 0

    # -- decisions ----------------------------------------------------

    def _should_block(self, url):
        if _is_local_url(url):
            return False
        if self.block_all_remote:
            return True
        host = url_host(url)
        if not host:
            # A relative path with blocking off: the document has no
            # base, so it cannot resolve anywhere. Harmless.
            return False
        return bool(self.is_blocked_host(host))

    def _record(self, url, is_tracker=False):
        self.report.blocked += 1
        if is_tracker:
            self.report.trackers += 1
        host = url_host(url)
        if host:
            self.report.hosts.add(host)

    @staticmethod
    def _looks_like_tracker(tag, attrs, url):
        if tag != "img":
            return False
        lowered = (url or "").lower()
        if any(hint in lowered for hint in PIXEL_HINTS):
            return True
        alt = (attrs.get("alt") or "").strip()
        for name in ("width", "height"):
            value = (attrs.get(name) or "").strip().rstrip("px")
            if value.isdigit() and int(value) <= 3:
                return True
        style = (attrs.get("style") or "").lower()
        if re.search(r"(width|height)\s*:\s*[0-3](\.\d+)?\s*px", style):
            return True
        # No alt text and a query string: nothing to show a reader,
        # but something to tell the sender.
        return not alt and "?" in lowered

    # -- css ----------------------------------------------------------

    def _filter_css(self, css):
        if not css:
            return css

        def replace_url(match):
            url = match.group(2)
            if self._should_block(url):
                self._record(url)
                return "url(about:blank)"
            return match.group(0)

        def replace_import(match):
            url = match.group(1) or match.group(2) or ""
            if self._should_block(url):
                self._record(url)
                return ""
            return match.group(0)

        css = CSS_IMPORT_RE.sub(replace_import, css)
        return CSS_URL_RE.sub(replace_url, css)

    # -- attributes ---------------------------------------------------

    def _filter_attrs(self, tag, attrs):
        """Returns (attrs, drop_element). attrs is a list of
        (name, value) with remote references removed."""
        as_dict = {name.lower(): (value or "") for name, value in attrs}
        url_names = set(URL_ATTRS.get("*", ())) | set(URL_ATTRS.get(tag, ()))
        kept = []
        blocked_here = False

        for name, value in attrs:
            lowered = name.lower()

            # Inline event handlers: gone in every mode.
            if lowered.startswith("on"):
                continue

            if lowered == "style":
                kept.append((name, self._filter_css(value or "")))
                continue

            if lowered == "srcset" and lowered in url_names:
                candidates = [c.strip() for c in (value or "").split(",")]
                urls = [c.split()[0] for c in candidates if c.split()]
                if any(self._should_block(u) for u in urls):
                    for u in urls:
                        if self._should_block(u):
                            self._record(u)
                    blocked_here = True
                    continue
                kept.append((name, value))
                continue

            if lowered in url_names:
                if self._should_block(value):
                    self._record(
                        value, self._looks_like_tracker(tag, as_dict, value)
                    )
                    blocked_here = True
                    continue
                kept.append((name, value))
                continue

            # Forms in mail post somewhere; there is no case for it.
            if lowered in ("action", "formaction") and not _is_local_url(value):
                self._record(value)
                blocked_here = True
                continue

            if (tag == "meta" and lowered == "http-equiv"
                    and (value or "").strip().lower() == "refresh"):
                return kept, True

            kept.append((name, value))

        # An <img> with no src left and nothing to announce is noise
        # in the reading order; one with alt text still says what was
        # there, so it stays.
        if tag == "img" and blocked_here:
            if not (as_dict.get("alt") or "").strip():
                return kept, True
            kept.append(("data-zbox-blocked", "1"))

        # A stylesheet <link> whose href was stripped does nothing.
        if tag == "link" and blocked_here:
            return kept, True

        return kept, False

    @staticmethod
    def _render_tag(tag, attrs, self_closing=False):
        parts = ["<", tag]
        for name, value in attrs:
            if value is None:
                parts.append(" " + name)
            else:
                escaped = value.replace("&", "&amp;").replace('"', "&quot;")
                parts.append(' %s="%s"' % (name, escaped))
        parts.append(" />" if self_closing else ">")
        return "".join(parts)

    # -- parser callbacks ---------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_stack:
            self._skip_stack.append(tag)
            return
        if tag in DROP_ELEMENTS:
            if tag == "script":
                self.report.scripts += 1
            elif tag != "base":
                self.report.blocked += 1
            # <base> and void-ish drops have no end tag to wait for.
            if tag not in ("base",):
                self._skip_stack.append(tag)
            return
        kept, drop = self._filter_attrs(tag, attrs)
        if drop:
            return
        if tag in CSS_ELEMENTS:
            self._style_depth += 1
        self.out.append(self._render_tag(tag, kept))

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_stack:
            return
        if tag in DROP_ELEMENTS:
            if tag == "script":
                self.report.scripts += 1
            elif tag != "base":
                self.report.blocked += 1
            return
        kept, drop = self._filter_attrs(tag, attrs)
        if drop:
            return
        self.out.append(self._render_tag(tag, kept, self_closing=True))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            # Unwind to the matching open tag; malformed mail is the
            # norm, so tolerate stray end tags rather than trusting
            # the nesting.
            if tag in self._skip_stack:
                while self._skip_stack:
                    if self._skip_stack.pop() == tag:
                        break
            return
        if tag in CSS_ELEMENTS and self._style_depth:
            self._style_depth -= 1
        self.out.append("</%s>" % tag)

    def handle_data(self, data):
        if self._skip_stack:
            return
        # CDATA content of <style> arrives here; filter its url()s.
        # Tracked with an explicit depth rather than self.lasttag,
        # which stays set to "style" for the text that follows the
        # closing tag as well.
        if self._style_depth:
            self.out.append(self._filter_css(data))
            return
        self.out.append(data)

    def handle_entityref(self, name):
        if not self._skip_stack:
            self.out.append("&%s;" % name)

    def handle_charref(self, name):
        if not self._skip_stack:
            self.out.append("&#%s;" % name)

    def handle_comment(self, data):
        if not self._skip_stack:
            self.out.append("<!--%s-->" % data)

    def handle_decl(self, decl):
        if not self._skip_stack:
            self.out.append("<!%s>" % decl)

    def handle_pi(self, data):
        # Processing instructions in mail are Word artefacts. Drop.
        return

    def unknown_decl(self, data):
        return

    def result(self):
        return "".join(self.out)


def filter_message_html(html_content, block_all_remote=True,
                        is_blocked_host=None):
    """
    Returns (filtered_html, BlockReport).

    Never raises on malformed input: mail HTML is frequently broken,
    and a message that fails to filter must still be readable. On any
    parse failure the original is returned with an empty report,
    which is the pre-existing behaviour rather than a blank screen.
    """
    if not html_content:
        return html_content, BlockReport()
    parser = _RemoteContentFilter(
        block_all_remote=block_all_remote, is_blocked_host=is_blocked_host
    )
    try:
        parser.feed(html_content)
        parser.close()
    except Exception:
        return html_content, BlockReport()
    return parser.result(), parser.report
