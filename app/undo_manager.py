"""
Audit finding 48: single-level undo for the last Delete (to Trash),
Archive, Mark as Junk or Mark as Not Junk. Deliberately single-level,
not a full undo stack -- the audit's own wording ("a single-level
undo... covers most accidents") and Thunderbird's own Ctrl+Z (also
single-level for message moves) both point the same way, and a real
stack would have to answer much harder questions about what happens
when the mailbox changes underneath a queued-up older entry.

Every one of the four actions above is, at the IMAP level, a move
from one folder to another. Permanent delete is excluded on purpose:
it already has its own separate confirmation precisely because it is
NOT reversible. A message already sitting in the destination folder
(deleting from Trash, marking a Trash message as junk, marking an
Inbox message as not-junk) is left alone by the actions themselves,
so there is nothing to record for it either.

Undoing a move means moving the message back -- but the id it was
given in the destination folder is not the id it had before the move
(audit finding 37), so what gets recorded is each message's stable
Message-ID header plus the two folder names, and undo re-finds the
message by that header once it runs (see himalaya_client.undo_moves).

This module is pure and wx-free, like bulk_execution.py, so it is
tested directly without the wx stub.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class UndoableMove:
    """One message's part of an undoable action.

    account is whatever object main_frame's bulk contexts already
    carry (an Account instance); this module never inspects it, only
    passes it through and groups by it.

    from_folder is where the message was BEFORE the action ran --
    undo moves it back there. to_folder is where the action put it --
    undo re-finds the message there by message_id_header, since its
    id in to_folder is not the id it had in from_folder.
    """

    account: object
    message_id_header: str
    from_folder: str
    to_folder: str


@dataclass(frozen=True)
class UndoRecord:
    """label is shown back to the user ("Archive" becomes the menu
    item "Undo Archive" and the status line "Undid Archive."). moves
    is the tuple of UndoableMove to reverse -- never empty; see
    UndoManager.record()."""

    label: str
    moves: tuple


class UndoManager:
    """Holds at most one UndoRecord. Recording a new action always
    replaces whatever was there before -- single-level by design."""

    def __init__(self):
        self._record = None

    def record(self, label, moves):
        """Stores moves as the new undo record, dropping any entry
        with no message_id_header (nothing to re-find that message by
        -- see UndoableMove). If nothing usable is left, clears any
        existing record instead of leaving a stale one behind: once
        an unrecordable action has run, undoing the action before it
        would silently reverse the wrong thing."""
        usable = tuple(m for m in moves if m.message_id_header)
        self._record = UndoRecord(label, usable) if usable else None

    def has_undo(self):
        return self._record is not None

    def describe(self):
        """The current record's label, or None when there is nothing
        to undo. Used to build the "Undo <label>" menu text."""
        return self._record.label if self._record else None

    def take(self):
        """Returns the current record and clears it. Call this only
        once committed to attempting the undo, so a failed or partial
        undo is never retried against mailbox state that has already
        moved on."""
        record = self._record
        self._record = None
        return record

    def clear(self):
        self._record = None
