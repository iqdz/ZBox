"""
Account.filters (audit finding 45) -- per-account filter rules,
edited in Tools > Message Filters, persisted alongside every other
account field. This only covers Account.to_dict/from_dict; the
matching engine itself is tested in test_filter_rules.py, and the
dialog widgets are real wx so they aren't unit-tested here (same
limitation already hit by ServerPage in test_provider_presets.py and
by Account.signature in test_account_signature.py).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from account_manager import Account
from filter_rules import FilterCondition, FilterRule


class AccountFilters(unittest.TestCase):
    def test_defaults_to_an_empty_list(self):
        account = Account(display_name="Harith", login_email="h@example.com")
        self.assertEqual(account.filters, [])

    def test_round_trips_through_to_dict_and_from_dict(self):
        rule = FilterRule(
            name="Newsletter",
            enabled=True,
            match_all=False,
            conditions=[FilterCondition(field="from", value="news@example.com")],
            mark_read=True,
            flag=False,
            move_to="Archive",
        )
        account = Account(
            display_name="Harith", login_email="h@example.com", filters=[rule],
        )
        restored = Account.from_dict(account.to_dict())
        self.assertEqual(restored.filters, [rule])

    def test_from_dict_defaults_missing_filters_to_an_empty_list(self):
        # An accounts.json entry saved before this feature existed
        # has no "filters" key at all.
        account = Account.from_dict({
            "account_id": "acct_old",
            "display_name": "Harith",
            "login_email": "h@example.com",
        })
        self.assertEqual(account.filters, [])

    def test_from_dict_skips_non_dict_filter_entries(self):
        # Defensive against a hand-edited or corrupted accounts.json.
        account = Account.from_dict({
            "account_id": "acct_x",
            "login_email": "h@example.com",
            "filters": ["not-a-dict", None, {"name": "Real rule"}],
        })
        self.assertEqual(len(account.filters), 1)
        self.assertEqual(account.filters[0].name, "Real rule")

    def test_multiple_rules_keep_their_order(self):
        rules = [FilterRule(name="A"), FilterRule(name="B"), FilterRule(name="C")]
        account = Account(login_email="h@example.com", filters=rules)
        restored = Account.from_dict(account.to_dict())
        self.assertEqual([rule.name for rule in restored.filters], ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
