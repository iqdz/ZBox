"""
Audit finding 48: himalaya_client.undo_moves() re-finds each message
in the folder a prior action left it in (by Message-ID header, since
that move gave it a new id there -- finding 37) and moves it back to
where it came from. Mocks list_envelopes/move_messages_batch rather
than spawning the real CLI, same reasoning and pattern as
test_permanent_delete_batch.py.

The copy-picking tests exist because undo used to move the OLDEST
same-Message-ID copy back: its header-to-id map kept the last entry
of a newest-first listing. The copy an action put in a folder got
that folder's next UID, so undo now takes the highest unclaimed one.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc
from undo_manager import UndoableMove


def _envelope(message_id_header, current_id):
    return {"id": current_id, "message-id": message_id_header}


def _all_moved(paths, account, items, to_folder):
    return list(items), []


class _Patched(unittest.TestCase):
    """move_messages_batch mocked to report every item moved."""

    def setUp(self):
        patcher = mock.patch.object(hc, "move_messages_batch", side_effect=_all_moved)
        self.batch = patcher.start()
        self.addCleanup(patcher.stop)

    def moved_ids(self):
        return [item[0] for call in self.batch.call_args_list for item in call.args[2]]


class UndoMovesBasics(_Patched):
    def test_found_message_is_moved_back(self):
        moves = [UndoableMove("acct", "<a@x>", "INBOX", "Archive")]
        with mock.patch.object(hc, "list_envelopes") as list_env:
            list_env.return_value = [_envelope("<a@x>", "7")]
            unresolved = hc.undo_moves("paths", moves)

        self.assertEqual(unresolved, [])
        list_env.assert_called_once_with(
            "paths", "acct", folder="Archive", page_size=500, backend="imap"
        )
        self.batch.assert_called_once_with(
            "paths", "acct", [("7", "Archive", "<a@x>")], "INBOX",
        )

    def test_message_not_found_is_unresolved_and_not_moved(self):
        moves = [UndoableMove("acct", "<missing@x>", "INBOX", "Archive")]
        with mock.patch.object(hc, "list_envelopes") as list_env:
            list_env.return_value = [_envelope("<someone-else@x>", "7")]
            unresolved = hc.undo_moves("paths", moves)

        self.assertEqual(unresolved, moves)
        self.batch.assert_not_called()

    def test_empty_moves_list_does_nothing(self):
        with mock.patch.object(hc, "list_envelopes") as list_env:
            unresolved = hc.undo_moves("paths", [])

        self.assertEqual(unresolved, [])
        list_env.assert_not_called()
        self.batch.assert_not_called()


class UndoMovesBatching(_Patched):
    def test_same_account_and_folder_is_listed_once_and_moved_in_one_batch(self):
        moves = [
            UndoableMove("acct", f"<m{i}@x>", "INBOX", "Archive")
            for i in range(5)
        ]
        with mock.patch.object(hc, "list_envelopes") as list_env:
            list_env.return_value = [
                _envelope(f"<m{i}@x>", str(10 + i)) for i in range(5)
            ]
            unresolved = hc.undo_moves("paths", moves)

        self.assertEqual(unresolved, [])
        list_env.assert_called_once()
        self.batch.assert_called_once()
        self.assertEqual(len(self.batch.call_args.args[2]), 5)

    def test_different_destination_folders_are_each_listed_once(self):
        moves = [
            UndoableMove("acct", "<a@x>", "INBOX", "Archive"),
            UndoableMove("acct", "<b@x>", "INBOX", "Junk"),
        ]

        def fake_list_envelopes(paths, account, folder, page_size, backend):
            if folder == "Archive":
                return [_envelope("<a@x>", "1")]
            return [_envelope("<b@x>", "2")]

        with mock.patch.object(hc, "list_envelopes", side_effect=fake_list_envelopes) as list_env:
            unresolved = hc.undo_moves("paths", moves)

        self.assertEqual(unresolved, [])
        self.assertEqual(list_env.call_count, 2)
        self.assertEqual(self.batch.call_count, 2)

    def test_different_origin_folders_get_one_move_each(self):
        moves = [
            UndoableMove("acct", "<a@x>", "INBOX", "Junk"),
            UndoableMove("acct", "<b@x>", "Work", "Junk"),
        ]
        with mock.patch.object(hc, "list_envelopes") as list_env:
            list_env.return_value = [_envelope("<a@x>", "1"), _envelope("<b@x>", "2")]
            unresolved = hc.undo_moves("paths", moves)

        self.assertEqual(unresolved, [])
        list_env.assert_called_once()
        self.assertEqual(
            sorted(call.args[3] for call in self.batch.call_args_list), ["INBOX", "Work"],
        )

    def test_different_accounts_with_the_same_folder_name_are_kept_separate(self):
        moves = [
            UndoableMove("acct-1", "<a@x>", "INBOX", "Archive"),
            UndoableMove("acct-2", "<a@x>", "INBOX", "Archive"),
        ]

        def fake_list_envelopes(paths, account, folder, page_size, backend):
            return [_envelope("<a@x>", "7")]

        with mock.patch.object(hc, "list_envelopes", side_effect=fake_list_envelopes) as list_env:
            unresolved = hc.undo_moves("paths", moves)

        self.assertEqual(unresolved, [])
        self.assertEqual(list_env.call_count, 2)
        self.assertEqual(
            {call.args[1] for call in self.batch.call_args_list}, {"acct-1", "acct-2"},
        )

    def test_one_unresolved_message_does_not_block_the_rest_of_the_batch(self):
        moves = [
            UndoableMove("acct", "<found@x>", "INBOX", "Archive"),
            UndoableMove("acct", "<missing@x>", "INBOX", "Archive"),
        ]
        with mock.patch.object(hc, "list_envelopes") as list_env:
            list_env.return_value = [_envelope("<found@x>", "7")]
            unresolved = hc.undo_moves("paths", moves)

        self.assertEqual(unresolved, [moves[1]])
        self.assertEqual(self.moved_ids(), ["7"])


class UndoMovesPicksTheRightCopy(_Patched):
    def _undo(self, moves, listing):
        with mock.patch.object(hc, "list_envelopes", return_value=listing):
            return hc.undo_moves("paths", moves)

    def test_the_newest_copy_goes_back_not_an_older_one(self):
        # uid 3 was already in Archive; uid 40 is the copy Archive put there.
        moves = [UndoableMove("acct", "<a@x>", "INBOX", "Archive")]
        self._undo(moves, [_envelope("<a@x>", "40"), _envelope("<a@x>", "3")])
        self.assertEqual(self.moved_ids(), ["40"])

    def test_listing_order_does_not_change_the_pick(self):
        moves = [UndoableMove("acct", "<a@x>", "INBOX", "Archive")]
        self._undo(moves, [_envelope("<a@x>", "3"), _envelope("<a@x>", "40")])
        self.assertEqual(self.moved_ids(), ["40"])

    def test_two_moves_with_one_header_take_two_distinct_copies(self):
        moves = [
            UndoableMove("acct", "<a@x>", "INBOX", "Archive"),
            UndoableMove("acct", "<a@x>", "Work", "Archive"),
        ]
        self._undo(moves, [_envelope("<a@x>", "3"), _envelope("<a@x>", "40"), _envelope("<a@x>", "41")])
        self.assertEqual(sorted(self.moved_ids()), ["40", "41"])

    def test_headers_match_regardless_of_brackets_and_case(self):
        moves = [UndoableMove("acct", "<ABC@Example.com>", "INBOX", "Archive")]
        unresolved = self._undo(moves, [_envelope("abc@example.com", "9")])
        self.assertEqual(unresolved, [])
        self.assertEqual(self.moved_ids(), ["9"])

    def test_a_move_back_that_did_not_land_is_unresolved(self):
        self.batch.side_effect = lambda paths, account, items, to_folder: ([], list(items))
        moves = [UndoableMove("acct", "<a@x>", "INBOX", "Archive")]
        unresolved = self._undo(moves, [_envelope("<a@x>", "7")])
        self.assertEqual(unresolved, moves)


if __name__ == "__main__":
    unittest.main()
