"""
Import and export -- what actually travels between two machines.

Audit B named this the second daily user path with nothing testing
its behaviour. The risk here is not a crash: it is an export file
that quietly carries something it should not (a password), or an
import that quietly overwrites an account already configured on the
target machine. Both are silent, and both are exactly what these
tests watch.

All five public names of import_export are reached:
build_export_data, write_export_file, read_export_file,
accounts_from_export_data and apply_portable_settings.

Every file written here lives in a temporary folder. Nothing touches
the real config folder, and no AccountManager is constructed -- these
are the pure functions, and assigning ids and regenerating
himalaya.toml is add_account's job, tested elsewhere.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import import_export
from account_manager import Account
from import_export import (
    PORTABLE_SETTINGS_FIELDS,
    accounts_from_export_data,
    apply_portable_settings,
    build_export_data,
    read_export_file,
    write_export_file,
)
from settings_manager import Settings


def sample_account():
    return Account(
        account_id="oldid1234567",
        display_name="Work",
        login_email="someone@example.com",
        password="hunter2",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )


class ExportShapeTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.data = build_export_data([sample_account()], self.settings)

    def test_the_file_says_what_it_is(self):
        self.assertEqual(self.data["zbox_export_version"],
                         import_export.FORMAT_VERSION)
        self.assertEqual(len(self.data["accounts"]), 1)
        self.assertIsInstance(self.data["settings"], dict)

    def test_no_password_reaches_the_file_in_any_form(self):
        # Asserted against the whole serialized file rather than one
        # field: the point is that nothing anywhere in it is a
        # password, however the structure changes later.
        dumped = json.dumps(self.data)
        self.assertNotIn("hunter2", dumped)
        self.assertNotIn("password", dumped)

    def test_the_account_itself_still_travels(self):
        account = self.data["accounts"][0]
        self.assertEqual(account["login_email"], "someone@example.com")
        self.assertEqual(account["imap_host"], "imap.example.com")
        self.assertEqual(account["smtp_host"], "smtp.example.com")

    def test_machine_specific_settings_stay_behind(self):
        exported = self.data["settings"]
        for key in ("window_x", "window_y", "window_maximized",
                    "outer_sash_position", "inner_sash_position",
                    "last_selected_account_id", "last_selected_folder",
                    "last_selected_unified_type"):
            self.assertNotIn(key, exported)

    def test_preferences_do_travel(self):
        exported = self.data["settings"]
        for key in ("default_message_view", "block_remote_content",
                    "announcement_hold_ms", "sound_theme"):
            self.assertIn(key, exported)

    def test_write_then_read_round_trips(self):
        temp = tempfile.mkdtemp(prefix="zbox_export_")
        try:
            path = os.path.join(temp, "zbox_export.json")
            written = write_export_file(path, [sample_account()], self.settings)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(read_export_file(path), written)
        finally:
            shutil.rmtree(temp, ignore_errors=True)


class ImportAccountsTests(unittest.TestCase):
    def setUp(self):
        self.data = build_export_data([sample_account()], Settings())

    def test_the_exported_id_is_discarded(self):
        imported = accounts_from_export_data(self.data)
        self.assertEqual(len(imported), 1)
        self.assertNotEqual(imported[0].account_id, "oldid1234567")
        self.assertEqual(len(imported[0].account_id), 12)

    def test_importing_twice_adds_rather_than_overwrites(self):
        first = accounts_from_export_data(self.data)[0]
        second = accounts_from_export_data(self.data)[0]
        self.assertNotEqual(first.account_id, second.account_id)

    def test_an_imported_account_arrives_with_no_password(self):
        imported = accounts_from_export_data(self.data)[0]
        self.assertEqual(imported.password, "")

    def test_the_rest_of_the_account_survives_the_trip(self):
        imported = accounts_from_export_data(self.data)[0]
        self.assertEqual(imported.display_name, "Work")
        self.assertEqual(imported.login_email, "someone@example.com")
        self.assertEqual(imported.imap_host, "imap.example.com")

    def test_something_that_is_not_an_export_file_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            accounts_from_export_data(["not", "an", "export"])
        self.assertIn("Not a ZBox export file", str(caught.exception))

    def test_junk_entries_inside_the_account_list_are_skipped(self):
        data = {"accounts": [{"login_email": "a@example.com"}, "junk", 5, None]}
        imported = accounts_from_export_data(data)
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].login_email, "a@example.com")

    def test_a_file_with_no_accounts_is_not_an_error(self):
        self.assertEqual(accounts_from_export_data({"settings": {}}), [])
        self.assertEqual(accounts_from_export_data({"accounts": None}), [])


class PortableSettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_allowlisted_fields_are_merged(self):
        apply_portable_settings(self.settings, {"settings": {
            "sort_key": "subject",
            "draft_autosave_seconds": 15,
            "sound_theme": "quiet",
        }})
        self.assertEqual(self.settings.sort_key, "subject")
        self.assertEqual(self.settings.draft_autosave_seconds, 15)
        self.assertEqual(self.settings.sound_theme, "quiet")

    def test_machine_specific_fields_are_ignored_even_when_present(self):
        self.settings.window_x = 100
        self.settings.outer_sash_position = 220
        apply_portable_settings(self.settings, {"settings": {
            "window_x": 9999,
            "outer_sash_position": 9999,
            "last_selected_account_id": "someone elses account",
        }})
        self.assertEqual(self.settings.window_x, 100)
        self.assertEqual(self.settings.outer_sash_position, 220)
        self.assertIsNone(self.settings.last_selected_account_id)

    def test_fields_absent_from_an_older_export_are_left_alone(self):
        before = self.settings.sound_theme
        apply_portable_settings(self.settings, {"settings": {"sort_key": "from"}})
        self.assertEqual(self.settings.sound_theme, before)

    def test_a_payload_that_is_not_a_dict_does_nothing(self):
        before = self.settings.sort_key
        apply_portable_settings(self.settings, ["nonsense"])
        apply_portable_settings(self.settings, None)
        self.assertEqual(self.settings.sort_key, before)

    def test_a_settings_section_that_is_not_a_dict_does_nothing(self):
        before = self.settings.sort_key
        apply_portable_settings(self.settings, {"settings": "nonsense"})
        self.assertEqual(self.settings.sort_key, before)

    def test_every_portable_field_still_exists(self):
        """A drift guard. Renaming a setting without updating the
        allowlist would silently drop it out of every future export,
        and nothing else in the app would notice."""
        keys = Settings().to_dict()
        for field in PORTABLE_SETTINGS_FIELDS:
            self.assertIn(field, keys,
                          "%s is on the export allowlist but is no longer a "
                          "setting" % field)


if __name__ == "__main__":
    unittest.main()
