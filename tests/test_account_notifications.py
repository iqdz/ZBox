"""
Account.new_mail_sound_enabled / new_mail_announce_enabled (audit
finding 47's remaining half) -- per-account new-mail notification
granularity, edited in Account Settings, persisted alongside every
other account field. This only covers Account.to_dict/from_dict; the
gating itself lives in mail_fetch.py's new-mail diff and
main_frame._announce_new_mail, both real wx/threaded code not
unit-tested here (same limitation already hit by every other
per-account setting in this file's siblings, e.g.
test_account_junk_folder.py).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from account_manager import Account


class AccountNotificationSettings(unittest.TestCase):
    def test_defaults_match_thunderbird_style_expectations(self):
        # Sound stays on by default (matches the pre-existing global
        # default); announcing starts off, since speech interrupts in
        # a way a sound doesn't.
        account = Account(display_name="Harith", login_email="h@example.com")
        self.assertTrue(account.new_mail_sound_enabled)
        self.assertFalse(account.new_mail_announce_enabled)

    def test_round_trips_through_to_dict_and_from_dict(self):
        account = Account(
            login_email="h@example.com",
            new_mail_sound_enabled=False,
            new_mail_announce_enabled=True,
        )
        restored = Account.from_dict(account.to_dict())
        self.assertFalse(restored.new_mail_sound_enabled)
        self.assertTrue(restored.new_mail_announce_enabled)

    def test_from_dict_defaults_missing_keys(self):
        # An accounts.json entry saved before this feature existed
        # has neither key at all.
        account = Account.from_dict({
            "account_id": "acct_old",
            "login_email": "h@example.com",
        })
        self.assertTrue(account.new_mail_sound_enabled)
        self.assertFalse(account.new_mail_announce_enabled)

    def test_values_are_normalized_to_bool(self):
        account = Account(
            login_email="h@example.com",
            new_mail_sound_enabled=1,
            new_mail_announce_enabled="yes",
        )
        self.assertIs(account.new_mail_sound_enabled, True)
        self.assertIs(account.new_mail_announce_enabled, True)


if __name__ == "__main__":
    unittest.main()
