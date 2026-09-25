"""
Shared message body extraction, used by both the inline reader pane
(main_frame.py's ReaderPanel) and the dedicated message tab
(message_view_panel.py), so there is exactly one implementation of
this logic rather than two copies that could drift apart.

Confirmed via a full untruncated capture of a real 'message read'
response: parts is a flat array; text_body/html_body are lists of
INDEXES into parts, not text. Each part's own 'body' field is a
tagged union: {"Text": "..."} for plain text, {"Html": "..."} for
HTML, {"Multipart": [child indexes]} for a container part with no
text of its own.
"""

import html as html_module
import re

_SUBJECT_PREFIX_RE = re.compile(r"^(re|fwd?|aw)\s*:\s*", re.IGNORECASE)


def extract_message_body(message):
    """
    Plain text preferred (simplest and most reliable for a screen
    reader); HTML is stripped down to plain text as a fallback when
    no text part exists. Returns None if no body could be found.
    """
    if not isinstance(message, dict):
        return None

    parts = message.get("parts") or []

    def part_text(indexes, tag):
        collected = []
        for index in indexes:
            if 0 <= index < len(parts):
                body = parts[index].get("body")
                if isinstance(body, dict) and tag in body:
                    collected.append(body[tag])
        return collected

    text_parts = part_text(message.get("text_body") or [], "Text")
    if text_parts:
        return clean_body_text("\n\n".join(text_parts))

    html_parts = part_text(message.get("html_body") or [], "Html")
    if html_parts:
        return clean_body_text(html_to_text("\n\n".join(html_parts)))

    return None


def extract_message_html(message):
    """
    Returns the original HTML body for the rendered WebView mode.
    HTML parts are preferred; when a message only has a plain-text
    part it is wrapped in a <pre> block so the rendered view still
    shows something readable. Returns None when no body exists at
    all.
    """
    if not isinstance(message, dict):
        return None

    parts = message.get("parts") or []

    def part_text(indexes, tag):
        collected = []
        for index in indexes:
            if 0 <= index < len(parts):
                body = parts[index].get("body")
                if isinstance(body, dict) and tag in body:
                    collected.append(body[tag])
        return collected

    html_parts = part_text(message.get("html_body") or [], "Html")
    if html_parts:
        return "\n".join(html_parts)

    text_parts = part_text(message.get("text_body") or [], "Text")
    if text_parts:
        return "<pre>" + html_module.escape("\n\n".join(text_parts)) + "</pre>"

    return None


def clean_body_text(text):
    """
    Screen-reader cleanup applied to every displayed message body.
    Strips leading/trailing whitespace (indentation) from each line
    and drops empty lines entirely, so NVDA/JAWS don't announce
    repeated "blank" as the user arrows through a message. HTML mail
    converted to plain text is the worst offender (layout indentation
    and blank lines); plain-text mail benefits too.
    """
    cleaned = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def html_to_text(html_content):
    """Crude but dependency-free HTML-to-text: drop what is never
    text (style and script blocks, the head, comments), strip tags,
    unescape entities, collapse excess whitespace. No external
    dependency, used everywhere HTML needs to become plain,
    screen-reader-safe text rather than being rendered by any
    browser-engine control. Without the first step a newsletter's
    whole style sheet was read out ahead of its message."""
    text = re.sub(r"(?is)<!--.*?-->", " ", html_content)
    text = re.sub(r"(?is)<(style|script|head|title)\b[^>]*>.*?</\1\s*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- Header access ----------------------------------------------------
#
# Himalaya parses headers into parts[i]["headers"], a list of
# {"name": ..., "value": ...}. Recognised header names arrive as
# snake_case strings ("message_id", "references", "reply_to");
# anything it does not know arrives as {"other": "X-Whatever"}.
# Values are a tagged union whose tag depends on the header's type:
# {"Text": "..."} for free text, {"TextList": [...]} for a list of
# tokens like References, {"Address": {"List": [...]}} for address
# headers.
#
# One trap worth stating plainly: an address inside a *message
# header* uses the key "address", while an address inside a
# *envelope* (from list_envelopes) uses "email". They are different
# shapes from the same server. Both are accepted below.


def _header_entries(message):
    if not isinstance(message, dict):
        return []
    entries = []
    for part in message.get("parts") or []:
        if isinstance(part, dict):
            entries.extend(part.get("headers") or [])
    return entries


def _header_name_matches(header, name):
    raw = header.get("name")
    if isinstance(raw, str):
        return raw.lower() == name
    if isinstance(raw, dict):
        other = raw.get("other")
        if isinstance(other, str):
            return other.lower().replace("-", "_") == name
    return False


def header_value(message, name):
    """Raw tagged value of the first header with this name, or None.
    Name is snake_case, as Himalaya reports it."""
    name = name.lower()
    for header in _header_entries(message):
        if _header_name_matches(header, name):
            return header.get("value")
    return None


def header_text(message, name):
    """Header as a plain string, joining list-valued headers with
    spaces (which is how References is written on the wire). Empty
    string when the header is absent or is a shape this does not
    understand -- callers treat a missing header as "no threading
    information", never as an error."""
    value = header_value(message, name)
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    text = value.get("Text")
    if isinstance(text, str):
        return text.strip()
    for key in ("TextList", "List", "Ids", "MessageIds"):
        items = value.get(key)
        if isinstance(items, list):
            parts = [item.strip() for item in items if isinstance(item, str)]
            if parts:
                return " ".join(parts)
    return ""


def header_addresses(message, name):
    """[(display name, address)] for an address header, in order."""
    value = header_value(message, name)
    if not isinstance(value, dict):
        return []
    address_block = value.get("Address")
    if not isinstance(address_block, dict):
        return []

    collected = []

    def take(entries):
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            # "address" in message headers, "email" in envelopes.
            email = entry.get("address") or entry.get("email")
            if email:
                collected.append((entry.get("name") or "", str(email).strip()))
            take(entry.get("addresses"))

    take(address_block.get("List"))
    take(address_block.get("Group"))
    return collected


def angle_wrap(message_id):
    """RFC 5322 wants Message-ID and In-Reply-To in angle brackets.
    Himalaya hands them over without."""
    value = (message_id or "").strip()
    if not value:
        return ""
    if value.startswith("<") and value.endswith(">"):
        return value
    return "<%s>" % value.strip("<>")


def normalized_subject(subject):
    """
    Strips leading Re:/Fwd:/Fw:/Aw: reply-or-forward prefixes
    (repeated, case-insensitive -- "Re: Re: Fwd: Q3 report" reduces
    the same as "Q3 report") and surrounding whitespace, lower-cased.

    Used only as a cheap pre-filter for candidate messages before a
    real relatedness check against thread_ids() below (see audit
    finding 44 / related_messages_panel.py) -- never on its own to
    decide two messages are related, since an unrelated message can
    coincidentally share a subject line. A message whose subject was
    deliberately changed partway through a thread will be missed by
    this pre-filter; that is a known, accepted limitation of the
    "at minimum" version of finding 44, not an oversight.
    """
    text = (subject or "").strip()
    changed = True
    while changed:
        changed = False
        stripped = _SUBJECT_PREFIX_RE.sub("", text, count=1)
        if stripped != text:
            text = stripped.strip()
            changed = True
    return text.lower()


def thread_ids(message):
    """
    Every Message-ID this message is directly connected to by RFC
    5322 threading headers: its own Message-ID, every id in its own
    References chain, and its In-Reply-To (usually already the last
    References entry -- included again in case a message sends one
    without the other, which happens).

    Two messages belong to the same conversation iff
    thread_ids(a) & thread_ids(b) is non-empty: a shared id means one
    is the other's direct parent, direct child, or they share a
    common ancestor -- one intersection covers all three cases,
    including a thread's own root (which has no References of its
    own, but is still included via its own Message-ID, so a reply to
    it always intersects). A message with no threading headers at
    all (no Message-ID, no References, no In-Reply-To -- true of some
    very old or malformed mail) returns an empty set, which never
    intersects anything, including itself; callers treat that as "no
    threading information to search on" rather than a false match.
    """
    ids = set()
    own_id = angle_wrap(header_text(message, "message_id"))
    if own_id:
        ids.add(own_id)
    for token in header_text(message, "references").split():
        token = token.strip()
        if token:
            ids.add(token)
    in_reply_to = angle_wrap(header_text(message, "in_reply_to"))
    if in_reply_to:
        ids.add(in_reply_to)
    return ids


def threading_headers(message):
    """(in_reply_to, references) for a reply to this message.

    References is the original's References plus its Message-ID, per
    RFC 5322: that chain is what Gmail, Outlook and Thunderbird use
    to group a conversation. Without it every reply ZBox sends starts
    a new thread in the recipient's client.
    """
    message_id = angle_wrap(header_text(message, "message_id"))
    existing = header_text(message, "references")
    chain = [token for token in existing.split() if token.strip()]
    if message_id and message_id not in chain:
        chain.append(message_id)
    # Long chains are trimmed by convention; keep the first (thread
    # root) and the most recent, which is what mail clients rely on.
    if len(chain) > 20:
        chain = chain[:1] + chain[-19:]
    return message_id, " ".join(chain)
