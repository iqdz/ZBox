"""
undo_manager.py -- audit finding 48's single-level undo record. Pure
logic, no wx and no himalaya_client, same reasoning as
bulk_execution.py's own tests.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from undo_manager import UndoManager, UndoableMove, UndoRecord


def move(header="msg-1@example.com", account="acct-1", from_folder="INBOX", to_folder="Archive"):
    return UndoableMove(account, header, from_folder, to_folder)


class UndoManagerBasics(unittest.TestCase):
    def test_starts_with_nothing_to_undo(self):
        manager = UndoManager()
        self.assertFalse(manager.has_undo())
        self.assertIsNone(manager.describe())
        self.assertIsNone(manager.take())

    def test_record_then_describe_and_has_undo(self):
        manager = UndoManager()
        manager.record("Archive", [move()])
        self.assertTrue(manager.has_undo())
        self.assertEqual(manager.describe(), "Archive")

    def test_take_returns_record_and_clears_it(self):
        manager = UndoManager()
        moves = [move()]
        manager.record("Archive", moves)
        record = manager.take()
        self.assertIsInstance(record, UndoRecord)
        self.assertEqual(record.label, "Archive")
        self.assertEqual(list(record.moves), moves)
        self.assertFalse(manager.has_undo())
        self.assertIsNone(manager.describe())

    def test_take_when_empty_returns_none(self):
        manager = UndoManager()
        self.assertIsNone(manager.take())

    def test_clear_drops_any_recorded_action(self):
        manager = UndoManager()
        manager.record("Archive", [move()])
        manager.clear()
        self.assertFalse(manager.has_undo())


class UndoManagerSingleLevel(unittest.TestCase):
    def test_recording_a_new_action_replaces_the_old_one(self):
        manager = UndoManager()
        manager.record("Archive", [move(header="a@example.com")])
        manager.record("Delete", [move(header="b@example.com")])
        self.assertEqual(manager.describe(), "Delete")
        record = manager.take()
        self.assertEqual(record.label, "Delete")
        self.assertEqual(len(record.moves), 1)
        self.assertEqual(record.moves[0].message_id_header, "b@example.com")


class UndoManagerHeaderFiltering(unittest.TestCase):
    """A message with no Message-ID header can never be re-found in
    its destination folder (see UndoableMove's docstring), so
    recording it would leave an undo entry that silently does
    nothing for that message."""

    def test_moves_without_a_header_are_dropped(self):
        manager = UndoManager()
        manager.record("Archive", [move(header=""), move(header=None)])
        self.assertFalse(manager.has_undo())

    def test_mixed_batch_keeps_only_moves_with_a_header(self):
        manager = UndoManager()
        with_header = move(header="a@example.com")
        without_header = move(header="")
        manager.record("Archive", [with_header, without_header])
        record = manager.take()
        self.assertEqual(record.moves, (with_header,))

    def test_empty_move_list_records_nothing(self):
        manager = UndoManager()
        manager.record("Archive", [])
        self.assertFalse(manager.has_undo())

    def test_recording_all_unusable_moves_clears_a_prior_record(self):
        # Guards against a stale, mismatched undo entry surviving an
        # action that turned out to have nothing undoable in it.
        manager = UndoManager()
        manager.record("Archive", [move(header="a@example.com")])
        manager.record("Delete", [move(header="")])
        self.assertFalse(manager.has_undo())


if __name__ == "__main__":
    unittest.main()
