"""
Message filters and rules (audit finding 45): per-account, ordered
rules that test an incoming or already-received message's From, To
and Subject and, when a rule matches, mark it read, flag it and/or
move it to another folder.

Two run paths, both built on the same matching_rules() below:
  - Automatic: mail_fetch.py's own new-mail diff already knows the
    instant a genuinely new envelope shows up in an account's Inbox
    (that's what plays the new-mail sound); it hands those envelopes
    here so filters act on real incoming mail the moment it arrives,
    same as Thunderbird's own default "Getting New Mail" trigger.
  - Manual: Tools > Message Filters' own "Run Now" button (finding
    45's explicitly named second half) applies the same rules to
    every message already sitting in a chosen folder, for rules
    written after the mail they should have caught already arrived.

Pure and wx-free, matching bulk_execution.py's own reasoning: what
audit finding 45 is actually about is deciding whether a rule
matches and what it should do, and that needs no wx widget to be
correct or to test. main_frame.py supplies the wx editor dialog and
turns a matched rule's actions into real himalaya_client calls.

Conditions are scoped to From/To/Subject, deliberately not Body:
those three already sit on every envelope Himalaya hands back, so
testing a rule against however many messages are in a folder costs
nothing extra. A Body condition would mean fetching every candidate
message's full text first -- exactly the cost audit finding 44's own
"related messages" action found expensive enough to need a
pre-filter for, and here there would be no subject-equality shortcut
to lean on. Matching is a case-insensitive substring test throughout,
Thunderbird's own default operator for every field this covers.
"""

from dataclasses import dataclass, field

import junk_rules

FIELDS = ("from", "to", "subject")


def _address_list_text(addresses):
    """
    Every display name and address in an envelope's from/to list,
    space-joined, so a condition can match on either a name
    ("Newsletter" in "ZBox Newsletter <news@example.com>") or a bare
    domain/address. Envelope address entries use the "email" key (see
    message_body.py's own header-vs-envelope note); "address" is
    accepted too so a hand-built fixture or future shape change
    doesn't silently stop matching.

    A single address is accepted as well as a list: rows listed
    through Himalaya carry From as one address, while rows from the
    kept-open connection carry a list. Matching only the list shape
    made every From condition miss on Himalaya's rows.
    """
    if isinstance(addresses, dict):
        addresses = [addresses]
    if not isinstance(addresses, list):
        return ""
    parts = []
    for entry in addresses:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        email = entry.get("email") or entry.get("address")
        if name:
            parts.append(str(name))
        if email:
            parts.append(str(email))
    return " ".join(parts)


def _envelope_field_text(envelope, field_name):
    if not isinstance(envelope, dict):
        return ""
    if field_name == "subject":
        return envelope.get("subject") or ""
    if field_name in ("from", "to"):
        return _address_list_text(envelope.get(field_name))
    return ""


@dataclass
class FilterCondition:
    field: str = "from"  # one of FIELDS
    value: str = ""

    def matches(self, envelope):
        needle = (self.value or "").strip().lower()
        if not needle:
            return False  # a blank condition never matches anything
        haystack = _envelope_field_text(envelope, self.field).lower()
        return needle in haystack

    def to_dict(self):
        return {"field": self.field, "value": self.value}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        field_name = data.get("field")
        if field_name not in FIELDS:
            field_name = "from"
        return cls(field=field_name, value=data.get("value") or "")


@dataclass
class FilterRule:
    name: str = ""
    enabled: bool = True
    match_all: bool = True  # True: every condition must match (AND). False: any (OR).
    conditions: list = field(default_factory=list)  # [FilterCondition, ...]
    mark_read: bool = False
    flag: bool = False
    move_to: str = ""  # a real folder name, or "" for no move

    def has_action(self):
        return self.mark_read or self.flag or bool(self.move_to)

    def has_condition(self):
        return any((condition.value or "").strip() for condition in self.conditions)

    def matches(self, envelope):
        active = [c for c in self.conditions if (c.value or "").strip()]
        if not active:
            return False  # a rule with nothing to test on never matches
        results = (condition.matches(envelope) for condition in active)
        return all(results) if self.match_all else any(results)

    def to_dict(self):
        return {
            "name": self.name,
            "enabled": self.enabled,
            "match_all": self.match_all,
            "conditions": [c.to_dict() for c in self.conditions],
            "mark_read": self.mark_read,
            "flag": self.flag,
            "move_to": self.move_to,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        raw_conditions = data.get("conditions")
        conditions = [
            FilterCondition.from_dict(entry)
            for entry in raw_conditions
            if isinstance(entry, dict)
        ] if isinstance(raw_conditions, list) else []
        return cls(
            name=data.get("name") or "",
            enabled=bool(data.get("enabled", True)),
            match_all=bool(data.get("match_all", True)),
            conditions=conditions,
            mark_read=bool(data.get("mark_read", False)),
            flag=bool(data.get("flag", False)),
            move_to=data.get("move_to") or "",
        )


def matching_rules(rules, envelope):
    """
    Every enabled rule (kept in list order -- order is the only
    priority scheme this gives rules, same as Thunderbird's own
    default) that both has a real action configured and whose
    conditions match this envelope. A rule with no action, or no
    condition with an actual value in it (mid-edit, or someone
    cleared every field), never matches anything -- see
    FilterRule.matches/has_action -- so it can't silently do nothing
    while still counting as "handled" for a caller that stops at the
    first match.
    """
    return [
        rule for rule in (rules or [])
        if rule.enabled and rule.has_action() and rule.matches(envelope)
    ]


# --- Block This Sender ------------------------------------------------

def _sender_name(envelope):
    """The display name of an envelope's first From entry, without
    surrounding quotes, or ""."""
    sender = (envelope or {}).get("from")
    entries = sender if isinstance(sender, list) else [sender]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name"):
            return str(entry["name"]).strip().strip("\"'").strip()
    return ""


def block_sender_rule(envelope, trash_folder, own_addresses=(), name_template="Block {sender}"):
    """
    The filter that blocks the sender of one message: new Inbox mail
    from them is marked read and moved to trash_folder. Returns
    (status, rule, sender_text):

      "ok"            rule is the FilterRule, sender_text names whom it blocks
      "none"          the message has no sender address
      "own"           the sender is one of own_addresses; never blocked
      "list_unknown"  mailing-list mail that names no member

    Mailing-list mail is told apart the way junk_rules does it -- the
    sender is also a recipient, as groups.io and most list servers
    address a post -- or by a sender name containing "via", which is
    how lists rewrite From when they put their own address there
    ("Jane Doe via groups.io"). Blocking that address alone would
    block the whole list, so the rule matches the member's name and
    the list's address together, and only that member's posts to that
    list are caught. A list post with no name says nothing about who
    sent it, so it is not blocked at all.
    """
    address = junk_rules.envelope_sender(envelope)
    if not address:
        return "none", None, ""
    own = {a for a in map(junk_rules.normalize_address, own_addresses) if a}
    if address in own:
        return "own", None, address
    name = _sender_name(envelope)
    via_list = junk_rules.is_mailing_list_mail(envelope) or " via " in (" %s " % name.casefold())
    if via_list:
        if not name:
            return "list_unknown", None, address
        conditions = [FilterCondition("from", name), FilterCondition("from", address)]
        sender_text = "%s (%s)" % (name, address)
    else:
        conditions = [FilterCondition("from", address)]
        sender_text = address
    rule = FilterRule(
        name=name_template.format(sender=sender_text),
        enabled=True,
        match_all=True,
        conditions=conditions,
        mark_read=True,
        move_to=trash_folder,
    )
    return "ok", rule, sender_text


def same_block(rule, other):
    """True when two rules block the same sender into the same folder:
    the same From conditions, case aside, and the same destination."""
    def key(r):
        return (
            (r.move_to or "").casefold(),
            sorted(
                (c.field, (c.value or "").strip().casefold())
                for c in r.conditions if (c.value or "").strip()
            ),
        )
    return key(rule) == key(other)
