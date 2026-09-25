"""
Audit finding 52, the other two pieces beyond window geometry (see
test_window_geometry.py): remembering the last-selected account/
folder (or Unified Folders entry) and the two splitter sash
positions, instead of always reopening on the first account's Inbox
at fixed 220/420 splits.

account_tree_panel.target_to_saved_fields/saved_fields_to_target are
the pure conversion between selected_fetch_target()'s dict shape and
Settings' three flat last_selected_* fields; Settings' own
to_dict/from_dict round trip for those fields plus outer_sash_position/
inner_sash_position gets the same coverage every other Settings field
gets (see test_draft_settings.py). main_frame.py's own wiring
(_save_selected_folder, _on_sash_position_changed, restoring on
launch) is wx event plumbing on ZBoxMainFrame, which this project has
never unit-tested directly (it has no __init__-free construction path
worth building just for this) -- same accepted gap as every other
frame-level wiring change in this audit.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from account_tree_panel import target_to_saved_fields, saved_fields_to_target
from settings_manager import Settings


class TargetToSavedFields(unittest.TestCase):
    def test_none_target_is_all_none(self):
        self.assertEqual(target_to_saved_fields(None), (None, None, None))

    def test_empty_dict_target_is_all_none(self):
        self.assertEqual(target_to_saved_fields({}), (None, None, None))

    def test_account_folder_target(self):
        target = {"account_id": "acct_1", "folder": "INBOX"}
        self.assertEqual(
            target_to_saved_fields(target), ("acct_1", "INBOX", None)
        )

    def test_unified_target(self):
        target = {"unified": "Inbox"}
        self.assertEqual(
            target_to_saved_fields(target), (None, None, "Inbox")
        )

    def test_unified_takes_precedence_if_somehow_both_present(self):
        # Not a shape selected_fetch_target() actually produces, but
        # the dict-key check should still be unambiguous either way.
        target = {"unified": "Inbox", "account_id": "acct_1", "folder": "INBOX"}
        self.assertEqual(
            target_to_saved_fields(target), (None, None, "Inbox")
        )


class SavedFieldsToTarget(unittest.TestCase):
    def test_nothing_saved_returns_none(self):
        self.assertIsNone(saved_fields_to_target(None, None, None))

    def test_unified_type_wins(self):
        self.assertEqual(
            saved_fields_to_target("acct_1", "INBOX", "Inbox"),
            {"unified": "Inbox"},
        )

    def test_account_and_folder_rebuild_the_target(self):
        self.assertEqual(
            saved_fields_to_target("acct_1", "INBOX", None),
            {"account_id": "acct_1", "folder": "INBOX"},
        )

    def test_half_a_pair_is_not_enough(self):
        self.assertIsNone(saved_fields_to_target("acct_1", None, None))
        self.assertIsNone(saved_fields_to_target(None, "INBOX", None))

    def test_round_trips_through_target_to_saved_fields(self):
        for target in (
            {"account_id": "acct_2", "folder": "Sent"},
            {"unified": "Sent"},
        ):
            fields = target_to_saved_fields(target)
            self.assertEqual(saved_fields_to_target(*fields), target)


class FolderAndSashSettingsRoundTrip(unittest.TestCase):
    def test_defaults(self):
        settings = Settings()
        self.assertIsNone(settings.last_selected_account_id)
        self.assertIsNone(settings.last_selected_folder)
        self.assertIsNone(settings.last_selected_unified_type)
        self.assertEqual(settings.outer_sash_position, 220)
        self.assertEqual(settings.inner_sash_position, 420)

    def test_to_dict_and_from_dict_round_trip(self):
        settings = Settings(
            last_selected_account_id="acct_9",
            last_selected_folder="Archive",
            last_selected_unified_type=None,
            outer_sash_position=260,
            inner_sash_position=500,
        )
        restored = Settings.from_dict(settings.to_dict())
        self.assertEqual(restored.last_selected_account_id, "acct_9")
        self.assertEqual(restored.last_selected_folder, "Archive")
        self.assertIsNone(restored.last_selected_unified_type)
        self.assertEqual(restored.outer_sash_position, 260)
        self.assertEqual(restored.inner_sash_position, 500)

    def test_from_dict_with_no_keys_uses_defaults(self):
        # A settings.json written before this finding has none of
        # these keys at all.
        restored = Settings.from_dict({})
        self.assertIsNone(restored.last_selected_account_id)
        self.assertEqual(restored.outer_sash_position, 220)
        self.assertEqual(restored.inner_sash_position, 420)


if __name__ == "__main__":
    unittest.main()
