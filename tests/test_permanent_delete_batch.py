"""
Batched Delete and Shift+Delete (session s): one Himalaya move per
source folder, one delete per chunk, and a Trash match that can only
ever pick the copy the batch just moved there.

The match rule is why this file exists. Trash can already hold an
older message with the same Message-ID -- an earlier accidental
Delete, a copy sent to yourself, a mailing-list duplicate. The old
code built a Message-ID -> id map from a newest-first listing, so
the last, oldest copy won: the recoverable one was destroyed and the
one just moved stayed. Only a UID above Trash's highest UID from
before the move can be the message just moved, because IMAP never
reuses a UID within a folder.

Himalaya and the pooled connection are mocked; what is under test is
the orchestration and the matching, not Himalaya itself.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc
import imap_body_fetch as ibf
from paths import Paths


def _env(uid, header):
    return {"id": str(uid), "message-id": header}


class _Patched(unittest.TestCase):
    """No cache files and no offline copies are touched."""

    def setUp(self):
        for name in ("_invalidate_cached_message", "forget_offline_copies", "_pause_before_recheck"):
            patcher = mock.patch.object(hc, name)
            patcher.start()
            self.addCleanup(patcher.stop)


class MoveMessages(_Patched):
    def test_one_call_for_the_whole_batch(self):
        with mock.patch.object(hc, "_run") as run:
            hc.move_messages("paths", "acct", ["1", "2", "3"], "INBOX", "Trash")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[2],
            ["message", "move", "1", "2", "3", "--from", "INBOX", "--to", "Trash"],
        )

    def test_large_batches_are_chunked(self):
        ids = [str(n) for n in range(1, 1201)]
        with mock.patch.object(hc, "_run") as run:
            hc.move_messages("paths", "acct", ids, "INBOX", "Trash")
        self.assertEqual(run.call_count, 3)
        sent = [arg for call in run.call_args_list for arg in call.args[2][2:-4]]
        self.assertEqual(sent, ids)

    def test_cache_is_invalidated_before_the_move(self):
        order = []
        hc._invalidate_cached_message.side_effect = lambda *a, **k: order.append("invalidate")
        with mock.patch.object(hc, "_run", side_effect=lambda *a, **k: order.append("move")):
            hc.move_messages("paths", "acct", ["1", "2"], "INBOX", "Trash")
        self.assertEqual(order, ["invalidate", "invalidate", "move"])


class MoveMessagesBatch(_Patched):
    """The shared move step behind Delete, Archive, the junk actions,
    Move To and Undo."""

    def test_one_move_per_source_folder_into_the_destination(self):
        items = [("1", "INBOX", "<a@x>"), ("4", "Work", "<b@x>"), ("2", "INBOX", "<c@x>")]
        with mock.patch.object(hc, "folder_uids", return_value=set()), \
                mock.patch.object(hc, "move_messages") as move:
            moved, failed = hc.move_messages_batch("paths", "acct", items, "Archive")
        self.assertEqual(failed, [])
        self.assertEqual(sorted(item[0] for item in moved), ["1", "2", "4"])
        calls = sorted((call.args[2], call.args[3], call.args[4]) for call in move.call_args_list)
        self.assertEqual(calls, [(["1", "2"], "INBOX", "Archive"), (["4"], "Work", "Archive")])

    def test_what_is_still_in_the_source_folder_did_not_move(self):
        items = [("1", "INBOX", "<a@x>"), ("2", "INBOX", "<b@x>")]
        with mock.patch.object(hc, "folder_uids", return_value={2}), \
                mock.patch.object(hc, "move_messages"):
            moved, failed = hc.move_messages_batch("paths", "acct", items, "Junk")
        self.assertEqual([item[0] for item in moved], ["1"])
        self.assertEqual([item[0] for item in failed], ["2"])

    def test_an_errored_move_with_an_unreadable_source_fails_the_group(self):
        items = [("1", "INBOX", "<a@x>")]
        with mock.patch.object(hc, "folder_uids", side_effect=hc.HimalayaError("down")), \
                mock.patch.object(hc, "move_messages", side_effect=hc.HimalayaError("no")):
            moved, failed = hc.move_messages_batch("paths", "acct", items, "Junk")
        self.assertEqual(moved, [])
        self.assertEqual(failed, items)

    def test_offline_copies_go_only_for_what_moved(self):
        items = [("1", "INBOX", "<a@x>"), ("2", "INBOX", "<b@x>")]
        with mock.patch.object(hc, "folder_uids", return_value={2}), \
                mock.patch.object(hc, "move_messages"):
            hc.move_messages_batch("paths", "acct", items, "Archive")
        hc.forget_offline_copies.assert_called_once_with("paths", "acct", "INBOX", ["<a@x>"])

    def test_a_message_still_listed_right_after_a_good_move_gets_a_second_look(self):
        # Gmail, live: the first re-read still listed a message that had
        # already reached Inbox.
        items = [("1", "[Gmail]/Spam", "<a@x>"), ("2", "[Gmail]/Spam", "<b@x>")]
        with mock.patch.object(hc, "folder_uids", side_effect=[{2}, set()]) as uids, \
                mock.patch.object(hc, "move_messages"):
            moved, failed = hc.move_messages_batch("paths", "acct", items, "INBOX")
        self.assertEqual(failed, [])
        self.assertEqual([item[0] for item in moved], ["1", "2"])
        self.assertEqual(uids.call_count, 2)
        hc._pause_before_recheck.assert_called_once()

    def test_no_second_look_after_a_move_that_errored(self):
        items = [("1", "INBOX", "<a@x>")]
        with mock.patch.object(hc, "folder_uids", return_value={1}) as uids, \
                mock.patch.object(hc, "move_messages", side_effect=hc.HimalayaError("no")):
            moved, failed = hc.move_messages_batch("paths", "acct", items, "Junk")
        self.assertEqual((moved, failed), ([], items))
        self.assertEqual(uids.call_count, 1)
        hc._pause_before_recheck.assert_not_called()


class TrashMessagesBatch(_Patched):
    def test_reports_the_trash_mark_and_what_moved(self):
        uids = {"Trash": {5, 9}, "INBOX": {2}}
        items = [("1", "INBOX", "<a@x>"), ("2", "INBOX", "<b@x>"), ("3", "INBOX", "<c@x>")]
        with mock.patch.object(hc, "folder_uids", side_effect=lambda p, a, f: uids[f]), \
                mock.patch.object(hc, "move_messages") as move:
            pre_max, moved, failed = hc.trash_messages_batch("paths", "acct", items, "Trash")
        self.assertEqual(pre_max, 9)
        self.assertEqual([item[0] for item in moved], ["1", "3"])
        self.assertEqual([item[0] for item in failed], ["2"])
        move.assert_called_once_with("paths", "acct", ["1", "2", "3"], "INBOX", "Trash")

    def test_one_move_per_source_folder(self):
        items = [("1", "INBOX", "<a@x>"), ("4", "Archive", "<b@x>"), ("2", "INBOX", "<c@x>")]
        with mock.patch.object(hc, "folder_uids", return_value=set()), \
                mock.patch.object(hc, "move_messages") as move:
            hc.trash_messages_batch("paths", "acct", items, "Trash")
        self.assertEqual(sorted(call.args[3] for call in move.call_args_list), ["Archive", "INBOX"])

    def test_an_empty_trash_has_a_mark_of_zero(self):
        with mock.patch.object(hc, "folder_uids", return_value=set()), \
                mock.patch.object(hc, "move_messages"):
            pre_max, _moved, _failed = hc.trash_messages_batch(
                "paths", "acct", [("1", "INBOX", "<a@x>")], "Trash",
            )
        self.assertEqual(pre_max, 0)

    def test_unreadable_trash_moves_nothing(self):
        with mock.patch.object(hc, "folder_uids", side_effect=hc.HimalayaError("down")), \
                mock.patch.object(hc, "move_messages") as move:
            with self.assertRaises(hc.HimalayaError):
                hc.trash_messages_batch("paths", "acct", [("1", "INBOX", "<a@x>")], "Trash")
        move.assert_not_called()

    def test_a_failed_move_is_judged_by_what_is_still_in_the_folder(self):
        uids = {"Trash": set(), "INBOX": {1, 2}}
        with mock.patch.object(hc, "folder_uids", side_effect=lambda p, a, f: uids[f]), \
                mock.patch.object(hc, "move_messages", side_effect=hc.HimalayaError("no")):
            _pre_max, moved, failed = hc.trash_messages_batch(
                "paths", "acct", [("1", "INBOX", "<a@x>"), ("2", "INBOX", "<b@x>")], "Trash",
            )
        self.assertEqual(moved, [])
        self.assertEqual([item[0] for item in failed], ["1", "2"])


class PurgeMovedFromTrash(_Patched):
    def _purge(self, moved, listing, pre_max=9, report=None):
        with mock.patch.object(hc, "list_envelopes", return_value=listing) as list_env, \
                mock.patch.object(
                    hc, "_run_with_folder_flag",
                    return_value=report if report is not None else {"action": "deleted"},
                ) as delete:
            unresolved = hc.purge_moved_from_trash("paths", "acct", moved, "Trash", pre_max)
        return unresolved, delete, list_env

    @staticmethod
    def _deleted_ids(delete):
        return [arg for call in delete.call_args_list for arg in call.args[2][2:]]

    def test_an_older_copy_with_the_same_message_id_is_never_touched(self):
        # uid 7 was in Trash before the move; uid 12 is the copy just moved.
        unresolved, delete, _ = self._purge(
            [("1", "INBOX", "<a@x>")], [_env(12, "<a@x>"), _env(7, "<a@x>")],
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(self._deleted_ids(delete), ["12"])

    def test_the_older_copy_is_never_used_as_a_stand_in(self):
        unresolved, delete, _ = self._purge([("1", "INBOX", "<a@x>")], [_env(7, "<a@x>")])
        delete.assert_not_called()
        self.assertEqual(unresolved, [("1", "INBOX", "not_found")])

    def test_message_ids_match_regardless_of_brackets_and_case(self):
        unresolved, delete, _ = self._purge(
            [("1", "INBOX", "<ABC@Example.com>")], [_env(10, "abc@example.com")],
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(self._deleted_ids(delete), ["10"])

    def test_the_same_message_twice_in_one_batch_takes_two_new_copies(self):
        moved = [("1", "INBOX", "<a@x>"), ("5", "Archive", "<a@x>")]
        unresolved, delete, _ = self._purge(
            moved, [_env(11, "<a@x>"), _env(12, "<a@x>"), _env(3, "<a@x>")],
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(sorted(self._deleted_ids(delete)), ["11", "12"])

    def test_one_delete_call_for_the_whole_batch(self):
        moved = [(str(n), "INBOX", "<m%d@x>" % n) for n in range(1, 6)]
        listing = [_env(20 + n, "<m%d@x>" % n) for n in range(1, 6)]
        unresolved, delete, _ = self._purge(moved, listing)
        self.assertEqual(unresolved, [])
        delete.assert_called_once()
        self.assertEqual(delete.call_args.args[3], "Trash")

    def test_missing_header_is_reported_without_listing_or_deleting(self):
        unresolved, delete, list_env = self._purge([("1", "INBOX", None)], [])
        self.assertEqual(unresolved, [("1", "INBOX", "no_header")])
        list_env.assert_not_called()
        delete.assert_not_called()

    def test_a_short_first_read_is_retried_wider(self):
        with mock.patch.object(hc, "list_envelopes", side_effect=[[], [_env(15, "<a@x>")]]) as list_env, \
                mock.patch.object(hc, "_run_with_folder_flag", return_value={"action": "deleted"}):
            unresolved = hc.purge_moved_from_trash(
                "paths", "acct", [("1", "INBOX", "<a@x>")], "Trash", 9,
            )
        self.assertEqual(list_env.call_count, 2)
        self.assertEqual(list_env.call_args_list[1].kwargs["page_size"], 2000)
        self.assertEqual(unresolved, [])

    def test_a_server_that_only_flags_is_reported(self):
        unresolved, _delete, _ = self._purge(
            [("1", "INBOX", "<a@x>")], [_env(12, "<a@x>")],
            report={"action": "flagged", "count": 1},
        )
        self.assertEqual(unresolved, [("1", "INBOX", "flagged_only")])

    def test_a_failed_delete_is_reported_as_purge_failed(self):
        with mock.patch.object(hc, "list_envelopes", return_value=[_env(12, "<a@x>")]), \
                mock.patch.object(hc, "_run_with_folder_flag", side_effect=hc.HimalayaError("no")):
            unresolved = hc.purge_moved_from_trash(
                "paths", "acct", [("1", "INBOX", "<a@x>")], "Trash", 9,
            )
        self.assertEqual(unresolved, [("1", "INBOX", "purge_failed")])

    def test_offline_copies_go_only_for_what_was_removed(self):
        self._purge([("1", "INBOX", "<a@x>")], [_env(12, "<a@x>")])
        hc.forget_offline_copies.assert_called_once_with("paths", "acct", "Trash", ["<a@x>"])


class PermanentlyDeleteMessages(_Patched):
    def test_already_in_trash_is_one_direct_delete(self):
        items = [("1", "Trash", "<a@x>"), ("2", "Trash", "<b@x>")]
        with mock.patch.object(hc, "_run_with_folder_flag", return_value={"action": "deleted"}) as delete, \
                mock.patch.object(hc, "trash_messages_batch") as step_one:
            unresolved = hc.permanently_delete_messages("paths", "acct", items, "Trash")
        self.assertEqual(unresolved, [])
        step_one.assert_not_called()
        delete.assert_called_once()
        self.assertEqual(delete.call_args.args[2], ["message", "delete", "1", "2"])

    def test_elsewhere_runs_both_steps_and_reports_what_did_not_move(self):
        items = [("1", "INBOX", "<a@x>"), ("2", "INBOX", "<b@x>")]
        with mock.patch.object(hc, "trash_messages_batch", return_value=(9, [items[0]], [items[1]])), \
                mock.patch.object(hc, "purge_moved_from_trash", return_value=[]) as step_two:
            unresolved = hc.permanently_delete_messages("paths", "acct", items, "Trash")
        step_two.assert_called_once_with("paths", "acct", [items[0]], "Trash", 9)
        self.assertEqual(unresolved, [("2", "INBOX", "move_failed")])

    def test_step_one_failing_outright_reports_every_message_as_not_moved(self):
        with mock.patch.object(hc, "trash_messages_batch", side_effect=hc.HimalayaError("down")), \
                mock.patch.object(hc, "purge_moved_from_trash") as step_two:
            unresolved = hc.permanently_delete_messages(
                "paths", "acct", [("1", "INBOX", "<a@x>")], "Trash",
            )
        step_two.assert_not_called()
        self.assertEqual(unresolved, [("1", "INBOX", "move_failed")])


class PermanentlyDeleteMessageSingle(_Patched):
    def _message_for(self, reason):
        with mock.patch.object(hc, "permanently_delete_messages", return_value=[("1", "INBOX", reason)]):
            with self.assertRaises(hc.HimalayaError) as ctx:
                hc.permanently_delete_message(
                    "paths", "acct", "1", "INBOX", "Trash", message_id_header="<a@x>",
                )
        return str(ctx.exception)

    def test_success_raises_nothing(self):
        with mock.patch.object(hc, "permanently_delete_messages", return_value=[]) as batch:
            hc.permanently_delete_message(
                "paths", "acct", "1", "INBOX", "Trash", message_id_header="<a@x>",
            )
        batch.assert_called_once_with("paths", "acct", [("1", "INBOX", "<a@x>")], "Trash")

    def test_each_reason_says_where_the_message_is(self):
        self.assertIn("no Message-ID header", self._message_for("no_header"))
        self.assertIn("could not be found there", self._message_for("not_found"))
        self.assertIn("still in its original folder", self._message_for("move_failed"))
        self.assertIn("still in Trash", self._message_for("flagged_only"))
        self.assertIn("still in Trash", self._message_for("purge_failed"))


class EmptyJunkPath(_Patched):
    ENVELOPES = [{"id": "1", "message-id": "<a@x>"}, {"id": "2", "message-id": "<b@x>"}]

    def test_uses_both_steps_and_keeps_its_two_part_result(self):
        items = [("1", "Junk", "<a@x>"), ("2", "Junk", "<b@x>")]
        with mock.patch.object(hc, "trash_messages_batch", return_value=(9, [items[0]], [items[1]])) as step_one, \
                mock.patch.object(hc, "purge_moved_from_trash", return_value=[]) as step_two:
            unresolved = hc.purge_folder("paths", "acct", "Junk", "Trash", self.ENVELOPES)
        step_one.assert_called_once_with("paths", "acct", items, "Trash")
        step_two.assert_called_once_with("paths", "acct", [items[0]], "Trash", 9)
        self.assertEqual(unresolved, [("2", "move_failed")])

    def test_nothing_moved_at_all_raises(self):
        with mock.patch.object(hc, "trash_messages_batch", return_value=(0, [], [("1", "Junk", "<a@x>")])):
            with self.assertRaises(hc.HimalayaError):
                hc.purge_folder("paths", "acct", "Junk", "Trash", self.ENVELOPES[:1])

    def test_no_whole_trash_expunge(self):
        with mock.patch.object(hc, "trash_messages_batch", return_value=(9, [("1", "Junk", "<a@x>")], [])), \
                mock.patch.object(hc, "purge_moved_from_trash", return_value=[]), \
                mock.patch.object(hc, "_run") as run:
            hc.purge_folder("paths", "acct", "Junk", "Trash", self.ENVELOPES[:1])
        run.assert_not_called()


class FolderUids(unittest.TestCase):
    def test_the_pooled_connection_noops_before_searching(self):
        calls = []

        class _Client:
            def noop(self):
                calls.append("noop")

            def search(self, criteria):
                calls.append(("search", criteria))
                return [9, 3, 5]

        connection = ibf._PooledConnection(account=None, paths=None)
        connection._client = _Client()
        connection._ensure = lambda folder: None
        self.assertEqual(connection.folder_uids("Trash"), [3, 5, 9])
        self.assertEqual(calls, ["noop", ("search", ["ALL"])])

    def test_the_folder_is_selected_afresh_even_when_already_selected(self):
        class _Client:
            def noop(self):
                pass

            def search(self, criteria):
                return [1]

        seen = []
        connection = ibf._PooledConnection(account=None, paths=None)
        connection._client = _Client()
        connection._folder = "[Gmail]/Spam"
        connection._ensure = lambda folder: seen.append(connection._folder)
        connection.folder_uids("[Gmail]/Spam")
        self.assertEqual(seen, [None])

    def test_subprocess_fallback_when_the_pool_is_unavailable(self):
        with mock.patch.object(ibf, "folder_uids", side_effect=ibf.ImapBodyFetchUnavailable("no")), \
                mock.patch.object(hc, "_run", return_value=[{"id": "4"}, {"id": "x"}, {"id": "7"}]) as run:
            self.assertEqual(hc.folder_uids("paths", "acct", "Trash"), {4, 7})
        self.assertEqual(run.call_args.args[2][:3], ["envelope", "list", "-m"])


class ForgetOfflineCopies(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = self._tmp.name
        self.paths = Paths(
            base=root, app=root, apps_files=root, himalaya=root, data=root,
            userdata=os.path.join(root, "userdata"), cache=os.path.join(root, "cache"),
            config=os.path.join(root, "config"), logs=os.path.join(root, "logs"),
        )
        self.account = mock.Mock(account_id="acct1")

    def _write(self, name, header):
        directory = os.path.join(self._tmp.name, "cache", "acct1", "offline_mail", "INBOX", "cur")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(("Message-ID: %s\r\nSubject: s\r\n\r\nbody" % header).encode("utf-8"))
        return path

    def test_only_matching_local_copies_are_removed(self):
        gone = self._write("one", "<A@x>")
        kept = self._write("two", "<b@x>")
        removed = hc.forget_offline_copies(self.paths, self.account, "INBOX", ["<a@X>"])
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(gone))
        self.assertTrue(os.path.exists(kept))

    def test_no_offline_folder_is_a_no_op(self):
        self.assertEqual(
            hc.forget_offline_copies(self.paths, self.account, "INBOX", ["<a@x>"]), 0,
        )


if __name__ == "__main__":
    unittest.main()
