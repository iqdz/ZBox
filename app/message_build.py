"""
Message construction, address handling and the checks a send depends
on. Built in session y alongside the composer work; see
docs/handoff_2026-09-17_y.md.

Deliberately free of wx, so the suite proves exactly what a recipient
receives without opening a window: single part text/plain when
nothing is formatted, multipart/alternative when something is, inline
images as cid parts inside the HTML alternative, and attachments with
a guessed content type rather than octet-stream for everything.

Nothing in the app imports this yet. app/compose_panel.py still
builds its own message; reconciling the two is part of wiring the new
composer, not of landing this file.
"""

import mimetypes
import os
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid

import body_html

ADDRESS = re.compile(r"^[^@\s,]+@[^@\s,\.]+(\.[^@\s,\.]+)+$")

ATTACH_WORDS = re.compile(
    r"\b(attach|attached|attaching|attachment|attachments|enclosed)\b", re.I
)

PRIORITY_HEADERS = {
    "high": ("1 (Highest)", "High"),
    "normal": ("3 (Normal)", "Normal"),
    "low": ("5 (Lowest)", "Low"),
}


def split_addresses(field):
    """A To, Cc or Bcc field's text as a list of (name, email)."""
    return [pair for pair in getaddresses([field or ""]) if pair[1] or pair[0]]


def invalid_addresses(field):
    """Every malformed address in one field, all of them, not just
    the first: someone fixing a typo should be told about all three
    typos at once rather than discovering them one send at a time."""
    bad = []
    for name, email in split_addresses(field):
        if not email or not ADDRESS.match(email):
            bad.append(formataddr((name, email)) if name else (email or name))
    return bad


def dedupe_addresses(field):
    """Collapses repeats, case-insensitively on the address, keeping
    the first spelling and the original order."""
    seen = set()
    kept = []
    for name, email in split_addresses(field):
        key = (email or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        kept.append(formataddr((name, email)) if name else email)
    return ", ".join(kept)


def recipient_count(*fields):
    total = 0
    for field in fields:
        total += len(split_addresses(field))
    return total


def mentions_attachment(body):
    return bool(ATTACH_WORDS.search(body or ""))


def read_attachment(path):
    """(filename, data, maintype, subtype) for one path. The content
    type is guessed from the name, with octet-stream as the fallback
    rather than the default, so images and PDFs preview in the
    recipient's client instead of arriving as unknown blobs."""
    with open(path, "rb") as handle:
        data = handle.read()
    filename = os.path.basename(path)
    guessed, _encoding = mimetypes.guess_type(filename)
    if guessed and "/" in guessed:
        maintype, subtype = guessed.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"
    return filename, data, maintype, subtype


def build_message(spec):
    """
    Builds the message. spec keys:
      from_name, from_email, to, cc, bcc, reply_to, subject
      text_body           the plain part, always present
      html_body           fragment or None; None means single part
      attachments         list of (filename, data, maintype, subtype)
      inline_images       list of (cid, filename, data, maintype, subtype)
      priority            high, normal or low
      read_receipt, dsn   bools
      in_reply_to, references, message_id
      format_flowed       bool, plain part only
      sent_copy           recorded as a header for now
    """
    message = EmailMessage()
    message["From"] = formataddr((spec.get("from_name", ""), spec["from_email"]))
    for header, key in (("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"),
                        ("Reply-To", "reply_to")):
        value = (spec.get(key) or "").strip()
        if value:
            message[header] = value
    message["Subject"] = (spec.get("subject") or "").strip()
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = spec.get("message_id") or make_msgid()
    if spec.get("in_reply_to"):
        message["In-Reply-To"] = spec["in_reply_to"]
    if spec.get("references"):
        message["References"] = spec["references"]

    priority = (spec.get("priority") or "normal").lower()
    if priority in PRIORITY_HEADERS and priority != "normal":
        x_priority, importance = PRIORITY_HEADERS[priority]
        message["X-Priority"] = x_priority
        message["Importance"] = importance

    if spec.get("read_receipt"):
        message["Disposition-Notification-To"] = spec["from_email"]
    if spec.get("dsn"):
        # A real DSN is asked for at SMTP time, not in a header. This
        # records the intent; the send path passes it to the command.
        message["X-ZBox-DSN"] = "success,failure,delay"
    if spec.get("sent_copy"):
        message["X-ZBox-Sent-Copy"] = spec["sent_copy"]

    text_body = spec.get("text_body") or ""
    html_body = spec.get("html_body")

    message.set_content(text_body)
    if spec.get("format_flowed"):
        message.set_param("format", "flowed")

    if html_body:
        message.add_alternative(body_html.wrap_document(html_body), subtype="html")
        html_part = message.get_payload()[-1]
        for cid, filename, data, maintype, subtype in spec.get("inline_images", []):
            html_part.add_related(
                data, maintype=maintype, subtype=subtype,
                cid="<%s>" % cid, filename=filename,
            )

    for filename, data, maintype, subtype in spec.get("attachments", []):
        message.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=filename
        )

    return message
