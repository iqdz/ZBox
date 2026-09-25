"""
Local address book. Contacts are captured automatically from mail the
user already sent or received (matching Thunderbird's "Collected
Addresses" behavior), then offered back as autocomplete suggestions
in the compose window's To/Cc/Bcc fields. They can also be entered,
edited or removed directly, and imported/exported as vCard (.vcf) or
CSV, from the Address Book dialog (Ctrl+Shift+B).

Stored as data/userdata/contacts.json, one flat list of {"name", "email",
"phone", "notes"} keyed internally by lowercased email so the same
address seen from different messages collapses into a single entry
(phone/notes are manual-only fields -- automatic capture from mail
never has either, so they're always "" unless entered by hand or
imported). Also stores groups -- named mailing lists of member email
addresses (audit finding 50) -- alongside the contacts, in the same
file.
"""

import csv
import json
import logging
import os
import re
import time

import data_crypto

logger = logging.getLogger("zbox.contacts")

# Matches a bare address inside a "Name <addr>" chunk or a standalone
# "addr" chunk, good enough for splitting a To/Cc/Bcc field on commas
# without pulling in a full RFC 5322 address parser.
_EMAIL_RE = re.compile(r'[^\s<>",;]+@[^\s<>",;]+')


class ContactManager:
    def __init__(self, paths):
        self._file = os.path.join(paths.userdata, "contacts.json")
        # The file key is derived from the same keyset that protects
        # the account passwords, and that lives in the config folder.
        self._config = paths.config
        # Set when contacts.json exists but could not be opened.
        # _save refuses to write while it is set.
        self._load_error = None
        self._contacts = {}  # email.lower() -> {"name", "email", "phone", "notes"}
        self._groups = {}  # name.lower() -> {"name": ..., "members": [email.lower(), ...]}
        self._load()

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            data = data_crypto.read_json(self._config, self._file, default={})
        except data_crypto.EncryptedDataUnavailable as exc:
            # Fails closed. An address book that cannot be decrypted
            # must not come back as an empty one, because the next
            # automatic capture would call _save and write that empty
            # book straight over it.
            self._load_error = str(exc)
            logger.error("Could not open contacts.json: %s", exc)
            return
        except (OSError, ValueError) as exc:
            self._load_error = str(exc) or "unreadable"
            logger.exception("Could not load contacts.json.")
            return

        try:
            for entry in data.get("contacts", []):
                email = (entry.get("email") or "").strip()
                if email:
                    self._contacts[email.lower()] = {
                        "name": entry.get("name", ""),
                        "email": email,
                        "phone": entry.get("phone", ""),
                        "notes": entry.get("notes", ""),
                        "use_count": _as_int(entry.get("use_count")),
                        "last_used": _as_int(entry.get("last_used")),
                    }
            for group in data.get("groups", []):
                name = (group.get("name") or "").strip()
                if not name:
                    continue
                members = [
                    member.strip().lower()
                    for member in (group.get("members") or [])
                    if isinstance(member, str) and member.strip()
                ]
                self._groups[name.lower()] = {"name": name, "members": members}
        except (AttributeError, TypeError) as exc:
            self._load_error = str(exc) or "unexpected shape"
            logger.exception("contacts.json is not in the expected shape.")
            return

        try:
            data_crypto.encrypt_in_place(self._config, self._file)
        except data_crypto.EncryptedDataUnavailable as exc:
            logger.warning("contacts.json is still in plain text: %s", exc)

    def _save(self):
        if self._load_error:
            # The book on disk holds contacts this session never
            # managed to read. Writing memory over it would replace
            # them with the handful collected since startup.
            logger.error(
                "Not saving contacts.json: it could not be opened this "
                "session (%s).", self._load_error,
            )
            return
        try:
            data_crypto.write_json(
                self._config, self._file,
                {
                    "contacts": list(self._contacts.values()),
                    "groups": list(self._groups.values()),
                },
            )
        except (OSError, data_crypto.EncryptedDataUnavailable):
            logger.exception("Could not save contacts.json.")

    def add(self, name, email, phone="", notes=""):
        """Soft-merge add used by automatic capture and vCard/CSV
        import. A blank incoming name/phone/notes never overwrites a
        non-blank value already on file, so e.g. a bare address seen
        later (a Cc with no display name) can't erase a name learned
        earlier from a proper From header. Returns True if this
        created a new contact, False if an existing one was matched
        (which may still have been updated) -- audit finding 50's
        "Add Sender to Address Book" uses this to say which happened."""
        email = (email or "").strip()
        if not email:
            return False
        name = (name or "").strip()
        phone = (phone or "").strip()
        notes = (notes or "").strip()
        key = email.lower()
        existing = self._contacts.get(key)
        if existing is not None:
            changed = False
            for field, value in (("name", name), ("phone", phone), ("notes", notes)):
                if value and not existing.get(field):
                    existing[field] = value
                    changed = True
            if changed:
                self._save()
            return False
        self._contacts[key] = {
            "name": name, "email": email, "phone": phone, "notes": notes,
            "use_count": 0, "last_used": 0,
        }
        self._save()
        return True

    def record_use(self, email):
        """Counts one message actually addressed to this contact, and
        when. Both feed autocomplete ranking (see search).

        Only sending counts. Thunderbird ranks its own recipient
        autocomplete on a popularity counter of the same shape, with
        a long-standing request to weigh recency alongside it; the
        two values are stored here so search can do both from the
        start rather than needing a second migration later.
        """
        contact = self._contacts.get((email or "").strip().lower())
        if contact is None:
            return
        contact["use_count"] = _as_int(contact.get("use_count")) + 1
        contact["last_used"] = int(time.time())
        self._save()

    def upsert(self, name, email, previous_email=None, phone="", notes=""):
        """Explicit create/edit from the Address Book dialog. Unlike
        add(), this always sets every field exactly as given,
        including blanking one out. previous_email, when given and
        different from email, removes the old entry -- covers editing
        a contact's email address itself, not just its other fields."""
        email = (email or "").strip()
        if not email:
            return
        name = (name or "").strip()
        phone = (phone or "").strip()
        notes = (notes or "").strip()
        if previous_email:
            previous_key = previous_email.strip().lower()
            if previous_key != email.lower():
                self._contacts.pop(previous_key, None)
        # Usage history survives an edit: renaming someone, or fixing
        # a typo in their address, is not a reason to forget that
        # they are the person you write to most.
        existing = self._contacts.get(email.lower(), {})
        self._contacts[email.lower()] = {
            "name": name, "email": email, "phone": phone, "notes": notes,
            "use_count": _as_int(existing.get("use_count")),
            "last_used": _as_int(existing.get("last_used")),
        }
        self._save()

    def remove(self, email):
        """Removes the contact and drops it out of every group's
        membership too -- a group only ever references live
        contacts, it never keeps its own shadow copy of one."""
        key = (email or "").strip().lower()
        if key not in self._contacts:
            return
        del self._contacts[key]
        for group in self._groups.values():
            if key in group["members"]:
                group["members"].remove(key)
        self._save()

    # --- Groups (mailing lists) --------------------------------------

    def add_group(self, name):
        """Creates an empty group if one with this name (case-
        insensitive) doesn't already exist. Returns the name on file
        either way -- the new one, or the existing group's own
        casing if it already existed."""
        name = (name or "").strip()
        if not name:
            return None
        key = name.lower()
        if key not in self._groups:
            self._groups[key] = {"name": name, "members": []}
            self._save()
        return self._groups[key]["name"]

    def rename_group(self, old_name, new_name):
        """Returns False (no-op) when old_name doesn't exist, or when
        new_name collides with a different existing group -- the
        caller is expected to tell the user rather than silently
        losing one of the two groups."""
        old_key = (old_name or "").strip().lower()
        new_name = (new_name or "").strip()
        if not new_name or old_key not in self._groups:
            return False
        new_key = new_name.lower()
        if new_key != old_key and new_key in self._groups:
            return False
        group = self._groups.pop(old_key)
        group["name"] = new_name
        self._groups[new_key] = group
        self._save()
        return True

    def remove_group(self, name):
        """Deletes the group itself; its members' own contact entries
        are untouched, same as removing a mailing list doesn't delete
        the people on it."""
        key = (name or "").strip().lower()
        if key in self._groups:
            del self._groups[key]
            self._save()

    def all_groups(self):
        return sorted(self._groups.values(), key=lambda group: group["name"].lower())

    def search_groups(self, prefix):
        """Prefix match on group name, case-insensitive -- the
        mailing-list half of compose autocomplete (see
        compose_panel._ContactCompleter)."""
        prefix = (prefix or "").strip().lower()
        if not prefix:
            return []
        matches = [
            group for group in self._groups.values()
            if group["name"].lower().startswith(prefix)
        ]
        matches.sort(key=lambda group: group["name"].lower())
        return matches

    def group_members(self, name):
        """Contact dicts for this group's members, in the group's own
        member order, silently skipping any member whose contact was
        since deleted (see remove()'s own cleanup -- this is a second
        line of defense, not the normal path)."""
        key = (name or "").strip().lower()
        group = self._groups.get(key)
        if group is None:
            return []
        return [self._contacts[email] for email in group["members"] if email in self._contacts]

    def set_group_members(self, name, emails):
        """Replaces a group's whole membership list at once."""
        key = (name or "").strip().lower()
        if key not in self._groups:
            return
        self._groups[key]["members"] = [
            email.strip().lower() for email in emails if (email or "").strip()
        ]
        self._save()

    def add_to_group(self, name, email):
        key = (name or "").strip().lower()
        email_key = (email or "").strip().lower()
        if key not in self._groups or not email_key:
            return
        if email_key not in self._groups[key]["members"]:
            self._groups[key]["members"].append(email_key)
            self._save()

    def remove_from_group(self, name, email):
        key = (name or "").strip().lower()
        email_key = (email or "").strip().lower()
        if key in self._groups and email_key in self._groups[key]["members"]:
            self._groups[key]["members"].remove(email_key)
            self._save()

    def add_from_envelope_from(self, sender_list):
        """sender_list: an envelope's 'from' field, a list of
        {"name", "email"} dicts as returned by Himalaya."""
        if not isinstance(sender_list, list):
            return
        for sender in sender_list:
            if isinstance(sender, dict):
                self.add(sender.get("name", ""), sender.get("email", ""))

    def add_from_address_field(self, field_value, record_use=False):
        """field_value: a raw To/Cc/Bcc string, e.g.
        'Jane Doe <jane@x.com>, bob@y.com'.

        record_use=True means this field was just sent to, so each
        address in it also counts toward autocomplete ranking. The
        default is False because this is also used to collect
        addresses off mail that merely arrived, which says nothing
        about who the reader writes to.
        """
        if not field_value:
            return
        for chunk in field_value.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            match = _EMAIL_RE.search(chunk)
            if not match:
                continue
            email = match.group(0)
            name = chunk[: match.start()].strip(' "<>')
            self.add(name, email)
            if record_use:
                self.record_use(email)

    def all_contacts(self):
        return sorted(
            self._contacts.values(),
            key=lambda c: (c.get("name") or c.get("email")).lower(),
        )

    def search(self, prefix, limit=10):
        """Matches for compose autocomplete, most-used first.

        Two changes from a plain alphabetical prefix match, both
        because alphabetical order is the wrong answer to "who do you
        mean": the person written to most often should be the first
        thing offered, and typing a surname should find someone whose
        entry starts with their first name.

        Matching: the typed text against the start of the name, the
        start of any word in the name, the start of the address, and
        the start of its local part -- so "smith", "john" and "jsmith"
        all reach John Smith <jsmith@example.com>.

        Ranking: how often this address has been sent to, plus a
        small bonus for having been used recently. Thunderbird ranks
        the same list on a popularity counter alone and has an open
        request to weigh recency into it as well; doing both here
        costs one stored timestamp. Ties fall back to alphabetical,
        so a fresh address book behaves exactly as it did before any
        of this existed.
        """
        prefix = (prefix or "").strip().lower()
        if not prefix:
            return []
        matches = [
            contact for contact in self._contacts.values()
            if _contact_matches(contact, prefix)
        ]
        matches.sort(key=_autocomplete_rank)
        return matches[:limit]

    # --- Import / export ------------------------------------------

    def import_vcard(self, file_path):
        """
        Imports contacts from a vCard 3.0/4.0 (.vcf) file, the format
        Outlook, Apple Contacts, Google Contacts and Thunderbird all
        offer as an export option. One address-book entry is created
        per EMAIL line found (this address book is one row per email
        address, not per person), each taking the most recent FN
        (formatted name), TEL and NOTE line seen so far in that
        VCARD block -- so a card listing TEL/NOTE after its EMAIL
        line won't attach them to that address (export_vcard below
        always writes them first, for exactly this reason). Existing
        entries are soft-merged via add(), so re-importing the same
        file never blanks out a name/phone/notes edited locally
        since. Returns the number of email addresses imported.
        """
        with open(file_path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()

        imported = 0
        current_name = ""
        current_phone = ""
        current_notes = ""
        in_card = False
        for line in _unfold_vcard_lines(text):
            stripped = line.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper == "BEGIN:VCARD":
                in_card = True
                current_name = ""
                current_phone = ""
                current_notes = ""
                continue
            if upper == "END:VCARD":
                in_card = False
                continue
            if not in_card or ":" not in stripped:
                continue

            prop, _, value = stripped.partition(":")
            # Strip parameters (e.g. "EMAIL;TYPE=INTERNET" ->
            # "EMAIL") and any "item1." group prefix some exporters
            # (notably Apple's) add.
            prop_name = prop.split(";")[0].split(".")[-1].upper()
            value = _unescape_vcard_value(value)

            if prop_name == "FN":
                current_name = value
            elif prop_name == "TEL":
                current_phone = value
            elif prop_name == "NOTE":
                current_notes = value
            elif prop_name == "EMAIL" and value.strip():
                self.add(current_name, value.strip(), phone=current_phone, notes=current_notes)
                imported += 1

        return imported

    def export_vcard(self, file_path):
        """Writes every contact as a vCard 3.0 file, one VCARD block
        per email address -- readable by Outlook, Apple Contacts,
        Google Contacts and Thunderbird's own vCard importer."""
        lines = []
        for contact in self.all_contacts():
            email = contact.get("email", "")
            name = contact.get("name") or email
            lines.append("BEGIN:VCARD")
            lines.append("VERSION:3.0")
            lines.append(f"FN:{_escape_vcard_value(name)}")
            # TEL/NOTE before EMAIL, matching FN's own position above:
            # import_vcard applies whatever FN/TEL/NOTE it has seen so
            # far to the next EMAIL line in the block (its own
            # documented "most recent value seen" rule), so a vCard
            # this method writes itself must put them first to read
            # back correctly.
            if contact.get("phone"):
                lines.append(f"TEL:{_escape_vcard_value(contact['phone'])}")
            if contact.get("notes"):
                lines.append(f"NOTE:{_escape_vcard_value(contact['notes'])}")
            lines.append(f"EMAIL;TYPE=INTERNET:{_escape_vcard_value(email)}")
            lines.append("END:VCARD")
        with open(file_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\r\n".join(lines) + ("\r\n" if lines else ""))

    def import_csv(self, file_path):
        """
        Imports contacts from a CSV file. Looks for a header row
        with a column containing 'mail' (matches Name/Email,
        First Name/E-mail Address, Email 1 - Value and similar
        vendor variations from Outlook/Google/Thunderbird exports),
        a column containing 'name' for the display name, a column
        containing 'phone' and one containing 'note'. If no
        recognizable header is found, the first row is treated as
        data with column 0 as name and column 1 as email (no phone/
        notes in that fallback shape -- there's nothing to
        distinguish those columns by position alone). Returns the
        number of rows imported.
        """
        with open(file_path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            return 0

        header = [cell.strip().lower() for cell in rows[0]]
        email_idx = next((i for i, cell in enumerate(header) if "mail" in cell), None)
        name_idx = next((i for i, cell in enumerate(header) if "name" in cell), None)
        phone_idx = next((i for i, cell in enumerate(header) if "phone" in cell), None)
        notes_idx = next((i for i, cell in enumerate(header) if "note" in cell), None)

        if email_idx is not None:
            data_rows = rows[1:]
        else:
            # No recognizable header; assume the file starts straight
            # into data with name in column 0, email in column 1.
            email_idx = 1
            name_idx = 0
            data_rows = rows

        def cell(row, idx):
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        imported = 0
        for row in data_rows:
            if email_idx >= len(row):
                continue
            email = row[email_idx].strip()
            if not email:
                continue
            self.add(cell(row, name_idx), email, phone=cell(row, phone_idx), notes=cell(row, notes_idx))
            imported += 1
        return imported

    def export_csv(self, file_path):
        """Writes Name/Email/Phone/Notes columns, the same shape
        Outlook, Google Contacts and Thunderbird all accept on
        re-import (with Outlook and Google needing their own column-
        mapping step, same as importing any foreign CSV into either
        of them)."""
        with open(file_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Name", "Email", "Phone", "Notes"])
            for contact in self.all_contacts():
                writer.writerow([
                    contact.get("name", ""), contact.get("email", ""),
                    contact.get("phone", ""), contact.get("notes", ""),
                ])


def _as_int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _contact_matches(contact, prefix):
    """Whether the typed text starts the name, any word of the name,
    the address, or the address's local part."""
    name = (contact.get("name") or "").lower()
    email = (contact.get("email") or "").lower()
    if name.startswith(prefix) or email.startswith(prefix):
        return True
    if email.partition("@")[0].startswith(prefix):
        return True
    return any(word.startswith(prefix) for word in name.replace(",", " ").split())


def _autocomplete_rank(contact):
    """Sort key: most-used first, recent use worth a little extra,
    alphabetical to break ties. Negated because sort ascends."""
    score = _as_int(contact.get("use_count"))
    last_used = _as_int(contact.get("last_used"))
    if last_used:
        days = (time.time() - last_used) / 86400.0
        if days < 7:
            score += 3
        elif days < 30:
            score += 2
        elif days < 180:
            score += 1
    return (-score, (contact.get("name") or contact.get("email") or "").lower())


def _unfold_vcard_lines(text):
    """RFC 6350 line unfolding: a line starting with a space or tab
    is a continuation of the previous line, not a new property."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _escape_vcard_value(value):
    return (value or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _unescape_vcard_value(value):
    result = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            result.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def format_contact(contact):
    """'Name <email>' when a name is on file, otherwise the bare
    email address."""
    name = contact.get("name")
    email = contact.get("email", "")
    return f"{name} <{email}>" if name else email
