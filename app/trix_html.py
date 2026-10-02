"""
Trix output to email HTML.

Trix writes markup for a browser with its own stylesheet loaded: div
for lines, class names carrying the meaning of quotes, code and
attachments, and trix.css expected to style them. A mail client has
none of that, so sending Trix output raw gives the recipient
unstyled text with stray class attributes in it.

This converts. Every tag is checked against a whitelist, every
attribute is dropped except the few worth keeping, div becomes p, the
block elements get inline styles because that is the only styling
mail clients reliably honour, and Trix's attachment figures are
removed entirely: attachments belong to the message's attachment
list, not to the document.

No wx and no dependencies, so the suite proves it headless.
"""

import html as _html
from html.parser import HTMLParser

# Inline styles, since a style element in the head is stripped by
# most webmail and ignored by several desktop clients.
STYLES = {
    "p": "margin:0 0 1em 0;",
    "h1": "margin:0 0 0.5em 0;font-size:1.4em;font-weight:bold;",
    "blockquote": "margin:0 0 1em 0;padding:0 0 0 1em;"
                  "border-left:3px solid #cccccc;color:#555555;",
    "pre": "margin:0 0 1em 0;padding:0.6em;background:#f4f4f4;"
           "font-family:Consolas,'Courier New',monospace;white-space:pre-wrap;",
    "ul": "margin:0 0 1em 1.6em;padding:0;",
    "ol": "margin:0 0 1em 1.6em;padding:0;",
    "li": "margin:0 0 0.3em 0;",
}

TAG_MAP = {"div": "p"}

ALLOWED = {
    "p", "br", "strong", "b", "em", "i", "del", "s", "u", "a",
    "ul", "ol", "li", "blockquote", "pre", "h1", "h2", "h3", "img", "span",
}

VOID = {"br", "img"}

# Trix wraps an attached file in a figure. The whole subtree goes.
DROP_SUBTREE = {"figure"}

KEEP_ATTRS = {"a": ("href",), "img": ("src", "alt")}

SAFE_URL = ("http://", "https://", "mailto:", "cid:")

DOCUMENT_STYLE = (
    "font-family:'Segoe UI',Arial,sans-serif;font-size:11pt;"
    "line-height:1.4;color:#000000;"
)


class _Converter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.drop_depth = 0
        self.open_tags = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth or tag in DROP_SUBTREE:
            if tag in DROP_SUBTREE:
                self.drop_depth += 1
            return
        mapped = TAG_MAP.get(tag, tag)
        if mapped not in ALLOWED:
            return
        self.parts.append("<%s%s>" % (mapped, self._attrs(mapped, attrs)))
        if mapped not in VOID:
            self.open_tags.append(mapped)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth:
            return
        mapped = TAG_MAP.get(tag, tag)
        if mapped in ALLOWED:
            self.parts.append("<%s%s>" % (mapped, self._attrs(mapped, attrs)))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_SUBTREE and self.drop_depth:
            self.drop_depth -= 1
            return
        if self.drop_depth:
            return
        mapped = TAG_MAP.get(tag, tag)
        if mapped not in ALLOWED or mapped in VOID:
            return
        if mapped in self.open_tags:
            while self.open_tags:
                current = self.open_tags.pop()
                self.parts.append("</%s>" % current)
                if current == mapped:
                    break

    def handle_data(self, data):
        if self.drop_depth:
            return
        self.parts.append(_html.escape(data, quote=False))

    def _attrs(self, tag, attrs):
        keep = KEEP_ATTRS.get(tag, ())
        out = []
        for name, value in attrs:
            name = (name or "").lower()
            if name not in keep:
                continue
            value = value or ""
            if name in ("href", "src") and not value.lower().startswith(SAFE_URL):
                continue
            out.append('%s="%s"' % (name, _html.escape(value, quote=True)))
        style = STYLES.get(tag)
        if style:
            out.append('style="%s"' % style)
        return (" " + " ".join(out)) if out else ""

    def close_all(self):
        while self.open_tags:
            self.parts.append("</%s>" % self.open_tags.pop())
        return "".join(self.parts)


class _TextExtractor(HTMLParser):
    """The text a reader would actually see. Used only to decide
    whether a body is empty, which is why it cares about line breaks
    and ignores everything else."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_SUBTREE:
            self.drop_depth += 1
        elif tag == "br" and not self.drop_depth:
            self.chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br" and not self.drop_depth:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in DROP_SUBTREE and self.drop_depth:
            self.drop_depth -= 1

    def handle_data(self, data):
        if not self.drop_depth:
            self.chunks.append(data)

    def text(self):
        return "".join(self.chunks)


def plain_text(markup):
    extractor = _TextExtractor()
    extractor.feed(markup or "")
    extractor.close()
    return extractor.text()


def to_email_html(trix_output):
    """The fragment a recipient receives, self-contained and styled
    inline. An empty editor gives an empty string rather than an
    empty paragraph, so the caller can tell the difference: Trix
    leaves a div holding a single line break behind once everything
    is deleted, and that is not a message."""
    if not (trix_output or "").strip():
        return ""
    converter = _Converter()
    converter.feed(trix_output)
    converter.close()
    fragment = converter.close_all().strip()
    if not fragment:
        return ""
    # Text, or a surviving image: a picture with no words is still a
    # message worth sending.
    if not plain_text(fragment).strip() and "<img" not in fragment:
        return ""
    return '<div style="%s">%s</div>' % (DOCUMENT_STYLE, fragment)


def has_content(trix_output):
    """Whether anything was actually typed."""
    return bool(to_email_html(trix_output))
