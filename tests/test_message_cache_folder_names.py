"""
Audit finding 38: _sanitize_folder_name maps every non-alphanumeric
character to '_', so distinct real folder names -- "Sent Mail" and
"Sent_Mail", or two differently-punctuated Gmail labels -- used to
land in the same message_cache subdirectory. The real folder name is
now hashed into the directory name too.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc


class MessageCacheFolderKey(unittest.TestCase):
    def test_previously_colliding_names_now_differ(self):
        # The exact pair the audit named.
        self.assertNotEqual(
            hc._message_cache_folder_key("Sent Mail"),
            hc._message_cache_folder_key("Sent_Mail"),
        )

    def test_differently_punctuated_labels_now_differ(self):
        self.assertNotEqual(
            hc._message_cache_folder_key("[Gmail]/Sent Mail"),
            hc._message_cache_folder_key("[Gmail].Sent.Mail"),
        )

    def test_same_name_is_stable_across_calls(self):
        self.assertEqual(
            hc._message_cache_folder_key("INBOX"),
            hc._message_cache_folder_key("INBOX"),
        )

    def test_key_stays_a_readable_filesystem_safe_name(self):
        key = hc._message_cache_folder_key("Sent Mail")
        self.assertTrue(key.startswith("Sent_Mail-"))
        self.assertTrue(all(c.isalnum() or c in ("-", "_") for c in key))

    def test_none_folder_falls_back_to_the_same_key_as_the_literal_default(self):
        # Both mean "no folder name was given" -- not two distinct
        # real folders, so sharing a directory here is fine and
        # matches _sanitize_folder_name's own None-handling.
        self.assertEqual(
            hc._message_cache_folder_key(None),
            hc._message_cache_folder_key("folder"),
        )


if __name__ == "__main__":
    unittest.main()
