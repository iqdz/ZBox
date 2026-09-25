"""
_ensure_sent_copy: after a send, a Sent copy is added only when neither
the server nor Himalaya saved one. A disroot send on 16 September 2026
left none, while Gmail, which saves on SMTP itself, never showed it.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client
import imap_body_fetch

RAW = "Message-ID: <abc@example.org>\r\nSubject: Hi\r\n\r\nBody\r\n"


class _Account:
    account_id = "acct"
    imap_host = "disroot.org"


class SentCopy(unittest.TestCase):
    def _run_with(self, found, raw=RAW, account=None):
        with mock.patch.object(imap_body_fetch, "sent_copy_exists", side_effect=found) as exists, \
                mock.patch.object(himalaya_client, "add_message_raw") as add, \
                mock.patch.object(himalaya_client, "_pause_before_recheck"):
            himalaya_client._ensure_sent_copy(None, account or _Account(), raw)
        return exists, add

    def test_a_copy_the_server_saved_is_not_duplicated(self):
        exists, add = self._run_with([True])
        add.assert_not_called()
        self.assertEqual(exists.call_args[0][3], "<abc@example.org>")

    def test_a_late_server_copy_is_found_on_the_recheck(self):
        exists, add = self._run_with([False, True])
        self.assertEqual(exists.call_count, 2)
        add.assert_not_called()

    def test_a_missing_copy_is_added_to_sent_as_seen(self):
        exists, add = self._run_with([False, False])
        add.assert_called_once()
        self.assertEqual(add.call_args[0][2], "Sent")
        self.assertEqual(add.call_args[1]["flags"], ("seen",))

    def test_an_uncheckable_folder_adds_nothing(self):
        exists, add = self._run_with(imap_body_fetch.ImapBodyFetchUnavailable("down"))
        add.assert_not_called()

    def test_no_message_id_means_no_check(self):
        exists, add = self._run_with([False], raw="Subject: Hi\r\n\r\nBody\r\n")
        exists.assert_not_called()
        add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
