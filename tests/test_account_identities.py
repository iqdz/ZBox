"""
Audit finding 49: multiple identities (aliases) per account.
Account.extra_identities, Account.identities() and account_manager.
all_identities() are pure and covered here; the identities editor in
AccountSettingsDialog and the From wx.Choice wiring in
compose_panel.py are real wx and not unit-tested, same limitation
already accepted for Account.signature/junk_folder.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from account_manager import Account, all_identities


def _account(**overrides):
    defaults = dict(display_name="Harith", login_email="h@example.com")
    defaults.update(overrides)
    return Account(**defaults)


class AccountExtraIdentities(unittest.TestCase):
    def test_defaults_to_empty(self):
        account = _account()
        self.assertEqual(account.extra_identities, [])

    def test_none_is_normalized_to_empty_list(self):
        account = _account(extra_identities=None)
        self.assertEqual(account.extra_identities, [])

    def test_entries_are_stripped_and_kept(self):
        account = _account(extra_identities=[
            {"display_name": "  Work  ", "email": "  h@work.example.com  "},
        ])
        self.assertEqual(
            account.extra_identities,
            [{"display_name": "Work", "email": "h@work.example.com"}],
        )

    def test_entries_with_no_email_are_dropped(self):
        account = _account(extra_identities=[
            {"display_name": "No address", "email": ""},
            {"display_name": "Also none"},
            "not even a dict",
            {"display_name": "Good", "email": "good@example.com"},
        ])
        self.assertEqual(
            account.extra_identities,
            [{"display_name": "Good", "email": "good@example.com"}],
        )

    def test_missing_display_name_defaults_to_empty_string(self):
        account = _account(extra_identities=[{"email": "alias@example.com"}])
        self.assertEqual(
            account.extra_identities,
            [{"display_name": "", "email": "alias@example.com"}],
        )

    def test_round_trips_through_to_dict_and_from_dict(self):
        account = _account(extra_identities=[
            {"display_name": "Work", "email": "h@work.example.com"},
            {"display_name": "", "email": "catchall@example.com"},
        ])
        restored = Account.from_dict(account.to_dict())
        self.assertEqual(restored.extra_identities, account.extra_identities)

    def test_from_dict_defaults_missing_key_to_empty_list(self):
        # An accounts.json entry saved before this feature existed.
        account = Account.from_dict({
            "account_id": "acct_old", "display_name": "Harith",
            "login_email": "h@example.com",
        })
        self.assertEqual(account.extra_identities, [])


class AccountIdentities(unittest.TestCase):
    def test_primary_identity_comes_first(self):
        account = _account(display_name="Harith", identity_email="h@example.com")
        self.assertEqual(account.identities()[0], ("Harith", "h@example.com"))

    def test_only_primary_when_no_extras(self):
        account = _account()
        self.assertEqual(len(account.identities()), 1)

    def test_extras_follow_in_order(self):
        account = _account(extra_identities=[
            {"display_name": "Work", "email": "h@work.example.com"},
            {"display_name": "Alias", "email": "h@alias.example.com"},
        ])
        self.assertEqual(
            account.identities(),
            [
                ("Harith", "h@example.com"),
                ("Work", "h@work.example.com"),
                ("Alias", "h@alias.example.com"),
            ],
        )


class AllIdentities(unittest.TestCase):
    def test_empty_account_list(self):
        self.assertEqual(all_identities([]), [])

    def test_flattens_multiple_accounts_each_with_extras(self):
        acct1 = _account(
            account_id="a1", display_name="Personal", identity_email="p@example.com",
            extra_identities=[{"display_name": "Alias", "email": "alias@example.com"}],
        )
        acct2 = _account(
            account_id="a2", display_name="Work", login_email="w@example.com",
        )
        result = all_identities([acct1, acct2])
        self.assertEqual(result, [
            (acct1, "Personal", "p@example.com"),
            (acct1, "Alias", "alias@example.com"),
            (acct2, "Work", "w@example.com"),
        ])

    def test_account_with_no_identities_is_impossible_but_missing_from_list_is_fine(self):
        # Guard against a future regression rather than a real case:
        # every Account always has at least its primary identity.
        result = all_identities([_account()])
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
