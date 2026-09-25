"""
Account.junk_folder (audit finding 46) -- a per-account override of
which real folder Mark as Junk moves messages to, edited in Account
Settings, persisted alongside every other account field. This only
covers Account.to_dict/from_dict; the fallback-to-generic-mapping
logic is envelope_format._junk_folder_for_account, tested in
test_envelope_format.py, and the dialog widget is real wx so it
isn't unit-tested here (same limitation already hit by
Account.signature in test_account_signature.py).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from account_manager import Account


class AccountJunkFolder(unittest.TestCase):
    def test_defaults_to_empty(self):
        account = Account(display_name="Harith", login_email="h@example.com")
        self.assertEqual(account.junk_folder, "")

    def test_round_trips_through_to_dict_and_from_dict(self):
        account = Account(
            display_name="Harith", login_email="h@example.com",
            junk_folder="Custom Spam Folder",
        )
        restored = Account.from_dict(account.to_dict())
        self.assertEqual(restored.junk_folder, "Custom Spam Folder")

    def test_from_dict_defaults_missing_junk_folder_to_empty(self):
        # An accounts.json entry saved before this feature existed
        # has no "junk_folder" key at all.
        account = Account.from_dict({
            "account_id": "acct_old",
            "display_name": "Harith",
            "login_email": "h@example.com",
        })
        self.assertEqual(account.junk_folder, "")

    def test_none_is_normalized_to_empty_string(self):
        account = Account(login_email="h@example.com", junk_folder=None)
        self.assertEqual(account.junk_folder, "")


if __name__ == "__main__":
    unittest.main()
