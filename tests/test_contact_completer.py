"""
Audit finding 50: compose_panel._ContactCompleter expanding a group
name to every member's address. Runs under the wx stub -- Start()/
GetNext() are plain Python logic over a fake contacts provider, no
real wx autocomplete popup involved.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from compose_panel import _ContactCompleter


class _FakeContacts:
    def __init__(self, contacts=None, groups=None):
        # contacts: list of {"name", "email"}; groups: {name: [email, ...]}
        self._contacts = contacts or []
        self._groups = groups or {}

    def search(self, prefix):
        prefix = prefix.lower()
        return [
            c for c in self._contacts
            if c.get("name", "").lower().startswith(prefix) or c["email"].lower().startswith(prefix)
        ]

    def search_groups(self, prefix):
        prefix = prefix.lower()
        return [{"name": name} for name in self._groups if name.lower().startswith(prefix)]

    def group_members(self, name):
        return [c for c in self._contacts if c["email"] in self._groups.get(name, [])]


def _completer(contacts):
    return _ContactCompleter(lambda: contacts)


class ContactCompleterMatches(unittest.TestCase):
    def test_matches_a_plain_contact(self):
        contacts = _FakeContacts(contacts=[{"name": "Jane", "email": "jane@example.com"}])
        completer = _completer(contacts)
        self.assertTrue(completer.Start("ja"))
        self.assertEqual(completer.GetNext(), "Jane <jane@example.com>")
        self.assertEqual(completer.GetNext(), "")

    def test_empty_segment_has_no_matches(self):
        contacts = _FakeContacts(contacts=[{"name": "Jane", "email": "jane@example.com"}])
        completer = _completer(contacts)
        self.assertFalse(completer.Start(""))

    def test_preserves_text_before_the_last_comma(self):
        contacts = _FakeContacts(contacts=[{"name": "Jane", "email": "jane@example.com"}])
        completer = _completer(contacts)
        completer.Start("bob@example.com, ja")
        self.assertEqual(completer.GetNext(), "bob@example.com, Jane <jane@example.com>")


class ContactCompleterGroupExpansion(unittest.TestCase):
    def test_matching_group_expands_to_every_member(self):
        contacts = _FakeContacts(
            contacts=[
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "Bob", "email": "bob@example.com"},
            ],
            groups={"Team": ["alice@example.com", "bob@example.com"]},
        )
        completer = _completer(contacts)
        completer.Start("team")
        self.assertEqual(completer.GetNext(), "Alice <alice@example.com>, Bob <bob@example.com>")

    def test_group_expansion_preserves_text_before_the_last_comma(self):
        contacts = _FakeContacts(
            contacts=[{"name": "Alice", "email": "alice@example.com"}],
            groups={"Team": ["alice@example.com"]},
        )
        completer = _completer(contacts)
        completer.Start("carol@example.com, team")
        self.assertEqual(completer.GetNext(), "carol@example.com, Alice <alice@example.com>")

    def test_empty_group_contributes_no_match(self):
        contacts = _FakeContacts(groups={"Empty": []})
        completer = _completer(contacts)
        self.assertFalse(completer.Start("empty"))

    def test_contacts_and_groups_can_both_match_the_same_prefix(self):
        # "Team Lead" the contact and "Team" the group both start
        # with "team" -- both should show up as separate candidates,
        # the direct contact match and the group's expansion.
        contacts = _FakeContacts(
            contacts=[
                {"name": "Team Lead", "email": "lead@example.com"},
                {"name": "Alice", "email": "alice@example.com"},
            ],
            groups={"Team": ["alice@example.com"]},
        )
        completer = _completer(contacts)
        completer.Start("team")
        matches = []
        while True:
            value = completer.GetNext()
            if not value:
                break
            matches.append(value)
        self.assertIn("Team Lead <lead@example.com>", matches)
        self.assertIn("Alice <alice@example.com>", matches)


if __name__ == "__main__":
    unittest.main()
