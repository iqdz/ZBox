"""
Junk rules: fixed, explainable checks for new Inbox mail. They replaced
the learned spam filter (spam_filter.py) on 16 September 2026, after it
condemned ordinary mailing-list mail and sent mail the user had rescued
from Junk straight back. See docs/decisions.md.

Nothing here guesses. Three things decide:

- Blocked senders. Mark as Junk adds the sender's address, except for
  mailing-list mail (the sender is also a recipient, as groups.io
  sends it) and mail from the user's own addresses.
- Never-junk senders and exempt messages. Mark as Not Junk adds the
  sender and exempts that message for good. They win over everything.
- The phishing list (blocklist.PhishingList), matched against the
  hosts of the links in a message. main_frame only marks the row.

ExpectedArrivals is the other half of the fix: messages ZBox itself
moves into a folder are not new mail, so nothing treats them as such.

Pure and wx-free, so it is tested directly.
"""

import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger("zbox.junkrules")

ALLOWED = "allowed"
BLOCKED = "blocked"

# Newest entries kept; older ones fall off. A list of exempt messages
# that only grows would eventually be read on every new arrival.
MAX_EXEMPT = 5000
MAX_RECENT = 500

_LINK_RE = re.compile(r"https?://([A-Za-z0-9.-]+)|\b(www\.[A-Za-z0-9.-]+)", re.IGNORECASE)
_QP_SOFT_BREAK_RE = re.compile(r"=\r?\n")


def canonical_message_id(value):
    """Same canonical form as himalaya_client._canonical_message_id:
    no angle brackets, no surrounding space, casefolded."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return text.casefold() or None


def normalize_address(value):
    if not isinstance(value, str):
        return None
    text = value.strip().strip("<>").strip().casefold()
    return text if "@" in text else None


def _addresses(field):
    if isinstance(field, dict):
        field = [field]
    if not isinstance(field, list):
        return []
    found = []
    for entry in field:
        address = normalize_address(entry.get("email") if isinstance(entry, dict) else entry)
        if address:
            found.append(address)
    return found


def envelope_sender(envelope):
    """The first From address of a Himalaya envelope, normalised, or None."""
    addresses = _addresses((envelope or {}).get("from"))
    return addresses[0] if addresses else None


def is_mailing_list_mail(envelope):
    """True when the sender is also a recipient -- how groups.io and
    most list servers address a post. Blocking that address would
    block the whole list."""
    sender = envelope_sender(envelope)
    if not sender:
        return False
    recipients = set(_addresses(envelope.get("to"))) | set(_addresses(envelope.get("cc")))
    return sender in recipients


def link_hosts(raw_text):
    """Lowercased hosts of every http(s) or www. link in a raw message.
    Quoted-printable soft line breaks are joined first, so a link split
    across two encoded lines is still one host."""
    if not raw_text:
        return set()
    text = _QP_SOFT_BREAK_RE.sub("", raw_text)
    return {
        (match.group(1) or match.group(2)).strip(".").lower()
        for match in _LINK_RE.finditer(text)
    } - {""}


class JunkRules:
    """The sender lists and exempt messages, in one JSON file shared by
    every account. Read on the sync worker and written on the UI
    thread, so every access takes the lock. A failed save loses one
    change, never mail, and is logged."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._blocked = set()
        self._allowed = set()
        self._exempt = []  # canonical Message-IDs, oldest first
        # canonical Message-ID -> (kind, address, newly added), so Undo
        # can take back exactly what Mark as Junk or Not Junk added.
        self._recent = {}
        self._load()

    # -- disk ---------------------------------------------------------

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Could not read junk rules from %s; starting empty.", self._path)
            return
        if not isinstance(data, dict):
            return
        self._blocked = {a for a in map(normalize_address, data.get("blocked") or []) if a}
        self._allowed = {a for a in map(normalize_address, data.get("allowed") or []) if a}
        self._exempt = [m for m in map(canonical_message_id, data.get("exempt") or []) if m]

    def _save(self):
        data = {
            "blocked": sorted(self._blocked),
            "allowed": sorted(self._allowed),
            "exempt": self._exempt[-MAX_EXEMPT:],
        }
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            temp = self._path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(temp, self._path)
        except OSError:
            logger.warning("Could not save junk rules to %s.", self._path, exc_info=True)

    # -- reading ------------------------------------------------------

    @property
    def blocked(self):
        with self._lock:
            return sorted(self._blocked)

    @property
    def allowed(self):
        with self._lock:
            return sorted(self._allowed)

    def state_for(self, envelope):
        """ALLOWED, BLOCKED or None for a new message's envelope. An
        exempt message or never-junk sender wins over a blocked one."""
        header = canonical_message_id((envelope or {}).get("message-id"))
        sender = envelope_sender(envelope)
        with self._lock:
            if header and header in self._exempt:
                return ALLOWED
            if sender and sender in self._allowed:
                return ALLOWED
            if sender and sender in self._blocked:
                return BLOCKED
        return None

    # -- changes ------------------------------------------------------

    def _remember(self, header, kind, address, added):
        if not header:
            return
        self._recent[header] = (kind, address, added)
        while len(self._recent) > MAX_RECENT:
            self._recent.pop(next(iter(self._recent)))

    def block_senders(self, envelopes, own_addresses=()):
        """Mark as Junk. Returns how many addresses were newly blocked.
        Mailing-list mail and the user's own addresses are skipped."""
        own = {a for a in map(normalize_address, own_addresses) if a}
        added = 0
        with self._lock:
            for envelope in envelopes:
                sender = envelope_sender(envelope)
                if not sender or sender in own or is_mailing_list_mail(envelope):
                    continue
                was_allowed = sender in self._allowed
                new = sender not in self._blocked
                self._allowed.discard(sender)
                self._blocked.add(sender)
                self._remember(
                    canonical_message_id(envelope.get("message-id")),
                    "block", sender, (new, was_allowed),
                )
                added += 1 if new else 0
            self._save()
        return added

    def allow_senders(self, envelopes):
        """Mark as Not Junk: the sender goes on the never-junk list, off
        the blocked list, and the message is exempt for good."""
        with self._lock:
            for envelope in envelopes:
                header = canonical_message_id((envelope or {}).get("message-id"))
                sender = envelope_sender(envelope)
                was_blocked = bool(sender) and sender in self._blocked
                new = bool(sender) and sender not in self._allowed
                if sender:
                    self._blocked.discard(sender)
                    self._allowed.add(sender)
                if header and header not in self._exempt:
                    self._exempt.append(header)
                self._remember(header, "allow", sender, (new, was_blocked))
            del self._exempt[:-MAX_EXEMPT]
            self._save()

    def undo_block(self, message_id_headers):
        """Undo of Mark as Junk: takes back only what it added, and
        puts back a never-junk entry it replaced."""
        with self._lock:
            for header in map(canonical_message_id, message_id_headers):
                entry = self._recent.pop(header, None) if header else None
                if not entry or entry[0] != "block":
                    continue
                _kind, sender, (new, was_allowed) = entry
                if new:
                    self._blocked.discard(sender)
                if was_allowed:
                    self._allowed.add(sender)
            self._save()

    def undo_allow(self, message_id_headers):
        """Undo of Mark as Not Junk: the exemption and a newly added
        never-junk entry go, and a blocked entry it replaced returns."""
        with self._lock:
            for header in map(canonical_message_id, message_id_headers):
                entry = self._recent.pop(header, None) if header else None
                if not entry or entry[0] != "allow":
                    continue
                _kind, sender, (new, was_blocked) = entry
                if header in self._exempt:
                    self._exempt.remove(header)
                if sender and new:
                    self._allowed.discard(sender)
                if sender and was_blocked:
                    self._blocked.add(sender)
            self._save()

    def remove_senders(self, blocked, allowed):
        """Tools > Privacy and Content Blocking's only possible change:
        it can remove entries, never add them. Taking the removals
        rather than both lists whole is what keeps a Mark as Junk made
        while that dialog sat open from being erased when it is OK'd --
        the dialog's snapshot of the lists is taken when it opens."""
        removed_blocked = {a for a in map(normalize_address, blocked) if a}
        removed_allowed = {a for a in map(normalize_address, allowed) if a}
        if not removed_blocked and not removed_allowed:
            return
        with self._lock:
            self._blocked -= removed_blocked
            self._allowed -= removed_allowed
            self._save()

    def set_lists(self, blocked, allowed):
        """Replaces both lists outright. No caller in the app does this
        any more (see remove_senders); kept for import/export and tests."""
        with self._lock:
            self._blocked = {a for a in map(normalize_address, blocked) if a}
            self._allowed = {a for a in map(normalize_address, allowed) if a}
            self._save()


class ExpectedArrivals:
    """Messages ZBox is moving into a folder, keyed by account, folder
    and canonical Message-ID, so the listing that sees them arrive does
    not call them new mail. Each entry is used once and expires after
    `ttl` seconds, so a move that failed cannot hide a real delivery
    of the same message for long."""

    def __init__(self, ttl=600.0, clock=time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}  # (account_id, folder) -> {header: [expiry, ...]}

    @staticmethod
    def _key(account_id, folder):
        return (account_id, (folder or "").casefold())

    def add(self, account_id, folder, message_id_headers):
        expiry = self._clock() + self._ttl
        with self._lock:
            bucket = self._entries.setdefault(self._key(account_id, folder), {})
            for header in map(canonical_message_id, message_id_headers):
                if header:
                    bucket.setdefault(header, []).append(expiry)

    def filter_new(self, account_id, folder, envelopes):
        """`envelopes` minus the ones that were expected, consuming one
        expectation per match."""
        now = self._clock()
        key = self._key(account_id, folder)
        with self._lock:
            bucket = self._entries.get(key)
            if not bucket:
                return list(envelopes)
            for header in list(bucket):
                bucket[header] = [expiry for expiry in bucket[header] if expiry > now]
                if not bucket[header]:
                    del bucket[header]
            kept = []
            for envelope in envelopes:
                header = canonical_message_id((envelope or {}).get("message-id"))
                pending = bucket.get(header) if header else None
                if pending:
                    pending.pop(0)
                    if not pending:
                        del bucket[header]
                    continue
                kept.append(envelope)
            if not bucket:
                del self._entries[key]
            return kept
