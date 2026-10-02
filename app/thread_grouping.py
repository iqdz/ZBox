"""
Automatic message threading: groups a folder's envelope list into
conversations using the same Message-ID / In-Reply-To linkage
message_body.thread_ids() uses against a single message's full
headers -- but read here straight off the envelope list itself, at
no extra Himalaya cost.

Feasibility note, since ZBox's standing rule requires confirming any
new Himalaya CLI/config surface via `strings himalaya/himalaya.exe`
before writing code against it, and the pinned binary is Windows-only
so this project's Linux-side tooling can inspect it but never execute
it (no live JSON capture was possible): the binary's own embedded doc
comments describe himalaya::email::envelope::Envelope -- the exact
struct `envelope list` returns, doc'd there as "Lightweight summary
of a message: enough to display in a list without fetching the full
body" -- as carrying `message-id` and `in-reply-to` fields alongside
the `id`/`subject`/`from`/`date`/`flags` ZBox already reads from
every envelope (confirmed live by existing, working code in
envelope_format.py). Per that struct's own doc comments, both are
"normalised ... so the two compare byte-for-byte and a client can
pair a reply with its parent from a listing alone", and neither is
gated behind an opt-in flag the way the same struct's `has-attachment`
field is ("depending on the backend this can cost an extra lookup per
envelope, so it is opt-in") -- meaning real Message-ID/In-Reply-To
threading should already be available from every envelope list ZBox
already fetches, with no extra subprocess call. This is the one
Phase-1 assumption that could only be confirmed by binary inspection
rather than a live run; every accessor below degrades to "no
threading data for this envelope" (a singleton thread of its own)
rather than raising, in case the field names or shape are still
slightly off in practice, and it's worth a one-time live sanity check
against a real high-traffic mailing-list folder once Phase 2 puts
this on screen.

`in-reply-to` is documented as a list ("the grammar is `1*msg-id`");
only the first entry is used as this message's parent link -- the
same simplification message_body.threading_headers already makes in
the outbound direction, and correct for the overwhelming majority of
real mail (a genuine merged-thread reply citing several parents is
rare enough that ZBox doesn't need multi-parent threads to handle it,
just a link to the first).

This module never calls Himalaya and never touches wx -- same
pure/testable bucket as envelope_format.py and message_body.py.
"""

import logging

logger = logging.getLogger("zbox.threads")

# Both spellings are tried for every field: "-" is what the pinned
# binary's own struct doc comments show (kebab-case, matching the
# already-confirmed "has-attachment" flag name), "_" is kept as a
# fallback in case a future Himalaya version or a different backend
# serializes it differently. Either way, absent means "no threading
# data", never an error.
_MESSAGE_ID_KEYS = ("message-id", "message_id")
_IN_REPLY_TO_KEYS = ("in-reply-to", "in_reply_to")

_MAX_ANCESTRY_WALK = 1000


def _normalize_id(raw):
    """Strips angle brackets and surrounding whitespace so ids
    compare equal regardless of whether either side kept them --
    envelope list's own values are documented as already normalised,
    but this costs nothing and guards against a backend that isn't."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().strip("<>").strip()


def envelope_message_id(envelope):
    """This envelope's own Message-ID, normalized, or "" if absent."""
    if not isinstance(envelope, dict):
        return ""
    for key in _MESSAGE_ID_KEYS:
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_id(value)
    return ""


def envelope_parent_id(envelope):
    """The Message-ID of the message this envelope is a direct reply
    to, normalized, or "" if this envelope carries no usable
    In-Reply-To. Only the first id is used when In-Reply-To lists
    more than one (see module docstring)."""
    if not isinstance(envelope, dict):
        return ""
    for key in _IN_REPLY_TO_KEYS:
        value = envelope.get(key)
        if isinstance(value, list):
            for item in value:
                normalized = _normalize_id(item)
                if normalized:
                    return normalized
        elif isinstance(value, str) and value.strip():
            return _normalize_id(value)
    return ""


class ThreadNode:
    """One message's position in a thread: its envelope, its parent
    node (None for a thread root), and every direct reply to it (also
    ThreadNodes) in the order they appeared in the original envelope
    list.

    A root node -- one whose message is not itself a reply to
    anything else present in this same folder listing -- represents
    a whole thread; message_count() and all_envelopes() walk the
    whole subtree under it.
    """

    __slots__ = ("envelope", "parent", "children")

    def __init__(self, envelope):
        self.envelope = envelope
        self.parent = None
        self.children = []

    def message_count(self):
        return 1 + sum(child.message_count() for child in self.children)

    def all_envelopes(self):
        """Every envelope in this thread, root first, each message
        immediately followed by its own replies (depth-first) -- the
        stable row order a caller flattens a collapsed thread into."""
        result = [self.envelope]
        for child in self.children:
            result.extend(child.all_envelopes())
        return result

    def walk(self, depth=0):
        """(node, depth) pairs in the same depth-first, root-first
        order as all_envelopes(), but keeping each node and its
        nesting depth -- for a caller (envelope_list_panel's expanded-
        thread rendering) that needs to indent replies differently
        from the root rather than just list every envelope flat."""
        yield self, depth
        for child in self.children:
            yield from child.walk(depth + 1)


def _would_create_cycle(candidate_parent, node):
    """True if attaching `node` under `candidate_parent` would make
    node its own ancestor -- candidate_parent is already node's
    descendant. Real mail should never produce this (In-Reply-To
    points backwards in time), but headers are attacker- or
    bug-controlled input, not something this module can trust; a
    bounded walk up candidate_parent's existing ancestry is cheap
    (real thread depth is nowhere near the cap). Rejecting just the
    edge that would close the loop -- whichever of a cyclic pair is
    processed second -- turns a forged or malformed chain of
    In-Reply-To headers into one valid tree (the edge processed first
    stands) instead of an infinite loop or a crash the first time
    something walks it."""
    current = candidate_parent
    steps = 0
    while current is not None and steps < _MAX_ANCESTRY_WALK:
        if current is node:
            return True
        current = current.parent
        steps += 1
    return False


def group_into_threads(envelopes):
    """
    Groups a single folder's envelope list (as returned by
    himalaya_client.list_envelopes, in whatever order/sort the caller
    already applied) into threads.

    Returns an ordered list of root ThreadNodes, one per thread, in
    the order each thread's root envelope first appears in
    `envelopes`. A message with no Message-ID, no In-Reply-To, or an
    In-Reply-To that doesn't resolve to another message actually
    present in this same listing (its parent lives on another page,
    in another folder, or was never fetched) becomes the root of its
    own single-message thread -- this never merges messages across
    folders or pages, only within the exact list handed in, mirroring
    how the message list already only ever shows one folder at a
    time.

    Deliberately does not fall back to subject-based grouping the way
    related_messages_panel's own pre-filter does -- that pre-filter
    only decides which candidates are worth an expensive header
    fetch, and accepts the occasional miss as a known tradeoff.
    Grouping the visible message list is a decision the user acts on
    directly (collapsing a thread, watching or ignoring it), so it
    only merges messages a real Message-ID/In-Reply-To link actually
    connects, never messages that merely share a subject line.
    """
    nodes = [ThreadNode(envelope) for envelope in envelopes]

    nodes_by_message_id = {}
    for node in nodes:
        message_id = envelope_message_id(node.envelope)
        if message_id and message_id not in nodes_by_message_id:
            nodes_by_message_id[message_id] = node

    roots = []
    for node in nodes:
        parent_id = envelope_parent_id(node.envelope)
        parent_node = nodes_by_message_id.get(parent_id) if parent_id else None
        if (
            parent_node is None
            or parent_node is node
            or _would_create_cycle(parent_node, node)
        ):
            roots.append(node)
        else:
            parent_node.children.append(node)
            node.parent = parent_node

    return roots


def thread_key(root_envelope):
    """Stable identity for a thread, used to persist Watched/Ignored
    state (see thread_state.py) across refreshes and re-fetches: the
    root message's own normalized Message-ID, which -- unlike
    Himalaya's own backend id -- doesn't change if the message is
    later moved or the folder is re-synced. Falls back to a
    same-session-only key built from the backend id on the rare
    message with no Message-ID at all (very old or malformed mail);
    that fallback deliberately won't survive the message being moved,
    since there is no better handle on it to survive that with.
    """
    message_id = envelope_message_id(root_envelope)
    if message_id:
        return message_id
    return "noid:%s" % (root_envelope.get("id") if isinstance(root_envelope, dict) else "")
