"""
Audit finding 40 (Drafts): add_message_raw() builds the 'message add'
subprocess call Save Draft, autosave, and save-on-close all share.
Confirmed live against the pinned himalaya v2.1.0 build (downloaded
as the matching x86_64-linux release into a throwaway sandbox, run
against a scratch maildir account -- see the function's own docstring
for the exact command and why --mailbox is passed the real resolved
folder name rather than an alias). These tests mock hc._run entirely,
same as test_search_envelopes.py: the point is verifying the exact
argv this function hands to Himalaya, not Himalaya's own behavior.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc


class AddMessageRawArgvConstruction(unittest.TestCase):
    def test_default_flag_is_draft(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"id": "42", "sent": False}
            result = hc.add_message_raw("paths", "acct", "Drafts", "raw text")

        args, kwargs = run.call_args
        paths, account, argv = args[0], args[1], args[2]
        self.assertEqual(paths, "paths")
        self.assertEqual(account, "acct")
        self.assertEqual(argv, ["message", "add", "--mailbox", "Drafts", "-f", "draft"])
        self.assertEqual(kwargs.get("input_text"), "raw text")
        self.assertEqual(kwargs.get("backend"), "imap")
        self.assertEqual(result, "42")

    def test_message_is_piped_via_stdin_not_a_positional_argument(self):
        # Matches send_message_raw's own reasoning: keeps a large
        # message off the command line and out of process-list
        # visibility, and sidesteps the CLI's own positional-message
        # quirks entirely.
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"id": "1", "sent": False}
            hc.add_message_raw("paths", "acct", "Drafts", "From: a@b.com\r\n\r\nBody")

        argv = run.call_args.args[2]
        self.assertNotIn("From: a@b.com\r\n\r\nBody", argv)

    def test_multiple_flags_each_get_their_own_dash_f(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"id": "1", "sent": False}
            hc.add_message_raw("paths", "acct", "Sent", "raw", flags=("seen", "answered"))

        argv = run.call_args.args[2]
        self.assertEqual(
            argv, ["message", "add", "--mailbox", "Sent", "-f", "seen", "-f", "answered"],
        )

    def test_no_flags_omits_dash_f_entirely(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"id": "1", "sent": False}
            hc.add_message_raw("paths", "acct", "Drafts", "raw", flags=())

        argv = run.call_args.args[2]
        self.assertEqual(argv, ["message", "add", "--mailbox", "Drafts"])

    def test_returns_the_new_id_from_json_output(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"id": "abc123", "sent": False}
            result = hc.add_message_raw("paths", "acct", "Drafts", "raw")

        self.assertEqual(result, "abc123")

    def test_missing_id_in_response_raises(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"sent": False}
            with self.assertRaises(hc.HimalayaError):
                hc.add_message_raw("paths", "acct", "Drafts", "raw")

    def test_non_dict_response_raises(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = None
            with self.assertRaises(hc.HimalayaError):
                hc.add_message_raw("paths", "acct", "Drafts", "raw")


if __name__ == "__main__":
    unittest.main()
