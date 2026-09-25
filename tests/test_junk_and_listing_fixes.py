"""
Two fixes from session w, both about state going stale underneath
something that then wrote or read the wrong thing.

1. JunkRules.remove_senders. Tools > Privacy and Content Blocking used
   to write both sender lists back whole on OK, from a snapshot taken
   when the dialog opened, so a Mark as Junk made while it sat open was
   erased. It now sends only what was removed.

2. _PooledConnection.list_envelopes takes a fresh SELECT every time.
   A NOOP alone is not enough on Gmail: mail moved into a folder from
   another client never appeared on the 20-second poll while ZBox
   already had that folder selected.
"""

import datetime
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import imap_body_fetch as ibf
import junk_rules


class RemoveSenders(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "junk_rules.json")
        self.addCleanup(self._dir.cleanup)

    def _rules(self):
        return junk_rules.JunkRules(self.path)

    def test_removes_only_what_was_named(self):
        rules = self._rules()
        rules.set_lists(["one@example.com", "two@example.com"], ["three@example.com"])
        rules.remove_senders(["one@example.com"], [])
        self.assertEqual(rules.blocked, ["two@example.com"])
        self.assertEqual(rules.allowed, ["three@example.com"])

    def test_a_sender_blocked_while_the_dialog_was_open_survives(self):
        # The whole point of the fix: the dialog's snapshot is stale by
        # the time OK is pressed, and must not be written back whole.
        rules = self._rules()
        rules.set_lists(["old@example.com"], [])
        snapshot_blocked = rules.blocked
        rules.block_senders([{"from": [{"email": "new@example.com"}]}])
        rules.remove_senders(
            [a for a in snapshot_blocked if a != "old@example.com"], [],
        )
        self.assertIn("new@example.com", rules.blocked)

    def test_removals_reach_disk(self):
        rules = self._rules()
        rules.set_lists(["gone@example.com"], [])
        rules.remove_senders(["gone@example.com"], [])
        self.assertEqual(self._rules().blocked, [])

    def test_nothing_named_changes_nothing(self):
        rules = self._rules()
        rules.set_lists(["stay@example.com"], ["also@example.com"])
        rules.remove_senders([], [])
        self.assertEqual(rules.blocked, ["stay@example.com"])
        self.assertEqual(rules.allowed, ["also@example.com"])

    def test_an_unknown_address_is_harmless(self):
        rules = self._rules()
        rules.set_lists(["stay@example.com"], [])
        rules.remove_senders(["never@example.com"], [])
        self.assertEqual(rules.blocked, ["stay@example.com"])


class _FakeAccount:
    account_id = "acct"
    imap_host = "imap.example.com"


class _FakeClient:
    """Records the commands the pooled connection sends."""

    def __init__(self, uids=(1, 2)):
        self.selected = []
        self.noops = 0
        self._uids = list(uids)

    def select_folder(self, folder, readonly=False):
        self.selected.append(folder)

    def noop(self):
        self.noops += 1

    def search(self, _criteria):
        return list(self._uids)

    def fetch(self, uids, _fields):
        when = datetime.datetime(2026, 9, 16, 9, 0, tzinfo=datetime.timezone.utc)
        return {
            uid: {b"ENVELOPE": None, b"FLAGS": [], b"INTERNALDATE": when}
            for uid in uids
        }


class PooledListingTakesAFreshSelect(unittest.TestCase):
    def _connection(self, client):
        connection = ibf._PooledConnection(_FakeAccount(), None)
        connection._client = client
        connection._last_used = time.monotonic()
        return connection

    def test_selects_again_even_when_the_folder_is_already_selected(self):
        client = _FakeClient()
        connection = self._connection(client)
        connection._folder = "INBOX"
        connection.list_envelopes("INBOX")
        self.assertEqual(client.selected, ["INBOX"])

    def test_every_listing_selects_again(self):
        client = _FakeClient()
        connection = self._connection(client)
        connection._folder = "INBOX"
        connection.list_envelopes("INBOX")
        connection.list_envelopes("INBOX")
        self.assertEqual(client.selected, ["INBOX", "INBOX"])

    def test_the_noop_is_still_sent(self):
        client = _FakeClient()
        connection = self._connection(client)
        connection._folder = "INBOX"
        connection.list_envelopes("INBOX")
        self.assertEqual(client.noops, 1)

    def test_the_envelopes_still_come_back_newest_first(self):
        client = _FakeClient(uids=(1, 2, 3))
        connection = self._connection(client)
        connection._folder = None
        envelopes = connection.list_envelopes("INBOX")
        self.assertEqual([e["id"] for e in envelopes], ["3", "2", "1"])


if __name__ == "__main__":
    unittest.main()
