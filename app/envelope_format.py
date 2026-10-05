"""
Pure helpers for reading and formatting Himalaya's envelope and
message shapes.

Split out of main_frame.py, which had grown to 3,682 lines and held
the three panes, every menu, every command handler, all fetch
orchestration and these helpers besides. These went first because
they are the easy half: no wx, no window state, no threads -- given
an envelope dict they return a string or a bool. That also makes
them the only part of the reader that could be unit tested at all,
which tests/test_envelope_format.py now does.

Names keep their leading underscore. They read as private because
they were private to main_frame, and renaming a hundred call sites
in the same commit that moves them would make an already
regression-prone refactor impossible to review.

Two envelope shapes appear here and they are not the same. A list
envelope's addresses use the key "email"; a parsed message header's
use "address". Both are Himalaya 2.1.0, confirmed against live
capture.
"""

import sys
from datetime import datetime, timezone
from email.utils import formataddr
from urllib.parse import unquote, urlsplit

import provider_presets
from message_body import extract_message_body, header_addresses
from settings_manager import FOLDER_TYPES
import lang


_SYSTEMTIME = None


def _system_clock_text(local):
    """
    The time of day as Windows itself writes it for this user:
    GetTimeFormatEx with the user's own locale and no seconds, so the
    12- or 24-hour choice, the separator and AM/PM wording all follow
    Region settings exactly and change the moment the user changes
    them there. 24-hour "HH:MM" when Windows cannot be asked.
    """
    global _SYSTEMTIME
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            if _SYSTEMTIME is None:
                class SYSTEMTIME(ctypes.Structure):
                    _fields_ = [
                        (name, wintypes.WORD) for name in (
                            "wYear", "wMonth", "wDayOfWeek", "wDay",
                            "wHour", "wMinute", "wSecond", "wMilliseconds",
                        )
                    ]

                _SYSTEMTIME = SYSTEMTIME
            stamp = _SYSTEMTIME(local.year, local.month, 0, local.day, local.hour, local.minute, 0, 0)
            buffer = ctypes.create_unicode_buffer(64)
            time_noseconds = 0x00000002
            written = ctypes.windll.kernel32.GetTimeFormatEx(
                None, time_noseconds, ctypes.byref(stamp), None, buffer, len(buffer),
            )
            if written:
                return buffer.value
        except Exception:  # noqa: BLE001 - the fallback below is always valid
            pass
    return "%02d:%02d" % (local.hour, local.minute)


# The clock used for every list row. A module-level name so tests can
# pin it to 24-hour whatever the test machine's Region setting is.
_clock_text = _system_clock_text


def _folder_display_to_himalaya(folder_name, account=None):
    """
    Maps the tree's display labels to the folder names Himalaya/IMAP
    expects. INBOX is universal. Sent/Drafts/Trash/Junk/Archive naming
    varies by provider -- Gmail in particular uses "[Gmail]/..." names
    and has no real "Archive" folder (its archive concept is "All
    Mail") -- so when an account is given this resolves through
    provider_presets.himalaya_folder_name, matching Thunderbird's own
    per-provider special-folder mapping. Falls back to the plain
    Inbox-only mapping when no account is available (unified-folder
    call sites still do this today; see their own notes).
    """
    if account is not None:
        if folder_name == "Trash":
            # A server that keeps its trash under another name (Deleted
            # Items, INBOX.Trash, a \Trash folder), found by
            # himalaya_client.ensure_trash_folder this session.
            remembered = provider_presets.remembered_trash_folder(
                getattr(account, "account_id", None)
            )
            if remembered:
                return remembered
        return provider_presets.himalaya_folder_name(
            account.imap_host, folder_name, getattr(account, "account_id", None)
        )
    return {"Inbox": "INBOX"}.get(folder_name, folder_name)


def _junk_folder_for_account(account):
    """
    Real Himalaya folder name to treat as this account's Junk folder
    for Mark as Junk (audit finding 46). An explicit per-account
    override (Account Settings' own Junk folder field) wins over the
    generic mapping every other special folder already uses, since
    junk/spam folder naming is the least standardized of the six --
    some providers use "Junk", some "Spam", Gmail uses "[Gmail]/Spam",
    and a handful of plain IMAP accounts have no such folder
    provisioned at all until the person names one explicitly. Falls
    back to _folder_display_to_himalaya("Junk", account) -- the same
    provider-aware mapping Archive/Trash/Sent already rely on -- when
    no override is set.
    """
    override = (getattr(account, "junk_folder", "") or "").strip()
    if override:
        return override
    return _folder_display_to_himalaya("Junk", account)


def _is_account_trash(folder, account):
    """True when `folder` is this account's trash folder, whatever the
    server calls it: Trash, Deleted, Deleted Items, [Gmail]/Trash."""
    if account is None or not folder:
        return False
    trash = _folder_display_to_himalaya("Trash", account) or ""
    return str(folder).strip().casefold() == trash.strip().casefold()


def _ordered_folder_list(account, raw_names, unread_counts=None):
    """
    Turns a raw list of real server mailbox names (as returned by
    himalaya_client.list_all_folders) into an ordered list of
    (display_label, real_name, unread_count) triples for the account
    tree: the six special folders first, in FOLDER_TYPES order,
    labeled with their friendly Thunderbird-style name whenever the
    server actually has that mailbox; every other real mailbox the
    server reports follows, alphabetically, labeled with its own raw
    name -- ZBox doesn't invent a friendlier name for a folder it
    didn't create. A special folder the server doesn't have (e.g. no
    Archive on a non-Gmail account) is simply absent, not shown empty.

    Specials are matched case-insensitively against the server's own
    spelling, and each triple carries that spelling rather than the
    mapped constant. Gmail reports its inbox as "Inbox" while
    _folder_display_to_himalaya returns the universal "INBOX", and an
    exact match sent the inbox into the alphabetical remainder below
    every other special folder -- on every account, not only Gmail.

    unread_counts (audit finding 43): optional dict of real_name ->
    unread count (see himalaya_client._mailbox_unread_count), read
    straight through into each triple's third element. None -- the
    default, and also what a real_name missing from the dict gets --
    means "unknown", not zero; display_label itself is never modified
    here, so a caller building UI text decides how to show a count
    (see _folder_tree_label) or ignores it entirely (folder-management
    callers that only need name/real_name still unpack the same
    triples).

    Pure and account-aware only through _folder_display_to_himalaya
    (imap_host lookup); no wx, no network, so unit-testable against a
    fixture list of raw names, same as the rest of this module.
    """
    unread_counts = unread_counts or {}
    raw_set = set(raw_names)
    # Folded spelling -> the spelling the server actually used, so a
    # special folder is found whatever case it was reported in. First
    # spelling wins: a server exposing two mailboxes differing only by
    # case (which IMAP does not permit for INBOX) keeps the other one
    # visible in the remainder below rather than losing it.
    by_folded = {}
    for name in raw_names:
        if isinstance(name, str):
            by_folded.setdefault(name.casefold(), name)
    seen_specials = set()
    ordered = []
    for label in FOLDER_TYPES:
        mapped = _folder_display_to_himalaya(label, account)
        real = by_folded.get(mapped.casefold())
        if real is not None and real not in seen_specials:
            ordered.append((label, real, unread_counts.get(real)))
            seen_specials.add(real)
    customs = sorted(name for name in raw_set if name not in seen_specials)
    for name in customs:
        ordered.append((name, name, unread_counts.get(name)))
    return ordered


def _total_unread_count(folders_by_account):
    """Sums each account's Inbox unread count from the
    [(display_label, real_name, unread_count), ...] triples this
    module builds (see main_frame._tree_folder_lists, findings 42/43),
    for the system tray icon's tooltip and context-menu line.

    An account not yet in the dict (background fetch hasn't resolved
    yet), one with no recognized Inbox entry, or an unread_count of
    None (finding 43: None means "unknown", not zero) all contribute
    0 -- approximate and eventually-consistent, the same
    characteristic the tree's own folder labels already have while
    waiting on the next background 'mailbox list --all --counts' to
    resolve. Only Inbox counts toward the total, matching how a tray
    unread indicator is conventionally read (new mail, not every
    unread message in every folder including Sent/Archive).

    Pure, no wx -- unit-testable against a fixture dict shaped like
    main_frame._tree_folder_lists.
    """
    total = 0
    for pairs in (folders_by_account or {}).values():
        for label, _real_name, unread_count in pairs:
            if label == "Inbox":
                total += unread_count or 0
                break
    return total


def adjust_inbox_unread(folders_by_account, account_id, folder, delta):
    """Moves one account's Inbox unread number in the
    main_frame._tree_folder_lists shape by delta, never below zero,
    so the tray can follow a read, an unread or a delete at once
    instead of waiting for the next counts refresh from the server.

    folder is the folder the change happened in; anything but that
    account's Inbox is ignored. An unknown count (None) stays unknown.
    Returns True when the stored number changed. Pure, no wx.
    """
    if not delta or not folders_by_account:
        return False
    pairs = folders_by_account.get(account_id)
    if not pairs:
        return False
    for index, (label, real_name, unread_count) in enumerate(pairs):
        if label != "Inbox":
            continue
        if folder is not None:
            wanted = str(folder).casefold()
            if wanted not in (str(real_name).casefold(), "inbox"):
                return False
        if unread_count is None:
            return False
        new_count = max(0, int(unread_count) + int(delta))
        if new_count == unread_count:
            return False
        updated = list(pairs)
        updated[index] = (label, real_name, new_count)
        folders_by_account[account_id] = updated
        return True
    return False


def _folder_tree_label(label, unread_count):
    """
    Appends an unread count to a folder's tree label so the count is
    part of the announced text itself (audit finding 43 asks for
    exactly this -- "rendered as text so they are announced" -- not a
    separate control or attribute a screen reader might not surface
    on a wx.TreeCtrl item). Omitted entirely when there is no unread
    mail (0) or the count is unknown (None): Thunderbird's own unread
    badge does the same, and reading "0 unread" on every folder while
    arrowing through the tree would be exactly the kind of routine-
    state chatter this project has otherwise deliberately avoided
    (see announce.py's don't-interrupt-for-routine-completions rule).
    """
    if not unread_count:
        return label
    return f"{label} ({unread_count} unread)"


def _envelope_from(envelope):
    """
    'from' is a LIST of {"name": ..., "email": ...} dicts (confirmed
    via live Himalaya 2.1.0 response capture), not a single flat
    value or a single dict. Using the whole list where a string was
    expected previously produced a garbled Python repr instead of a
    sender name.
    """
    sender_list = envelope.get("from")
    if isinstance(sender_list, list) and sender_list:
        sender = sender_list[0]
        if isinstance(sender, dict):
            return sender.get("name") or sender.get("email") or "(unknown sender)"
    return "(unknown sender)"


# Subjects read from inside encrypted messages opened this session (the
# OpenPGP protected headers), by Message-ID, shown in place of the "..."
# the sender put outside. Memory only: never written anywhere.
_SESSION_SUBJECTS = {}


def remember_session_subject(message_id, subject):
    key = str(message_id or "").strip()
    if key and subject:
        _SESSION_SUBJECTS[key] = str(subject)


def _envelope_subject(envelope):
    if _SESSION_SUBJECTS:
        hidden = _SESSION_SUBJECTS.get(str(envelope.get("message-id") or "").strip())
        if hidden:
            return hidden
    subject = str(envelope.get("subject") or "(no subject)")
    if subject.strip() == "...":
        # OpenPGP protected headers: the real Subject is inside the
        # encrypted message. Until it is opened and read, say what it is
        # rather than a bare "..." a screen reader speaks as nothing.
        return lang.t("dialogs", "pgp_rs_hidden_subject", default="Encrypted Message")
    return subject


def _envelope_date(envelope):
    return envelope.get("date") or ""


_MAILTO_FIELDS = ("to", "cc", "bcc", "subject", "body")


def parse_mailto(url):
    """
    Splits a mailto: URL (RFC 6068) into the compose fields it names.

    Returns a dict with to/cc/bcc/subject/body keys, every one a
    string and absent parts empty, so a caller can hand the whole
    thing straight to ComposePanel without checking each field.

    unquote, never unquote_plus: RFC 6068 percent-encodes, it does not
    form-encode, so a literal "+" in a mailto is part of the address
    and turning it into a space would quietly break every
    user+tag@example.com link -- exactly the addresses most likely to
    appear in a mailing-list footer.

    Recipients before the "?" are joined with any to= in the query
    rather than one replacing the other, since a link is allowed to
    use either or both. Unknown query parameters (RFC 6068 allows
    arbitrary headers) are ignored: honouring an arbitrary header from
    a link inside an untrusted message is not something a mail client
    should do quietly.
    """
    if not url:
        return {field: "" for field in _MAILTO_FIELDS}

    split = urlsplit(url)
    # urlsplit puts everything after "mailto:" in .path when there is
    # no "//", which is the normal shape; the address list is the part
    # before "?", which urlsplit has already separated into .query.
    recipients = [unquote(split.path or "").strip()]

    fields = {field: "" for field in _MAILTO_FIELDS}
    for pair in (split.query or "").split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        name = unquote(name).strip().lower()
        if name not in _MAILTO_FIELDS:
            continue
        value = unquote(value)
        if name == "to":
            recipients.append(value.strip())
        elif fields[name]:
            fields[name] = fields[name] + ", " + value
        else:
            fields[name] = value

    fields["to"] = ", ".join(part for part in recipients if part)
    return fields


_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _friendly_envelope_date(envelope, now=None):
    """
    Himalaya's raw ISO-8601 date turned into something a screen reader
    can read as a date rather than spell out as a string.

    "2026-09-05T22:14:36+01:00" is correct and unreadable: NVDA and
    JAWS pronounce it character by character, once per row, which is
    most of what gets said while arrowing down a message list.

    Shown in the reader's own local time, and only as precise as it
    needs to be:
      today            -> "Today 22:14"
      yesterday        -> "Yesterday 22:14"
      this year        -> "5 Sep 22:14"
      any older year   -> "5 Sep 2025 22:14"

    Today and Yesterday are spelled out rather than left as a bare
    time, so a row is never ambiguous about which day it belongs to.
    The clock follows Windows' own time format (see
    _system_clock_text), so 12 or 24 hour is whatever the reader set
    in Region settings.

    Month names come from a literal table, not strftime("%b"), which
    would follow the C locale rather than anything the reader chose.
    An unparseable or missing date falls back to whatever the server
    sent -- wrong-looking is better than blank when a date is the
    thing being sorted on.

    now is injectable for testing; it defaults to the current local
    time.
    """
    raw = _envelope_date(envelope)
    if not raw:
        return ""
    parsed = _parse_envelope_date(envelope)
    if parsed == datetime.min.replace(tzinfo=timezone.utc):
        return raw
    try:
        local = parsed.astimezone()
    except (ValueError, OSError, OverflowError):
        return raw

    if now is None:
        now = datetime.now().astimezone()
    today = now.date()
    day = local.date()
    clock = _clock_text(local)
    month = _MONTH_ABBREVIATIONS[local.month - 1]

    if day == today:
        return "Today %s" % clock
    if (today - day).days == 1:
        return "Yesterday %s" % clock
    if local.year == now.year:
        return "%d %s %s" % (local.day, month, clock)
    return "%d %s %d %s" % (local.day, month, local.year, clock)


def _parse_envelope_date(envelope):
    """
    Parses Himalaya's ISO-8601 date string into a timezone-aware
    datetime for correct chronological sorting -- unlike sorting the
    string itself (what _SORT_KEY_FUNCS["date"] and the unified-folder
    merges in mail_fetch.py used to do).

    The string is fixed-format but its UTC offset varies message to
    message (each sender's own timezone: "+01:00", "-04:00", "Z",
    ...), and that defeats plain string sorting: "...T18:55:26-04:00"
    (22:55 UTC) sorts before "...T21:02:55+01:00" (20:02 UTC) purely
    because '1' < '2' as characters, when the second one actually
    happened first. Real ZBox mail spans enough providers and
    timezones (Gmail, Disroot, PayPal, Google Voice texts, ...) that
    this isn't hypothetical -- a fast-moving thread or a Unified
    folder mixing accounts hits it often.

    'Z' is normalised to '+00:00' first: datetime.fromisoformat only
    accepts a bare 'Z' suffix from Python 3.11, and ZBox runs on
    3.10. A missing or unparseable date returns datetime.min (UTC) --
    sorts first, rather than raising and breaking the whole sort.
    """
    date_string = envelope.get("date")
    if not date_string:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        if date_string.endswith("Z"):
            date_string = date_string[:-1] + "+00:00"
        parsed = datetime.fromisoformat(date_string)
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _envelope_sender_address(envelope):
    """
    A single 'Name <email>' (or bare email) string for the message's
    From address, suitable for a compose To field, built from the
    same {"name", "email"} shape _envelope_from reads. Returns ''
    if no usable address is present.
    """
    sender_list = envelope.get("from")
    if isinstance(sender_list, list) and sender_list:
        sender = sender_list[0]
        if isinstance(sender, dict) and sender.get("email"):
            name = sender.get("name")
            return formataddr((name, sender["email"])) if name else sender["email"]
    return ""


def _envelope_sender_spoken(envelope):
    """(text to announce, bare address) for the sender.

    Written for speech, not for a To field: "Sam Sender, sender@example.com"
    rather than "Sam Sender <sender@example.com>", because a screen reader
    reads angle brackets aloud and they carry nothing here. Falls back
    to the address alone when there is no display name, and says so
    plainly when there is no sender at all.
    """
    sender_list = envelope.get("from") if isinstance(envelope, dict) else None
    if isinstance(sender_list, list) and sender_list:
        sender = sender_list[0]
        if isinstance(sender, dict):
            name = (sender.get("name") or "").strip()
            email = (sender.get("email") or "").strip()
            if name and email:
                return "%s, %s" % (name, email), email
            if email:
                return email, email
            if name:
                return lang.t('actions_announcements', 'sender_name_no_address', default="%s, no address") % name, ""
    return lang.t('actions_announcements', 'sender_none', default="No sender on this message."), ""


def _envelope_sender_email_only(envelope):
    """Bare, lowercased sender email address, or '' -- used to keep
    the sender out of a Reply All Cc list (they're already in To)."""
    sender_list = envelope.get("from")
    if isinstance(sender_list, list) and sender_list:
        sender = sender_list[0]
        if isinstance(sender, dict):
            return (sender.get("email") or "").lower()
    return ""


def _format_address_entries(entries, exclude_emails):
    """
    Formats a Himalaya address list (list of {"name", "email"}
    dicts, the same shape as envelope 'from') into a comma-separated
    'Name <email>' string for a compose Cc field, skipping any
    address in exclude_emails (already lowercased) and de-duplicating
    as it goes.
    """
    if not isinstance(entries, list):
        return []
    formatted = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if not email or email.lower() in exclude_emails:
            continue
        exclude_emails.add(email.lower())
        name = entry.get("name")
        formatted.append(formataddr((name, email)) if name else email)
    return formatted


def _reply_recipient(envelope, message):
    """Who a reply actually goes to.

    Reply-To wins over From when the sender set one, which is the
    whole point of the header: mailing lists and no-reply senders use
    it to redirect replies, and reading From regardless sends the
    reply to an address nobody is watching. Falls back to From when
    there is no Reply-To, or when it carries no usable address.

    Returns (display string for the To field, set of lowercase
    addresses already covered by it).
    """
    entries = header_addresses(message, "reply_to")
    if entries:
        formatted = [
            formataddr((name, address)) if name else address
            for name, address in entries
        ]
        covered = {address.lower() for _name, address in entries}
        return ", ".join(formatted), covered

    to_field = _envelope_sender_address(envelope)
    sender_email = _envelope_sender_email_only(envelope)
    return to_field, {sender_email} if sender_email else set()


def _reply_all_cc_field(account, envelope, sender_email):
    """
    Builds the Cc field for Reply All: the original message's To and
    Cc recipients, minus the sender (already going in To) and minus
    this account's own addresses (no point Ccing yourself).
    """
    if isinstance(sender_email, (set, frozenset)):
        exclude = {address.lower() for address in sender_email if address}
    else:
        exclude = {sender_email} if sender_email else set()
    for address in (account.identity_email, account.login_email):
        if address:
            exclude.add(address.lower())

    combined = []
    for key in ("to", "cc"):
        combined.extend(_format_address_entries(envelope.get(key), exclude))
    return ", ".join(combined)


def _reply_from_identity(account, envelope):
    """
    Audit finding 49: which of this account's identities (its
    primary address plus any configured extra_identities/aliases --
    see Account.identities) a reply should send from, so replying to
    mail received at an alias goes back out under that same alias
    instead of always the account's primary address. The mirror
    image of _reply_all_cc_field excluding the account's own
    addresses from Cc above: this looks for one of them instead.

    Checks the original message's To then Cc recipients, in that
    order, for the first address matching one of this account's
    identities (case-insensitive), and returns that identity's email
    in its own stored casing. Falls back to account.identity_email
    when nothing matches -- the message arrived via a mailing list, a
    Bcc, or an address this account has no identity registered for.
    """
    known = {}
    for _display_name, email in account.identities():
        if email:
            known.setdefault(email.lower(), email)

    for key in ("to", "cc"):
        entries = envelope.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            email = entry.get("email")
            if email and email.lower() in known:
                return known[email.lower()]
    return account.identity_email


def _reply_subject(subject):
    subject = subject or "(no subject)"
    return subject if subject.strip().lower().startswith("re:") else f"Re: {subject}"


def _forward_subject(subject):
    subject = subject or "(no subject)"
    return subject if subject.strip().lower().startswith("fwd:") else f"Fwd: {subject}"


def _envelope_addr_list_field(envelope, key):
    """Comma-separated 'Name <email>' string for envelope['to'] or
    envelope['cc'], for display in a forwarded message's header
    block -- not for filling a compose field. Returns '' if absent."""
    entries = envelope.get(key)
    if not isinstance(entries, list):
        return ""
    formatted = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if not email:
            continue
        name = entry.get("name")
        formatted.append(formataddr((name, email)) if name else email)
    return ", ".join(formatted)


def _message_header_block(envelope):
    """The From/To/Cc/Date/Subject lines shown above an open message.

    A message tab used to render the body and nothing else, so from
    inside a message there was no way to tell who sent it, who else
    received it or when it arrived without closing the tab and going
    back to the list. Thunderbird reads a header block first for
    exactly this reason.

    Written as plain "Label: value" lines, one per line, because a
    screen reader reads them in order and the label is what tells you
    which field you are hearing. Empty fields are left out rather than
    announced as empty -- "Cc: " with nothing after it is a line the
    reader has to listen to for no reason.
    """
    lines = []
    sender = _envelope_sender_address(envelope)
    if sender:
        lines.append("From: %s" % sender)
    for key, label in (("to", "To"), ("cc", "Cc")):
        value = _envelope_addr_list_field(envelope, key)
        if value:
            lines.append("%s: %s" % (label, value))
    date = _friendly_envelope_date(envelope)
    if date:
        lines.append("Date: %s" % date)
    subject = envelope.get("subject") if isinstance(envelope, dict) else None
    lines.append("Subject: %s" % (subject or "(no subject)"))
    return "\n".join(lines)


def _quoted_reply_body(envelope, message):
    """Plain-text quoted original message, prefixed with a 'wrote:'
    attribution line, in the same simple '>'-quoting convention every
    plain-text mail client uses -- no HTML, matching the rest of
    ZBox's screen-reader-first, plain-TextCtrl composing."""
    sender = _envelope_from(envelope)
    date = _envelope_date(envelope)
    attribution = f"On {date}, {sender} wrote:" if date else f"{sender} wrote:"

    original_body = extract_message_body(message) or ""
    quoted_lines = ["> " + line if line else ">" for line in original_body.splitlines()]

    return "\n\n\n" + attribution + "\n" + "\n".join(quoted_lines) + "\n"


def _forwarded_body(envelope, message):
    """
    Plain-text forwarded message: a standard header block (From,
    Date, Subject, To) followed by the original body unquoted, the
    conventional forward layout every plain-text mail client uses.
    Unlike a reply, the body is not '>'-prefixed, since a forward is
    presented as the original message itself rather than something
    being replied to inline.
    """
    header_lines = [
        "---------- Forwarded message ----------",
        f"From: {_envelope_from(envelope)}",
    ]
    date = _envelope_date(envelope)
    if date:
        header_lines.append(f"Date: {date}")
    header_lines.append(f"Subject: {_envelope_subject(envelope)}")
    to_field = _envelope_addr_list_field(envelope, "to")
    if to_field:
        header_lines.append(f"To: {to_field}")

    original_body = extract_message_body(message) or ""
    return "\n\n\n" + "\n".join(header_lines) + "\n\n" + original_body + "\n"


def _append_signature(body, signature):
    """
    Appends an account's signature (audit finding 41) below the
    composed body -- new message, reply or forward alike -- using the
    conventional "-- " delimiter line on its own (two dashes, one
    trailing space) that plain-text mail clients have used for
    decades to mark where a signature starts, so anything that
    strips or requotes signatures has a line to key off. A signature
    that is empty or all whitespace is a no-op and returns body
    unchanged, which is also what a newly created account with no
    signature configured yet gets.
    """
    signature = (signature or "").strip()
    if not signature:
        return body
    return f"{body}\n\n-- \n{signature}\n"


def _accounts_for_search_selection(accounts, selected_index):
    """
    Which accounts search_panel.SearchTabPanel should search, given
    its Account wx.Choice's current selection. Index 0 in that choice
    is the synthetic "All Accounts" entry (see
    search_panel.ALL_ACCOUNTS_LABEL), so a real account at position N
    in `accounts` sits at choice index N + 1.

    Falls back to every account -- rather than none -- for "All
    Accounts" itself (index 0), no selection at all (wx.NOT_FOUND,
    -1), and an out-of-range index (a selection left stale after
    refresh_accounts() shrank the list from underneath it, e.g. the
    selected account was just removed). The one case that returns a
    single account is a real, in-range, non-zero selection.
    """
    accounts = list(accounts)
    if selected_index is None or selected_index <= 0:
        return accounts
    real_index = selected_index - 1
    if real_index >= len(accounts):
        return accounts
    return [accounts[real_index]]


def _resolve_search_folders(folder_names, account):
    """
    Resolves each of `folder_names` (as returned by
    main_frame._folders_for_account -- the six generic display names
    before that account's real folder list has ever been fetched, or
    real per-account server names after) through
    _folder_display_to_himalaya, exactly like
    main_frame._build_move_copy_menus already does before using any
    of these names in a real Himalaya call.

    Bug this fixes: search_panel's "All Folders" scope used to pass
    these names straight through unresolved. That's correct only for
    a provider whose real folder happens to be spelled exactly like
    the generic name ("Sent" really is "Sent") -- silently wrong for
    any that aren't, Gmail being the standout (its Sent is
    "[Gmail]/Sent Mail", Archive is "[Gmail]/All Mail", etc). Each
    wrong folder name then failed with a HimalayaError that
    SearchTabPanel's per-folder try/except quietly logs and skips, so
    "All Folders" search came back with matches from Inbox only (and
    even that only survives because IMAP treats INBOX
    case-insensitively) -- indistinguishable, to whoever is
    searching, from search not working at all. Confirmed live against
    the pinned himalaya v2.1.0 build: `envelope search -m Sent ...`
    against a folder actually named something else fails outright
    rather than falling back to a fuzzy match.
    """
    return [_folder_display_to_himalaya(name, account) for name in folder_names]


def _envelope_is_unread(envelope):
    """
    Confirmed via live capture: a read message's flags array contains
    an entry with iana == "seen" (raw == "\\Seen"); an unread message
    has no such entry at all (e.g. just [{"raw": "NonJunk", "iana":
    null}]).
    """
    flags = envelope.get("flags") or []
    for flag in flags:
        if isinstance(flag, dict) and flag.get("iana") == "seen":
            return False
    return True


def _set_envelope_flag(envelope, iana_name, present):
    """
    Mutates a locally-held envelope dict's flags list in place so the
    Status column can be recomputed immediately after a read/unread
    or flag toggle, without waiting for the next full folder refetch.
    """
    flags = envelope.get("flags")
    if not isinstance(flags, list):
        flags = []
        envelope["flags"] = flags
    flags[:] = [f for f in flags if not (isinstance(f, dict) and f.get("iana") == iana_name)]
    if present:
        flags.append({"iana": iana_name, "raw": "\\" + iana_name.capitalize()})


def _envelope_backend(envelope):
    """
    Which backend this envelope's data actually came from --
    "maildir" for a still-on-screen cache-preview row, "imap"
    otherwise. Reading a message must target the backend the
    displayed row actually came from, not just guess from the Work
    Offline toggle: a row can be on screen from the local cache
    preview before the live IMAP fetch has replaced it (see
    _show_cached_envelopes_first), and its "id" is in Maildir's id
    shape, not IMAP's numeric UID -- passing that id to the IMAP
    backend is exactly what Himalaya's own "Invalid message UID"
    error caught, confirmed via a real user's debug log.

    Defaults to "imap" for any envelope predating this tag.
    """
    return envelope.get("_zbox_backend", "imap")


def _envelope_is_flagged(envelope):
    """Confirmed via live capture: a starred/flagged message's flags
    array contains an entry with iana == "flagged" (raw == "\\Flagged"),
    the same shape as the "seen" entry _envelope_is_unread checks."""
    flags = envelope.get("flags") or []
    return any(isinstance(flag, dict) and flag.get("iana") == "flagged" for flag in flags)


def _envelope_status_text(unread, flagged):
    r"""Plain-text Status column value. Deliberately text, not an icon
    or color, per the accessibility rule that flagged/unread state
    must be readable by a screen reader as words.

    "Starred" rather than "Flagged" (2026-09-06, user request): the
    word a screen reader announces for this row's own \Flagged state,
    present only while the message is flagged and gone the moment
    it's unflagged -- himalaya_client.set_flagged's own docstring
    already called this the "starred" state, this just makes the
    on-screen wording match what users actually call it.
    """
    parts = []
    if unread:
        parts.append("Unread")
    if flagged:
        parts.append("Starred")
    return ", ".join(parts)


def _envelope_recipient(envelope):
    """Every To address, by name where it has one, joined: the
    Recipient column. To is a list of {"name", "email"} dicts, or a
    single one."""
    entries = envelope.get("to")
    if isinstance(entries, dict):
        entries = [entries]
    names = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            text = entry.get("name") or entry.get("email") or entry.get("address")
            if text:
                names.append(str(text))
    return ", ".join(names)


def _envelope_size(envelope):
    """The message size in bytes, 0 when the listing gave none."""
    try:
        return max(0, int(envelope.get("size") or 0))
    except (TypeError, ValueError):
        return 0


def _envelope_has_attachment(envelope):
    """True when the listing said the message has an attachment. Rows
    Himalaya listed without its costly option carry null: no."""
    return bool(envelope.get("has-attachment"))


def _envelope_priority(envelope):
    """1 (highest) to 5 (lowest); 3, normal, when none is known."""
    try:
        value = int(envelope.get("priority") or 3)
    except (TypeError, ValueError):
        return 3
    return value if 1 <= value <= 5 else 3


def _parse_envelope_received(envelope):
    """The server's arrival date, parsed like the Date header; the Date
    header itself for rows that came without one."""
    received = envelope.get("received")
    if received:
        return _parse_envelope_date({"date": received})
    return _parse_envelope_date(envelope)


def _friendly_envelope_received(envelope):
    """The Received column: the arrival date, read like the Date
    column, or the Date header for rows without one."""
    received = envelope.get("received")
    if received:
        return _friendly_envelope_date({"date": received})
    return _friendly_envelope_date(envelope)


def _envelope_order(envelope):
    """Order Received: the server's UID, which grows with each arrival.
    An offline-copy id that is not a number sorts after the numbers."""
    text = str(envelope.get("id") or "")
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text)


def _envelope_size_text(envelope):
    """The Size column, as Thunderbird writes it: whole kilobytes below
    a megabyte, then megabytes with one decimal. Empty when unknown."""
    size = _envelope_size(envelope)
    if size <= 0:
        return ""
    if size < 1024 * 1024:
        return lang.t("dialogs", "envlist_size_kb", default="{size} KB",
                      size=max(1, (size + 1023) // 1024))
    return lang.t("dialogs", "envlist_size_mb", default="{size} MB",
                  size="%.1f" % (size / 1048576.0))


def _envelope_priority_text(envelope):
    """The Priority column: nothing for Normal, as in Thunderbird."""
    value = _envelope_priority(envelope)
    if value == 1:
        return lang.t("dialogs", "envlist_priority_highest", default="Highest")
    if value == 2:
        return lang.t("dialogs", "envlist_priority_high", default="High")
    if value == 4:
        return lang.t("dialogs", "envlist_priority_low", default="Low")
    if value == 5:
        return lang.t("dialogs", "envlist_priority_lowest", default="Lowest")
    return ""


def _envelope_attachment_text(envelope):
    """The Attachments column: a word when there is one, else nothing."""
    if _envelope_has_attachment(envelope):
        return lang.t("dialogs", "envlist_attachment", default="Attachments")
    return ""


# View > Sort By, in Thunderbird's order. Keys that can tie take the
# date as their second value, so equal rows stay in date order.
# "correspondents" sorts by From here; the list panel uses "recipient"
# instead for Sent and Drafts (envelope_list_panel._apply_sort).
_SORT_KEY_FUNCS = {
    "date": lambda envelope: _parse_envelope_date(envelope),
    "received": lambda envelope: _parse_envelope_received(envelope),
    "star": lambda envelope: (_envelope_is_flagged(envelope), _parse_envelope_date(envelope)),
    "order": lambda envelope: _envelope_order(envelope),
    "priority": lambda envelope: (-_envelope_priority(envelope), _parse_envelope_date(envelope)),
    "from": lambda envelope: _envelope_from(envelope).lower(),
    "recipient": lambda envelope: _envelope_recipient(envelope).lower(),
    "correspondents": lambda envelope: _envelope_from(envelope).lower(),
    "size": lambda envelope: (_envelope_size(envelope), _parse_envelope_date(envelope)),
    "subject": lambda envelope: _envelope_subject(envelope).lower(),
    "read": lambda envelope: (_envelope_is_unread(envelope), _parse_envelope_date(envelope)),
    "junk": lambda envelope: (bool(envelope.get("_zbox_spam_label")), _parse_envelope_date(envelope)),
    "attachments": lambda envelope: (_envelope_has_attachment(envelope), _parse_envelope_date(envelope)),
}


def _envelopes_signature(envelopes):
    """
    Compact, comparable snapshot of an envelope list used by
    populate_if_changed. Includes the account id so unified folders
    (where two accounts can each have a message with the same id)
    don't collide, plus the fields that visibly change: read state,
    sender, subject and date.

    Deliberately order-independent. The server returns mail in its own
    order; the list may be showing the reader's chosen order. Comparing
    those as sequences reports "changed" on every single poll purely
    because the rows are arranged differently, which is what made a
    chosen sort survive exactly one refresh cycle. Account id is
    coerced to a string so the sort here cannot trip over None mixed
    with real ids in non-unified folders.
    """
    return tuple(sorted(
        (
            str(envelope.get("_zbox_account_id") or ""),
            str(envelope.get("id") or ""),
            _envelope_is_unread(envelope),
            _envelope_is_flagged(envelope),
            _envelope_from(envelope),
            _envelope_subject(envelope),
            _envelope_date(envelope),
            _envelope_recipient(envelope),
            _envelope_size(envelope),
            _envelope_has_attachment(envelope),
            _envelope_priority(envelope),
            str(envelope.get("received") or ""),
        )
        for envelope in envelopes
    ))
