"""
Body conversions: plain text to HTML, HTML to plain text, the markup
engine, and hard wrapping for the text part of a message.

Split out of the compose work in session y (see
docs/handoff_2026-09-17_y.md). No wx anywhere in here on purpose:
every function is a string in, string out, so the suite covers it
without a display, which is the same reason the envelope helpers were
split out of main_frame first.

Nothing in the app imports this yet. It lands ahead of the composer
work so the conversions and their tests exist in the repository
rather than in a scratch folder.
"""

import html as _html
import re

# The only tags any engine here will ever produce or let through.
# Anything else a writer types is escaped and shown literally, which
# is the safe direction: a stray angle bracket becomes text, never a
# tag.
ALLOWED = (
    "b", "strong", "i", "em", "u", "br", "p", "div", "span",
    "ul", "ol", "li", "h1", "h2", "h3", "blockquote", "a", "img",
)

_TAG = re.compile(r"<[^>]+>")
_FORMATTING = re.compile(
    r"<\s*/?\s*(b|strong|i|em|u|ul|ol|li|h1|h2|h3|blockquote|a|img)\b", re.I
)
_TAG_SCAN = re.compile(
    r"""<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>"""
)

SAFE_URL = re.compile(r"^(https?:|mailto:|cid:)", re.I)


def escape(text):
    return _html.escape(text or "", quote=True)


def text_to_html(text):
    """Plain text to an HTML fragment: one paragraph per blank-line
    run, single newlines kept as line breaks."""
    blocks = re.split(r"\n\s*\n", text or "")
    parts = []
    for block in blocks:
        if not block.strip():
            continue
        parts.append("<p>" + escape(block).replace("\n", "<br>") + "</p>")
    return "\n".join(parts)


def markup_to_html(text):
    """
    The markup engine's body to HTML.

    The text is walked tag by tag rather than escaped wholesale and
    unpicked afterwards: an allowed tag is rebuilt carrying only the
    attributes below, anything else is escaped and shown as the
    literal characters it is, and everything between tags is always
    escaped. So a writer gets what they typed for the tags they
    meant, sees the rest as plain words, and has no way to smuggle
    script in by either path.

    The earlier version escaped first and tried to put tags back with
    a regex. It could never match an attribute, because the quotes
    had already become entities, so every link was silently dropped.
    """
    parts = []
    position = 0
    source = text or ""
    for match in _TAG_SCAN.finditer(source):
        parts.append(_escaped_run(source[position:match.start()]))
        closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if name in ALLOWED:
            parts.append(
                "</%s>" % name if closing else "<%s%s>" % (name, _safe_attrs(name, attrs))
            )
        else:
            parts.append(escape(match.group(0)))
        position = match.end()
    parts.append(_escaped_run(source[position:]))
    return "".join(parts)


def _escaped_run(text):
    return escape(text).replace("\n", "<br>\n")


def _safe_attrs(tag, raw):
    """href on a, src and alt on img, nothing else -- attributes are
    where the unpleasant surprises live. An href or src that is not
    http, https, mailto or cid is dropped, which is what turns a
    javascript link into an ordinary word."""
    keep = {"a": ("href",), "img": ("src", "alt")}.get(tag, ())
    if not keep:
        return ""
    out = []
    for name in keep:
        found = re.search(
            r"""%s\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""" % name, raw or "", re.I
        )
        if not found:
            continue
        value = _html.unescape(found.group(1).strip("\"'"))
        if name in ("href", "src") and not SAFE_URL.match(value):
            continue
        out.append('%s="%s"' % (name, escape(value)))
    return (" " + " ".join(out)) if out else ""


def html_to_text(markup):
    """An HTML fragment as the plain alternative that rides alongside
    it. Block tags become line breaks, list items get a dash,
    everything else is stripped and unescaped."""
    text = markup or ""
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|h1|h2|h3|blockquote|ul|ol)\s*>", "\n\n", text)
    text = re.sub(r"(?i)<\s*li\s*>", "\n- ", text)
    text = _TAG.sub("", text)
    text = _html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_formatting(markup):
    """Whether this fragment carries anything a plain text part could
    not express. Decides single part text/plain against
    multipart/alternative at send time."""
    return bool(markup and _FORMATTING.search(markup))


def wrap_document(fragment):
    return (
        "<!DOCTYPE html>\n<html><head>"
        '<meta charset="utf-8">'
        "</head><body>\n%s\n</body></html>" % (fragment or "")
    )


def wrap_lines(text, column):
    """Hard wrap for the plain part. 0 leaves it alone. Quoted lines
    and the signature delimiter are never touched, because rewrapping
    either one changes what it means."""
    if not column or column <= 0:
        return text
    out = []
    for line in (text or "").split("\n"):
        if line.startswith(">") or line == "-- " or len(line) <= column:
            out.append(line)
            continue
        current = ""
        for word in line.split(" "):
            if current and len(current) + 1 + len(word) > column:
                out.append(current)
                current = word
            else:
                current = (current + " " + word).strip()
        out.append(current)
    return "\n".join(out)
