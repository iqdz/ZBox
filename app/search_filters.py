"""
The Search tab's filters that the mail server cannot apply exactly: date
ranges with times, checked here against each result's own date, and the
lists behind the tab's Look in and Date choices. No wx, so tests reach
every part of it.

The server compares whole days only (Himalaya's 'after' condition), in
its own idea of where a day starts, so the Search tab asks it for a
couple of days more on each side (server_dates) and keeps only what
in_range accepts.
"""

import re
from datetime import datetime, timedelta, timezone

from envelope_format import _parse_envelope_date

# The Look in choice, in its order, and the fields each one searches.
LOOK_IN = ("all", "subject", "from", "to", "body")
FIELDS = {
    "all": ("subject", "from", "to", "body"),
    "subject": ("subject",),
    "from": ("from",),
    "to": ("to",),
    "body": ("body",),
}

# The Date choice, in its order.
DATE_RANGES = ("any", "today", "yesterday", "week", "month", "year", "custom")

_WHEN = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]+(\d{1,2}):(\d{2}))?")
_NO_DATE = datetime.min.replace(tzinfo=timezone.utc)


def parse_when(text, end=False):
    """
    One edge of a Custom range, typed as year-month-day with an optional
    time as hours:minutes, as a time in this PC's own time zone. None when
    empty; ValueError when it cannot be read. A range includes its start
    and leaves out its end, so the end is moved just past what was typed:
    a date alone to the start of the next day, a time to the next minute.
    """
    text = (text or "").strip()
    if not text:
        return None
    match = _WHEN.fullmatch(text)
    if match is None:
        raise ValueError(text)
    year, month, day, hour, minute = match.groups()
    try:
        if hour is None:
            value = datetime(int(year), int(month), int(day))
            if end:
                value += timedelta(days=1)
        else:
            value = datetime(int(year), int(month), int(day), int(hour), int(minute))
            if end:
                value += timedelta(minutes=1)
        return value.astimezone()
    except (OverflowError, OSError) as exc:
        raise ValueError(text) from exc


def preset_range(key, now=None):
    """(start, end) for a Date choice other than Custom range, either
    edge None when open. Today, Yesterday and This year start at local
    midnight; Last 7 days and Last 30 days count back from now."""
    now = now if now is not None else datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        return midnight, None
    if key == "yesterday":
        return midnight - timedelta(days=1), midnight
    if key == "week":
        return now - timedelta(days=7), None
    if key == "month":
        return now - timedelta(days=30), None
    if key == "year":
        return midnight.replace(month=1, day=1), None
    return None, None


def server_dates(start, end):
    """(after, not_after) days for the server's own date conditions, or
    None for an open edge: two days before the start and one day after
    the end, so neither the server's time zone nor whether its edges
    count decides a match. in_range then checks the exact times."""
    after = start.date() - timedelta(days=2) if start is not None else None
    not_after = end.date() + timedelta(days=1) if end is not None else None
    return after, not_after


def in_range(envelope, start, end):
    """True when the envelope's date is at or after start and before end.
    With a range chosen, a message whose date cannot be read is left out,
    since it cannot be placed in it."""
    if start is None and end is None:
        return True
    when = _parse_envelope_date(envelope)
    if when == _NO_DATE:
        return False
    if start is not None and when < start:
        return False
    if end is not None and when >= end:
        return False
    return True


def uid_of(envelope):
    """The envelope's id as a number, or None."""
    try:
        return int(str(envelope.get("id")))
    except (TypeError, ValueError):
        return None
