"""
delete_queue against a fake IMAP server: Delete to Trash, Shift+Delete
on a normal server and on Gmail, rows still showing the offline copy,
messages already gone, server refusals, network retry, and batches
carried over to the next start.
"""

import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import delete_queue
import himalaya_client
import imap_body_fetch


def _canon(value):
    return imap_body_fetch.canonical_message_id(value)


class FakeServer:
    """Folders of {uid: message-id header}, with the four pooled calls
    delete_queue uses."""

    def __init__(self, folders, uidplus=True):
        self.folders = {name: dict(messages) for name, messages in folders.items()}
        self.next_uid = 1000
        self.uidplus = uidplus
        self.refuse_move = False
        self.network_failures = 0
        self.moves = []

    def _network(self):
        if self.network_failures:
            self.network_failures -= 1
            raise imap_body_fetch.ImapBodyFetchUnavailable("[Errno 11001] getaddrinfo failed")

    def find_uids(self, paths, account, folder, header):
        self._network()
        wanted = _canon(header)
        return sorted(uid for uid, h in self.folders.get(folder, {}).items() if _canon(h) == wanted)

    def folder_uids(self, paths, account, folder):
        self._network()
        return sorted(self.folders.get(folder, {}))

    def move_uids(self, paths, account, uids, folder, to_folder):
        self._network()
        if self.refuse_move:
            raise imap_body_fetch.ImapRefused("NO [CANNOT] move refused")
        self.moves.append((list(uids), folder, to_folder))
        source = self.folders.setdefault(folder, {})
        target = self.folders.setdefault(to_folder, {})
        for uid in uids:
            if uid in source:
                self.next_uid += 1
                target[self.next_uid] = source.pop(uid)

    def delete_uids(self, paths, account, folder, uids):
        self._network()
        if not self.uidplus:
            return "flagged_only"
        for uid in uids:
            self.folders.get(folder, {}).pop(uid, None)
        return "expunged"


class QueueTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zbox_delete_queue_")
        self.paths = types.SimpleNamespace(userdata=self.tmp)
        self.account = types.SimpleNamespace(account_id="acct")
        self.forgotten = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch_server(self, server):
        patches = [
            mock.patch.object(imap_body_fetch, "find_uids", server.find_uids),
            mock.patch.object(imap_body_fetch, "folder_uids", server.folder_uids),
            mock.patch.object(imap_body_fetch, "move_uids", server.move_uids),
            mock.patch.object(imap_body_fetch, "delete_uids", server.delete_uids),
            mock.patch.object(
                himalaya_client, "forget_offline_copies",
                lambda paths, account, folder, headers: self.forgotten.append((folder, list(headers))),
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_batch(self, items, permanent, trash):
        batch = {"id": "b", "account_id": "acct", "permanent": permanent,
                 "trash_folder": trash, "items": [list(item) for item in items]}
        return delete_queue.process_batch(self.paths, self.account, batch, pause=lambda _s: None)


class ProcessBatchTests(QueueTestBase):
    def test_delete_to_trash_finds_offline_copy_row_by_message_id(self):
        server = FakeServer({"INBOX": {5: "<a@x>", 6: "<b@x>"}, "Trash": {}})
        self.patch_server(server)
        moved, unresolved = self.run_batch(
            [("1789756464.#0M3.HSPC", "INBOX", "<a@x>")], permanent=False, trash="Trash",
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(len(moved), 1)
        self.assertEqual(list(server.folders["INBOX"]), [6])
        self.assertEqual(list(server.folders["Trash"].values()), ["<a@x>"])
        self.assertEqual(self.forgotten, [("INBOX", ["<a@x>"])])

    def test_permanent_on_normal_server_expunges_in_place(self):
        server = FakeServer({"INBOX": {5: "<a@x>", 6: "<b@x>"}, "Trash": {9: "<old@x>"}})
        self.patch_server(server)
        moved, unresolved = self.run_batch(
            [("5", "INBOX", "<a@x>"), ("6", "INBOX", "<b@x>")], permanent=True, trash="Trash",
        )
        self.assertEqual((moved, unresolved), ([], []))
        self.assertEqual(server.folders["INBOX"], {})
        self.assertEqual(server.folders["Trash"], {9: "<old@x>"})
        self.assertEqual(server.moves, [])

    def test_permanent_on_gmail_goes_through_trash_and_purges_it(self):
        server = FakeServer({"INBOX": {5: "<a@x>"}, "[Gmail]/Trash": {9: "<old@x>"}})
        self.patch_server(server)
        moved, unresolved = self.run_batch(
            [("offline-id", "INBOX", "<A@X>")], permanent=True, trash="[Gmail]/Trash",
        )
        self.assertEqual((moved, unresolved), ([], []))
        self.assertEqual(server.folders["INBOX"], {})
        self.assertEqual(server.folders["[Gmail]/Trash"], {9: "<old@x>"})

    def test_message_already_gone_is_done(self):
        server = FakeServer({"INBOX": {}, "Trash": {}})
        self.patch_server(server)
        moved, unresolved = self.run_batch([("5", "INBOX", "<gone@x>")], permanent=True, trash="Trash")
        self.assertEqual(unresolved, [])

    def test_server_refusal_is_reported_not_retried(self):
        server = FakeServer({"INBOX": {5: "<a@x>"}, "Trash": {}})
        server.refuse_move = True
        self.patch_server(server)
        moved, unresolved = self.run_batch([("5", "INBOX", "<a@x>")], permanent=False, trash="Trash")
        self.assertEqual(moved, [])
        self.assertEqual(unresolved, [("5", "INBOX", "move_failed")])

    def test_no_header_and_no_uid_cannot_be_found(self):
        server = FakeServer({"INBOX": {5: "<a@x>"}, "Trash": {}})
        self.patch_server(server)
        _moved, unresolved = self.run_batch([("offline-id", "INBOX", None)], permanent=False, trash="Trash")
        self.assertEqual(unresolved, [("offline-id", "INBOX", "move_failed")])
        self.assertEqual(server.folders["INBOX"], {5: "<a@x>"})

    def test_uid_without_header_still_moves(self):
        server = FakeServer({"INBOX": {5: None}, "Trash": {}})
        self.patch_server(server)
        moved, unresolved = self.run_batch([("5", "INBOX", None)], permanent=False, trash="Trash")
        self.assertEqual(unresolved, [])
        self.assertEqual(server.folders["INBOX"], {})

    def test_without_uidplus_nothing_is_expunged_and_it_is_reported(self):
        server = FakeServer({"INBOX": {5: "<a@x>"}, "Trash": {}}, uidplus=False)
        self.patch_server(server)
        _moved, unresolved = self.run_batch([("5", "INBOX", "<a@x>")], permanent=True, trash="Trash")
        self.assertEqual(unresolved, [("5", "INBOX", "flagged_only")])

    def test_network_failure_raises_for_a_retry(self):
        server = FakeServer({"INBOX": {5: "<a@x>"}, "Trash": {}})
        server.network_failures = 1
        self.patch_server(server)
        with self.assertRaises(imap_body_fetch.ImapBodyFetchUnavailable):
            self.run_batch([("5", "INBOX", "<a@x>")], permanent=False, trash="Trash")


class DeleteQueueTests(QueueTestBase):
    def make_queue(self, done):
        return delete_queue.DeleteQueue(
            self.paths, lambda account_id: self.account,
            lambda *args: done.append(args), lambda fn, *args: fn(*args),
            pause=lambda _s: None,
        )

    def wait_for(self, done, count=1):
        for _ in range(200):
            if len(done) >= count:
                return
            threading.Event().wait(0.02)
        self.fail("the queue never finished")

    def test_retries_through_network_trouble_then_finishes(self):
        server = FakeServer({"INBOX": {5: "<a@x>"}, "Trash": {}})
        server.network_failures = 2
        self.patch_server(server)
        done = []
        with mock.patch.object(delete_queue, "BACKOFF_SECONDS", (0,)):
            queue = self.make_queue(done)
            self.addCleanup(queue.stop)
            batch_id = queue.add(self.account, [("5", "INBOX", "<a@x>")], False, "Trash")
            self.wait_for(done)
        self.assertEqual(done[0][0], batch_id)
        self.assertEqual(done[0][4], [])
        self.assertEqual(server.folders["INBOX"], {})
        self.assertEqual(queue.pending(), [])

    def test_batch_is_saved_and_resumed_by_the_next_start(self):
        server = FakeServer({"INBOX": {5: "<a@x>"}, "Trash": {}})
        self.patch_server(server)
        done = []
        first = self.make_queue(done)
        first.stop()
        first.add(self.account, [("5", "INBOX", "<a@x>")], True, "Trash")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, delete_queue.QUEUE_FILE_NAME)))
        self.assertEqual(done, [])

        second = self.make_queue(done)
        self.addCleanup(second.stop)
        self.assertEqual(len(second.pending()), 1)
        second.start()
        self.wait_for(done)
        self.assertEqual(done[0][4], [])
        self.assertEqual(server.folders["INBOX"], {})
        self.assertEqual(second.pending(), [])


class CanonicalMessageIdTests(unittest.TestCase):
    def test_brackets_space_and_case(self):
        self.assertEqual(imap_body_fetch.canonical_message_id(" <AbC@X.org> "), "abc@x.org")
        self.assertIsNone(imap_body_fetch.canonical_message_id("<>"))
        self.assertIsNone(imap_body_fetch.canonical_message_id(None))


if __name__ == "__main__":
    unittest.main()
