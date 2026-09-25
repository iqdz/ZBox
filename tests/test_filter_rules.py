"""
filter_rules.py -- audit finding 45's matching engine. Pure logic,
no wx and no himalaya_client, same reasoning as bulk_execution.py's
own tests: what actually needs to be correct here is whether a rule
matches and what it says to do, not any UI or network call.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import filter_rules as fr


def envelope(**overrides):
    base = {
        "id": "1",
        "subject": "Weekly newsletter",
        "from": [{"name": "ZBox News", "email": "news@example.com"}],
        "to": [{"name": "Harith", "email": "harith@example.com"}],
    }
    base.update(overrides)
    return base


class ConditionMatching(unittest.TestCase):
    def test_subject_contains_is_case_insensitive(self):
        condition = fr.FilterCondition(field="subject", value="NEWSLETTER")
        self.assertTrue(condition.matches(envelope()))

    def test_subject_not_containing_does_not_match(self):
        condition = fr.FilterCondition(field="subject", value="invoice")
        self.assertFalse(condition.matches(envelope()))

    def test_from_matches_display_name_or_address(self):
        self.assertTrue(fr.FilterCondition(field="from", value="ZBox News").matches(envelope()))
        self.assertTrue(fr.FilterCondition(field="from", value="news@example.com").matches(envelope()))
        self.assertTrue(fr.FilterCondition(field="from", value="example.com").matches(envelope()))

    def test_to_field_reads_the_to_list(self):
        self.assertTrue(fr.FilterCondition(field="to", value="harith").matches(envelope()))

    def test_blank_value_never_matches(self):
        self.assertFalse(fr.FilterCondition(field="subject", value="").matches(envelope()))
        self.assertFalse(fr.FilterCondition(field="subject", value="   ").matches(envelope()))

    def test_malformed_envelope_is_not_an_error(self):
        self.assertFalse(fr.FilterCondition(field="from", value="x").matches(None))
        self.assertFalse(fr.FilterCondition(field="from", value="x").matches({"from": "not-a-list"}))


class RuleMatching(unittest.TestCase):
    def test_match_all_requires_every_condition(self):
        rule = fr.FilterRule(
            match_all=True,
            conditions=[
                fr.FilterCondition(field="subject", value="newsletter"),
                fr.FilterCondition(field="from", value="example.com"),
            ],
        )
        self.assertTrue(rule.matches(envelope()))

        rule_with_mismatch = fr.FilterRule(
            match_all=True,
            conditions=[
                fr.FilterCondition(field="subject", value="newsletter"),
                fr.FilterCondition(field="from", value="somewhere-else.com"),
            ],
        )
        self.assertFalse(rule_with_mismatch.matches(envelope()))

    def test_match_any_requires_only_one_condition(self):
        rule = fr.FilterRule(
            match_all=False,
            conditions=[
                fr.FilterCondition(field="subject", value="invoice"),
                fr.FilterCondition(field="from", value="example.com"),
            ],
        )
        self.assertTrue(rule.matches(envelope()))

    def test_a_rule_with_no_conditions_matches_nothing(self):
        self.assertFalse(fr.FilterRule().matches(envelope()))

    def test_conditions_with_a_blank_value_are_ignored_not_counted_as_failures(self):
        # A cleared-but-not-deleted condition slot must not make an
        # otherwise-matching AND rule fail.
        rule = fr.FilterRule(
            match_all=True,
            conditions=[
                fr.FilterCondition(field="subject", value="newsletter"),
                fr.FilterCondition(field="to", value=""),
            ],
        )
        self.assertTrue(rule.matches(envelope()))

    def test_has_action_and_has_condition(self):
        bare = fr.FilterRule()
        self.assertFalse(bare.has_action())
        self.assertFalse(bare.has_condition())

        with_condition = fr.FilterRule(conditions=[fr.FilterCondition(field="subject", value="x")])
        self.assertTrue(with_condition.has_condition())
        self.assertFalse(with_condition.has_action())

        for kwargs in ({"mark_read": True}, {"flag": True}, {"move_to": "Archive"}):
            self.assertTrue(fr.FilterRule(**kwargs).has_action())


class RuleSerialization(unittest.TestCase):
    def test_round_trip(self):
        rule = fr.FilterRule(
            name="Newsletter",
            enabled=False,
            match_all=False,
            conditions=[fr.FilterCondition(field="from", value="news@example.com")],
            mark_read=True,
            flag=True,
            move_to="Archive",
        )
        restored = fr.FilterRule.from_dict(rule.to_dict())
        self.assertEqual(restored, rule)

    def test_from_dict_defaults_are_safe_against_garbage(self):
        restored = fr.FilterRule.from_dict({
            "field": "not-a-real-key",
            "conditions": "not-a-list",
            "move_to": None,
        })
        self.assertEqual(restored.conditions, [])
        self.assertEqual(restored.move_to, "")
        self.assertTrue(restored.enabled)  # missing -> default True

        self.assertEqual(fr.FilterRule.from_dict(None), fr.FilterRule())
        self.assertEqual(fr.FilterCondition.from_dict(None), fr.FilterCondition())

    def test_unknown_condition_field_falls_back_to_from(self):
        restored = fr.FilterCondition.from_dict({"field": "body", "value": "x"})
        self.assertEqual(restored.field, "from")


class MatchingRules(unittest.TestCase):
    def _rule(self, name, **kwargs):
        kwargs.setdefault("conditions", [fr.FilterCondition(field="subject", value="newsletter")])
        kwargs.setdefault("mark_read", True)
        return fr.FilterRule(name=name, **kwargs)

    def test_disabled_rules_are_skipped(self):
        rules = [self._rule("A", enabled=False)]
        self.assertEqual(fr.matching_rules(rules, envelope()), [])

    def test_rules_with_no_action_are_skipped(self):
        rules = [fr.FilterRule(
            name="No-op", conditions=[fr.FilterCondition(field="subject", value="newsletter")],
        )]
        self.assertEqual(fr.matching_rules(rules, envelope()), [])

    def test_non_matching_rules_are_skipped(self):
        rules = [self._rule("A", conditions=[fr.FilterCondition(field="subject", value="invoice")])]
        self.assertEqual(fr.matching_rules(rules, envelope()), [])

    def test_matching_enabled_rules_come_back_in_order(self):
        first = self._rule("First")
        second = self._rule("Second", flag=True)
        self.assertEqual(fr.matching_rules([first, second], envelope()), [first, second])

    def test_empty_rule_list_is_fine(self):
        self.assertEqual(fr.matching_rules([], envelope()), [])
        self.assertEqual(fr.matching_rules(None, envelope()), [])


if __name__ == "__main__":
    unittest.main()
