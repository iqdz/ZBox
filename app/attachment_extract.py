"""
One attachment out of a message's raw source, and the message's full
header list, for the attachment Open and Save actions and View > Full
Headers.

Why the raw source. Himalaya's "attachment download" has no way to
pick one attachment: it always writes every attachment of a message.
himalaya_client.read_message_raw already fetches the message exactly
as it was sent, and Python's own email package can take any single
part out of that. Pure, no wx, no network -- the caller fetches the
raw text on a worker and hands it in.
"""

import email
import os
import re
from email import policy
from email.parser import BytesHeaderParser

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % n for n in range(1, 10)} | {
    "LPT%d" % n for n in range(1, 10)
}


def _as_bytes(raw):
    if isinstance(raw, bytes):
        return raw
    return str(raw or "").encode("utf-8", "surrogateescape")


def _is_attachment(part):
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    try:
        filename = part.get_filename()
    except Exception:  # noqa: BLE001 - a broken header is not an attachment
        filename = None
    return bool(filename)


def _walk(part, root):
    """Every leaf part in order. A forwarded message attached as a
    file (message/rfc822) counts as one attachment and is not opened
    up, the same way it is listed as one file."""
    if part is not root and part.get_content_type() == "message/rfc822" and _is_attachment(part):
        yield part
        return
    if part.is_multipart():
        for sub in part.iter_parts():
            yield from _walk(sub, root)
    else:
        yield part


def attachment_parts(raw):
    """[(filename, part), ...] for every attachment, in message order.
    filename is "" when the part names none."""
    message = email.message_from_bytes(_as_bytes(raw), policy=policy.default)
    found = []
    for part in _walk(message, message):
        if part is message or not _is_attachment(part):
            continue
        try:
            name = part.get_filename() or ""
        except Exception:  # noqa: BLE001
            name = ""
        found.append((name, part))
    return found


def _payload(part):
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        if isinstance(inner, list) and inner:
            return inner[0].as_bytes()
        return b""
    data = part.get_payload(decode=True)
    return data if data is not None else b""


def extract_attachment(raw, filename, ordinal):
    """The bytes of one attachment. Found by name first (case is
    ignored), then by its position among the attachments, which is
    how ZBox numbers them when a part has no usable name. Raises
    LookupError when neither finds it."""
    parts = attachment_parts(raw)
    wanted = (filename or "").casefold()
    if wanted:
        for name, part in parts:
            if name.casefold() == wanted:
                return _payload(part)
    if isinstance(ordinal, int) and 0 <= ordinal < len(parts):
        return _payload(parts[ordinal][1])
    raise LookupError("The attachment %s was not found in the message." % (filename or ordinal))


def safe_filename(name, fallback="attachment"):
    """A name Windows can save: no path characters, no reserved device
    names, not empty, not over-long."""
    cleaned = _UNSAFE.sub("_", str(name or "")).strip().strip(".").strip()
    if not cleaned:
        cleaned = fallback
    stem, extension = os.path.splitext(cleaned)
    if stem.upper() in _RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > 180:
        stem, extension = os.path.splitext(cleaned)
        cleaned = stem[: 180 - len(extension)] + extension
    return cleaned


def header_rows(raw):
    """[(name, value), ...] for every header, in the order received,
    values decoded and joined onto one line, so a screen reader reads
    each as one short row."""
    message = BytesHeaderParser(policy=policy.default).parsebytes(_as_bytes(raw))
    rows = []
    for name, raw_value in message.raw_items():
        try:
            value = str(message.policy.header_fetch_parse(name, raw_value))
        except Exception:  # noqa: BLE001 - show it as received rather than drop it
            value = str(raw_value)
        rows.append((name, " ".join(value.split())))
    return rows


def header_line(name, value):
    return "%s: %s" % (name, value)
