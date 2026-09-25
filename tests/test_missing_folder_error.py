"""
Follow-up to the 2026-09-03 audit's search-across-providers work:
searching/opening the "Archive" folder for a disroot.org account
raised "IMAP SELECT failed: NO Mailbox doesn't exist: Archive" --
confirmed via a live zbox_debug.log, not a naming mismatch like
Gmail's (provider_presets.SPECIAL_FOLDER_NAMES) but a mailbox that
was never provisioned on that account's server at all.

is_missing_folder_error() lets mail_fetch.py and search_panel.py
tell that expected, benign case apart from a genuine failure (auth,
network, a corrupted mailbox) that should still surface as an error.
These tests pin the exact error text observed live, plus text that
must NOT match so a real failure never gets silently swallowed.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc


class IsMissingFolderError(unittest.TestCase):
    def test_matches_live_disroot_archive_error(self):
        # Exact text captured from zbox_debug.log, 2026-09-05, account
        # acct_example (you@example.org / disroot.org), search
        # -m Archive.
        exc = hc.HimalayaError(
            'Himalaya exited with code 1: {"error":"IMAP SELECT failed: '
            'NO Mailbox doesn\'t exist: Archive (0.133 + 0.000 + 0.132 '
            'secs).","sources":[],"backtrace":null}'
        )
        self.assertTrue(hc.is_missing_folder_error(exc))

    def test_matches_regardless_of_folder_name_or_case(self):
        exc = hc.HimalayaError("NO MAILBOX DOESN'T EXIST: Some Folder")
        self.assertTrue(hc.is_missing_folder_error(exc))

    def test_matches_plain_string_not_just_exception(self):
        # Callers may have only the message text, not an exception
        # instance (e.g. re-checking stderr) -- str() on a plain
        # string is a no-op, so this must work either way.
        self.assertTrue(hc.is_missing_folder_error("Mailbox doesn't exist: Archive"))

    def test_does_not_match_auth_failure(self):
        exc = hc.HimalayaError("Himalaya exited with code 1: authentication failed")
        self.assertFalse(hc.is_missing_folder_error(exc))

    def test_does_not_match_network_error(self):
        exc = hc.HimalayaError("Himalaya exited with code 1: connection timed out")
        self.assertFalse(hc.is_missing_folder_error(exc))

    def test_does_not_match_unrelated_select_failure(self):
        # A SELECT can fail for reasons other than a missing mailbox
        # (e.g. permissions) -- only the specific "doesn't exist"
        # wording should count.
        exc = hc.HimalayaError("IMAP SELECT failed: NO Permission denied")
        self.assertFalse(hc.is_missing_folder_error(exc))


if __name__ == "__main__":
    unittest.main()
