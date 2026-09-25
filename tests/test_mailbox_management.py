"""
Audit finding 42 (Folder management): create_mailbox, delete_mailbox,
rename_mailbox and subscribe_mailbox/unsubscribe_mailbox each build
one flat, protocol-specific 'imap <verb>' subprocess call ('imap
create', 'imap delete', ...) -- confirmed live against the pinned
himalaya.exe's own --help, session k, 13 September 2026: the shared
'mailbox <verb>' shape these tests originally asserted was never a
real path (himalaya.exe mailbox --help lists only 'list' and 'help').
list_all_folders(counts=False) sends the same flat 'imap list --all'
(also confirmed live; 'imap mailbox list --all' isn't real either);
list_all_folders(counts=True) is the one call that DOES use the
shared 'mailbox list', with '--counts'. No live account here to catch
a wrong argv, so the argv itself is what these tests pin down. mock
hc._run entirely.

Audit finding 43 (unread counts): list_all_folders(counts=True) adds
'--counts'; _mailbox_unread_count and _folder_tree_list/_folder_tree_label
turn the resulting per-mailbox 'unread' field (also confirmed via
strings -- see himalaya_client.list_all_folders's own docstring) into
tree labels, tested here as pure functions.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc
from envelope_format import _ordered_folder_list, _folder_tree_label


class MailboxManagementArgvConstruction(unittest.TestCase):
    def test_create_mailbox(self):
        with mock.patch.object(hc, "_run") as run:
            hc.create_mailbox("paths", "acct", "Projects")
        args, kwargs = run.call_args
        self.assertEqual(args[0], "paths")
        self.assertEqual(args[1], "acct")
        self.assertEqual(args[2], ["imap", "create", "Projects"])
        self.assertEqual(kwargs.get("backend"), "imap")

    def test_delete_mailbox(self):
        with mock.patch.object(hc, "_run") as run:
            hc.delete_mailbox("paths", "acct", "Old Stuff")
        args, kwargs = run.call_args
        self.assertEqual(args[2], ["imap", "delete", "Old Stuff"])
        self.assertEqual(kwargs.get("backend"), "imap")

    def test_rename_mailbox(self):
        with mock.patch.object(hc, "_run") as run:
            hc.rename_mailbox("paths", "acct", "Old Name", "New Name")
        args, kwargs = run.call_args
        self.assertEqual(args[2], ["imap", "rename", "Old Name", "New Name"])
        self.assertEqual(kwargs.get("backend"), "imap")

    def test_subscribe_mailbox(self):
        with mock.patch.object(hc, "_run") as run:
            hc.subscribe_mailbox("paths", "acct", "Projects")
        args, kwargs = run.call_args
        self.assertEqual(args[2], ["imap", "subscribe", "Projects"])
        self.assertEqual(kwargs.get("backend"), "imap")

    def test_unsubscribe_mailbox(self):
        with mock.patch.object(hc, "_run") as run:
            hc.unsubscribe_mailbox("paths", "acct", "Projects")
        args, kwargs = run.call_args
        self.assertEqual(args[2], ["imap", "unsubscribe", "Projects"])
        self.assertEqual(kwargs.get("backend"), "imap")

    def test_list_all_folders_uses_the_imap_subcommand_for_all(self):
        # '--all' belongs to the flat, protocol-specific 'imap list'
        # subcommand, not the shared 'mailbox list' and not a nested
        # 'imap mailbox list' either -- confirmed live, session k,
        # 13 September 2026 ('imap mailbox --help': "unrecognized
        # subcommand 'mailbox'"). This test asserted the shared
        # command with '--all' first (which shipped and failed in
        # the field: every folder listing came back "error:
        # unexpected argument '--all' found"), then the nested
        # 'imap mailbox list --all' shape (which also doesn't exist
        # and went unnoticed because the running app never calls
        # this path -- only this argv check does).
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"mailboxes": [{"name": "INBOX"}, {"name": "Projects"}]}
            result = hc.list_all_folders("paths", "acct")
        args, kwargs = run.call_args
        self.assertEqual(args[2], ["imap", "list", "--all"])
        self.assertEqual(kwargs.get("backend"), "imap")
        self.assertEqual(result, [{"name": "INBOX"}, {"name": "Projects"}])

    def test_list_all_folders_counts_true_uses_the_shared_command(self):
        # '--counts' is the mirror image: only the shared
        # MailboxListCommand reports TOTAL/UNREAD, and it does not
        # accept '--all'. The two flags cannot be combined.
        with mock.patch.object(hc, "_run") as run:
            run.return_value = []
            hc.list_all_folders("paths", "acct", counts=True)
        args, kwargs = run.call_args
        self.assertEqual(args[2], ["mailbox", "list", "--counts"])
        self.assertNotIn("--all", args[2])

    def test_list_folders_unchanged_no_all_flag(self):
        # Move To / Copy To's existing subscribed-only fetch must not
        # regress just because list_all_folders was added alongside it.
        with mock.patch.object(hc, "_run") as run:
            run.return_value = []
            hc.list_folders("paths", "acct")
        args, kwargs = run.call_args
        self.assertEqual(args[2], ["mailbox", "list"])


class MailboxUnreadCountTests(unittest.TestCase):
    def test_reads_an_int_unread_field(self):
        self.assertEqual(hc._mailbox_unread_count({"name": "INBOX", "unread": 4}), 4)

    def test_zero_is_a_real_count_not_unknown(self):
        self.assertEqual(hc._mailbox_unread_count({"name": "INBOX", "unread": 0}), 0)

    def test_missing_field_is_unknown(self):
        self.assertIsNone(hc._mailbox_unread_count({"name": "INBOX"}))

    def test_null_field_is_unknown(self):
        self.assertIsNone(hc._mailbox_unread_count({"name": "INBOX", "unread": None}))

    def test_non_int_field_is_unknown_not_a_crash(self):
        self.assertIsNone(hc._mailbox_unread_count({"name": "INBOX", "unread": "many"}))

    def test_bool_is_rejected_despite_being_an_int_subclass(self):
        # isinstance(True, int) is True in Python -- guard against a
        # stray boolean masquerading as a count.
        self.assertIsNone(hc._mailbox_unread_count({"name": "INBOX", "unread": True}))

    def test_non_dict_entry_is_unknown_not_a_crash(self):
        self.assertIsNone(hc._mailbox_unread_count("INBOX"))


class FolderTreeLabelTests(unittest.TestCase):
    def test_positive_count_is_appended(self):
        self.assertEqual(_folder_tree_label("Inbox", 4), "Inbox (4 unread)")

    def test_zero_count_shows_plain_label(self):
        self.assertEqual(_folder_tree_label("Inbox", 0), "Inbox")

    def test_unknown_count_shows_plain_label(self):
        self.assertEqual(_folder_tree_label("Inbox", None), "Inbox")


class _Account:
    def __init__(self, imap_host):
        self.imap_host = imap_host


class OrderedFolderListTests(unittest.TestCase):
    def test_special_folders_first_in_folder_types_order_then_customs_alphabetically(self):
        account = _Account("imap.example.com")
        raw = ["Trash", "Zeta", "INBOX", "Alpha", "Sent", "Drafts"]
        result = _ordered_folder_list(account, raw)
        self.assertEqual(
            result,
            [
                ("Inbox", "INBOX", None),
                ("Sent", "Sent", None),
                ("Drafts", "Drafts", None),
                ("Trash", "Trash", None),
                ("Alpha", "Alpha", None),
                ("Zeta", "Zeta", None),
            ],
        )

    def test_missing_special_folder_is_simply_absent(self):
        # A non-Gmail account with no real Archive/Junk folder yet
        # shouldn't show an empty/placeholder row for either.
        account = _Account("imap.example.com")
        raw = ["INBOX", "Sent"]
        result = _ordered_folder_list(account, raw)
        self.assertEqual(result, [("Inbox", "INBOX", None), ("Sent", "Sent", None)])

    def test_gmail_special_names_resolve_through_provider_presets(self):
        account = _Account("imap.gmail.com")
        raw = ["INBOX", "[Gmail]/Sent Mail", "[Gmail]/Trash", "Newsletters"]
        result = _ordered_folder_list(account, raw)
        pairs = [(label, real) for label, real, _unread in result]
        self.assertIn(("Sent", "[Gmail]/Sent Mail"), pairs)
        self.assertIn(("Trash", "[Gmail]/Trash"), pairs)
        self.assertIn(("Newsletters", "Newsletters"), pairs)
        # Gmail's real Archive concept ("All Mail") isn't one of the
        # raw names here, so no Archive row should appear.
        self.assertNotIn("Archive", [label for label, _real, _unread in result])

    def test_a_custom_folder_that_happens_to_match_no_special_name_stays_raw(self):
        account = _Account("imap.example.com")
        raw = ["INBOX", "My Custom Folder"]
        result = _ordered_folder_list(account, raw)
        self.assertEqual(
            result,
            [("Inbox", "INBOX", None), ("My Custom Folder", "My Custom Folder", None)],
        )

    def test_unread_counts_are_read_through_by_real_name(self):
        account = _Account("imap.gmail.com")
        raw = ["INBOX", "[Gmail]/Sent Mail", "Newsletters"]
        counts = {"INBOX": 4, "[Gmail]/Sent Mail": 0, "Newsletters": None}
        result = _ordered_folder_list(account, raw, unread_counts=counts)
        by_label = {label: unread for label, _real, unread in result}
        self.assertEqual(by_label["Inbox"], 4)
        self.assertEqual(by_label["Sent"], 0)
        self.assertIsNone(by_label["Newsletters"])

    def test_a_real_name_missing_from_unread_counts_is_none_not_a_crash(self):
        account = _Account("imap.example.com")
        raw = ["INBOX", "Sent"]
        result = _ordered_folder_list(account, raw, unread_counts={"INBOX": 2})
        by_label = {label: unread for label, _real, unread in result}
        self.assertEqual(by_label["Inbox"], 2)
        self.assertIsNone(by_label["Sent"])


if __name__ == "__main__":
    unittest.main()
