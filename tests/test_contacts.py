"""
Audit finding 50: address book depth -- phone/notes fields on a
contact, and groups (named mailing lists of existing contacts).
Runs ContactManager against a real temp directory (contacts.json is
plain file I/O, no Himalaya CLI involved), same pattern
test_message_cache_maintenance.py uses for Paths.
"""

import os
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from paths import Paths
from contacts import ContactManager


def _make_paths(root):
    return Paths(
        base=root, app=root, apps_files=root, himalaya=root, data=root,
        userdata=os.path.join(root, "userdata"),
        cache=os.path.join(root, "cache"),
        config=os.path.join(root, "config"),
        logs=os.path.join(root, "logs"),
    )


class ContactsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = _make_paths(self._tmp.name)
        self.manager = ContactManager(self.paths)

    def _reload(self):
        """A fresh ContactManager reading back what _save() wrote,
        to catch a round-trip bug _load()/_save() alone wouldn't."""
        return ContactManager(self.paths)


class PhoneAndNotesFields(ContactsTestCase):
    def test_add_defaults_phone_and_notes_to_empty(self):
        self.manager.add("Jane", "jane@example.com")
        contact = self.manager.all_contacts()[0]
        self.assertEqual(contact["phone"], "")
        self.assertEqual(contact["notes"], "")

    def test_add_sets_phone_and_notes_on_a_new_contact(self):
        self.manager.add("Jane", "jane@example.com", phone="555-1234", notes="met at PyCon")
        contact = self.manager.all_contacts()[0]
        self.assertEqual(contact["phone"], "555-1234")
        self.assertEqual(contact["notes"], "met at PyCon")

    def test_add_never_blanks_an_existing_phone_or_notes(self):
        self.manager.add("Jane", "jane@example.com", phone="555-1234", notes="met at PyCon")
        self.manager.add("Jane", "jane@example.com")  # a later, blanker sighting
        contact = self.manager.all_contacts()[0]
        self.assertEqual(contact["phone"], "555-1234")
        self.assertEqual(contact["notes"], "met at PyCon")

    def test_add_returns_true_only_for_a_brand_new_contact(self):
        self.assertTrue(self.manager.add("Jane", "jane@example.com"))
        self.assertFalse(self.manager.add("Jane", "jane@example.com"))

    def test_upsert_always_sets_exactly_what_is_given_including_blank(self):
        self.manager.upsert("Jane", "jane@example.com", phone="555-1234", notes="old note")
        self.manager.upsert("Jane", "jane@example.com", phone="", notes="")
        contact = self.manager.all_contacts()[0]
        self.assertEqual(contact["phone"], "")
        self.assertEqual(contact["notes"], "")

    def test_round_trips_through_save_and_reload(self):
        self.manager.upsert("Jane", "jane@example.com", phone="555-1234", notes="met at PyCon")
        reloaded = self._reload()
        contact = reloaded.all_contacts()[0]
        self.assertEqual(contact["phone"], "555-1234")
        self.assertEqual(contact["notes"], "met at PyCon")


class VCardPhoneAndNotes(ContactsTestCase):
    def test_import_reads_tel_and_note(self):
        vcf_path = os.path.join(self._tmp.name, "in.vcf")
        with open(vcf_path, "w", encoding="utf-8") as handle:
            handle.write(
                "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Jane Doe\r\n"
                "TEL:555-1234\r\nNOTE:met at PyCon\r\n"
                "EMAIL;TYPE=INTERNET:jane@example.com\r\nEND:VCARD\r\n"
            )
        count = self.manager.import_vcard(vcf_path)
        self.assertEqual(count, 1)
        contact = self.manager.all_contacts()[0]
        self.assertEqual(contact["phone"], "555-1234")
        self.assertEqual(contact["notes"], "met at PyCon")

    def test_export_then_import_round_trips_phone_and_notes(self):
        self.manager.upsert("Jane", "jane@example.com", phone="555-1234", notes="met at PyCon")
        vcf_path = os.path.join(self._tmp.name, "out.vcf")
        self.manager.export_vcard(vcf_path)

        fresh = self._reload_empty()
        fresh.import_vcard(vcf_path)
        contact = fresh.all_contacts()[0]
        self.assertEqual(contact["phone"], "555-1234")
        self.assertEqual(contact["notes"], "met at PyCon")

    def test_export_writes_tel_and_note_before_email(self):
        # Regression guard: import_vcard applies whatever TEL/NOTE it
        # has seen SO FAR to the next EMAIL line (same "most recent
        # value seen" rule already used for FN) -- if export ever put
        # them after EMAIL again, the round-trip test above would
        # start failing the way it did until this was fixed.
        self.manager.upsert("Jane", "jane@example.com", phone="555-1234", notes="met at PyCon")
        vcf_path = os.path.join(self._tmp.name, "out.vcf")
        self.manager.export_vcard(vcf_path)
        with open(vcf_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertLess(text.index("TEL:"), text.index("EMAIL"))
        self.assertLess(text.index("NOTE:"), text.index("EMAIL"))

    def test_export_omits_tel_and_note_when_blank(self):
        self.manager.add("Jane", "jane@example.com")
        vcf_path = os.path.join(self._tmp.name, "out.vcf")
        self.manager.export_vcard(vcf_path)
        with open(vcf_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("TEL:", text)
        self.assertNotIn("NOTE:", text)

    def _reload_empty(self):
        other_root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(other_root, ignore_errors=True))
        return ContactManager(_make_paths(other_root))


class CsvPhoneAndNotes(ContactsTestCase):
    def test_import_with_header_reads_phone_and_notes_columns(self):
        csv_path = os.path.join(self._tmp.name, "in.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("Name,Email,Phone,Notes\r\nJane Doe,jane@example.com,555-1234,met at PyCon\r\n")
        count = self.manager.import_csv(csv_path)
        self.assertEqual(count, 1)
        contact = self.manager.all_contacts()[0]
        self.assertEqual(contact["phone"], "555-1234")
        self.assertEqual(contact["notes"], "met at PyCon")

    def test_export_writes_all_four_columns(self):
        self.manager.upsert("Jane", "jane@example.com", phone="555-1234", notes="met at PyCon")
        csv_path = os.path.join(self._tmp.name, "out.csv")
        self.manager.export_csv(csv_path)
        with open(csv_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Name,Email,Phone,Notes", text)
        self.assertIn("555-1234", text)
        self.assertIn("met at PyCon", text)

    def test_fallback_no_header_shape_has_no_phone_or_notes(self):
        # No recognizable header: column 0/1 are name/email only, per
        # import_csv's own documented fallback -- nothing to
        # distinguish a phone/notes column by position alone.
        csv_path = os.path.join(self._tmp.name, "in.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("Jane Doe,jane@example.com\r\n")
        self.manager.import_csv(csv_path)
        contact = self.manager.all_contacts()[0]
        self.assertEqual(contact["phone"], "")
        self.assertEqual(contact["notes"], "")


class Groups(ContactsTestCase):
    def setUp(self):
        super().setUp()
        self.manager.add("Alice", "alice@example.com")
        self.manager.add("Bob", "bob@example.com")
        self.manager.add("Carol", "carol@example.com")

    def test_new_group_starts_empty(self):
        self.manager.add_group("Team")
        self.assertEqual(self.manager.group_members("Team"), [])

    def test_add_group_is_idempotent_and_keeps_original_casing(self):
        self.manager.add_group("Team")
        name = self.manager.add_group("team")  # different case, same group
        self.assertEqual(name, "Team")
        self.assertEqual(len(self.manager.all_groups()), 1)

    def test_add_to_group_and_group_members(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.add_to_group("Team", "bob@example.com")
        members = self.manager.group_members("Team")
        self.assertEqual({m["email"] for m in members}, {"alice@example.com", "bob@example.com"})

    def test_add_to_group_does_not_duplicate_a_member(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.add_to_group("Team", "alice@example.com")
        self.assertEqual(len(self.manager.group_members("Team")), 1)

    def test_remove_from_group(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.remove_from_group("Team", "alice@example.com")
        self.assertEqual(self.manager.group_members("Team"), [])

    def test_deleting_a_contact_removes_it_from_every_group(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.remove("alice@example.com")
        self.assertEqual(self.manager.group_members("Team"), [])

    def test_deleting_a_contact_does_not_touch_other_groups_membership_lists(self):
        self.manager.add_group("Team")
        self.manager.add_group("Friends")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.add_to_group("Friends", "bob@example.com")
        self.manager.remove("alice@example.com")
        self.assertEqual([m["email"] for m in self.manager.group_members("Friends")], ["bob@example.com"])

    def test_rename_group(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.assertTrue(self.manager.rename_group("Team", "Squad"))
        self.assertEqual([g["name"] for g in self.manager.all_groups()], ["Squad"])
        self.assertEqual(len(self.manager.group_members("Squad")), 1)

    def test_rename_group_refuses_a_name_collision(self):
        self.manager.add_group("Team")
        self.manager.add_group("Friends")
        self.assertFalse(self.manager.rename_group("Team", "Friends"))
        self.assertEqual({g["name"] for g in self.manager.all_groups()}, {"Team", "Friends"})

    def test_rename_nonexistent_group_is_a_no_op(self):
        self.assertFalse(self.manager.rename_group("Ghost", "Team"))

    def test_remove_group_deletes_the_group_not_its_contacts(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.remove_group("Team")
        self.assertEqual(self.manager.all_groups(), [])
        self.assertEqual(len(self.manager.all_contacts()), 3)

    def test_set_group_members_replaces_the_whole_list(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.set_group_members("Team", ["bob@example.com", "carol@example.com"])
        self.assertEqual(
            {m["email"] for m in self.manager.group_members("Team")},
            {"bob@example.com", "carol@example.com"},
        )

    def test_search_groups_prefix_match_case_insensitive(self):
        self.manager.add_group("Teammates")
        self.manager.add_group("Family")
        self.assertEqual([g["name"] for g in self.manager.search_groups("team")], ["Teammates"])

    def test_search_groups_empty_prefix_returns_nothing(self):
        self.manager.add_group("Team")
        self.assertEqual(self.manager.search_groups(""), [])

    def test_group_members_skips_a_deleted_contact_defensively(self):
        # Guards against a stale reference surviving some other path
        # into the members list besides remove() itself.
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager._contacts.pop("alice@example.com")
        self.assertEqual(self.manager.group_members("Team"), [])

    def test_groups_round_trip_through_save_and_reload(self):
        self.manager.add_group("Team")
        self.manager.add_to_group("Team", "alice@example.com")
        self.manager.add_to_group("Team", "bob@example.com")
        reloaded = self._reload()
        members = {m["email"] for m in reloaded.group_members("Team")}
        self.assertEqual(members, {"alice@example.com", "bob@example.com"})

    def test_loading_a_contacts_file_with_no_groups_key_is_fine(self):
        # contacts.json written before this feature existed.
        import json
        with open(self.manager._file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.pop("groups", None)
        with open(self.manager._file, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        reloaded = self._reload()
        self.assertEqual(reloaded.all_groups(), [])


if __name__ == "__main__":
    unittest.main()
