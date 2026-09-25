"""
Audit finding 40 (Drafts): draft_autosave_seconds is a new Settings
field (how often an open compose tab autosaves to Drafts). These
tests cover its clamping (0 = off, otherwise 15-600) and its
to_dict/from_dict round trip, the same shape every other numeric
Settings field already gets tested this way (see e.g.
announcement_hold_ms's clamping, though that one has no dedicated
test file -- this is the first for a numeric setting).
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from settings_manager import Settings


class DraftAutosaveSecondsClamping(unittest.TestCase):
    def test_default_is_60(self):
        self.assertEqual(Settings().draft_autosave_seconds, 60)

    def test_zero_means_off_and_stays_zero(self):
        self.assertEqual(Settings(draft_autosave_seconds=0).draft_autosave_seconds, 0)

    def test_negative_is_treated_as_off(self):
        self.assertEqual(Settings(draft_autosave_seconds=-5).draft_autosave_seconds, 0)

    def test_below_15_is_floored_to_15_not_off(self):
        # A value below 15 isn't a real choice the Settings dialog
        # offers, but a hand-edited settings.json could still have
        # one -- clamp up to the floor rather than silently treating
        # "8" the same as "0" (off), which would change what the
        # user asked for from "very frequent" to "never".
        self.assertEqual(Settings(draft_autosave_seconds=8).draft_autosave_seconds, 15)

    def test_above_600_is_capped(self):
        self.assertEqual(Settings(draft_autosave_seconds=9999).draft_autosave_seconds, 600)

    def test_non_numeric_falls_back_to_default(self):
        self.assertEqual(Settings(draft_autosave_seconds="not a number").draft_autosave_seconds, 60)

    def test_to_dict_and_from_dict_round_trip(self):
        settings = Settings(draft_autosave_seconds=120)
        restored = Settings.from_dict(settings.to_dict())
        self.assertEqual(restored.draft_autosave_seconds, 120)

    def test_from_dict_missing_key_defaults_to_60(self):
        restored = Settings.from_dict({})
        self.assertEqual(restored.draft_autosave_seconds, 60)


if __name__ == "__main__":
    unittest.main()
