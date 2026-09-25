"""
junk_rules: the fixed checks that replaced the learned spam filter on
16 September 2026, and ExpectedArrivals, which stops ZBox's own moves
from being greeted as new mail.

The cases that matter most are the two that went wrong live: mailing
list mail must never put the whole list on the blocked list, and a
message the user marked Not Junk must never be judged again.
"""

import os
import shutil
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import junk_rules
from junk_rules import ALLOWED, BLOCKED, ExpectedArrivals, JunkRules


def _env(sender, header="<m1@x>", to=("me@example.com",)):
    return {
        "id": "1",
        "message-id": header,
        "from": [{"name": "Someone", "email": sender}],
        "to": [{"name": None, "email": address} for address in to],
    }


class _WithRules(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="zbox_junk_rules_")
        self.path = os.path.join(self.temp, "junk_rules.json")
        self.rules = JunkRules(self.path)

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)


class MarkAsJunk(_WithRules):
    def test_the_sender_is_blocked(self):
        self.assertEqual(self.rules.block_senders([_env("spam@bad.example")]), 1)
        self.assertEqual(self.rules.state_for(_env("SPAM@bad.example", "<other@x>")), BLOCKED)

    def test_mailing_list_mail_never_blocks_the_list(self):
        post = _env("nvda-addons@groups.io", to=("nvda-addons@groups.io",))
        self.rules.block_senders([post])
        self.assertEqual(self.rules.blocked, [])
        self.assertIsNone(self.rules.state_for(post))

    def test_your_own_address_is_never_blocked(self):
        self.rules.block_senders([_env("me@example.com")], own_addresses=["Me@Example.com"])
        self.assertEqual(self.rules.blocked, [])

    def test_it_replaces_a_never_junk_entry(self):
        self.rules.allow_senders([_env("x@example.org", "<a@x>")])
        self.rules.block_senders([_env("x@example.org", "<b@x>")])
        self.assertEqual(self.rules.blocked, ["x@example.org"])
        self.assertEqual(self.rules.allowed, [])


class MarkAsNotJunk(_WithRules):
    def test_the_message_is_exempt_for_good(self):
        message = _env("news@shop.example", "<keep@x>")
        self.rules.block_senders([_env("news@shop.example", "<old@x>")])
        self.rules.allow_senders([message])
        self.assertEqual(self.rules.state_for(message), ALLOWED)
        self.assertEqual(JunkRules(self.path).state_for(message), ALLOWED)

    def test_the_sender_moves_from_blocked_to_never_junk(self):
        self.rules.block_senders([_env("news@shop.example", "<old@x>")])
        self.rules.allow_senders([_env("news@shop.example", "<keep@x>")])
        self.assertEqual(self.rules.blocked, [])
        self.assertEqual(self.rules.allowed, ["news@shop.example"])
        self.assertEqual(self.rules.state_for(_env("news@shop.example", "<later@x>")), ALLOWED)

    def test_an_exempt_message_wins_over_a_later_block(self):
        message = _env("friend@example.net", "<keep@x>")
        self.rules.allow_senders([message])
        self.rules.set_lists(["friend@example.net"], [])
        self.assertEqual(self.rules.state_for(message), ALLOWED)

    def test_message_ids_match_regardless_of_brackets_and_case(self):
        self.rules.allow_senders([_env("a@example.net", "<ABC@Example.com>")])
        self.assertEqual(self.rules.state_for(_env("b@example.net", "abc@example.com")), ALLOWED)


class Undo(_WithRules):
    def test_undoing_junk_unblocks_only_what_it_added(self):
        self.rules.block_senders([_env("old@bad.example", "<a@x>")])
        self.rules.block_senders([_env("old@bad.example", "<b@x>"), _env("new@bad.example", "<c@x>")])
        self.rules.undo_block(["<b@x>", "<c@x>"])
        self.assertEqual(self.rules.blocked, ["old@bad.example"])

    def test_undoing_junk_restores_a_never_junk_entry_it_replaced(self):
        self.rules.allow_senders([_env("x@example.org", "<a@x>")])
        self.rules.block_senders([_env("x@example.org", "<b@x>")])
        self.rules.undo_block(["<b@x>"])
        self.assertEqual(self.rules.allowed, ["x@example.org"])
        self.assertEqual(self.rules.blocked, [])

    def test_undoing_not_junk_removes_the_exemption_and_restores_the_block(self):
        self.rules.block_senders([_env("x@example.org", "<a@x>")])
        message = _env("x@example.org", "<b@x>")
        self.rules.allow_senders([message])
        self.rules.undo_allow(["<b@x>"])
        self.assertEqual(self.rules.state_for(message), BLOCKED)


class Storage(_WithRules):
    def test_a_missing_or_broken_file_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("not json")
        rules = JunkRules(self.path)
        self.assertEqual((rules.blocked, rules.allowed), ([], []))

    def test_lists_written_back_by_the_dialog_are_saved(self):
        self.rules.block_senders([_env("a@bad.example", "<a@x>"), _env("b@bad.example", "<b@x>")])
        self.rules.set_lists(["b@bad.example"], [])
        self.assertEqual(JunkRules(self.path).blocked, ["b@bad.example"])


class LinkHosts(unittest.TestCase):
    def test_hosts_are_found_in_html_and_plain_text(self):
        raw = 'Visit <a href="https://Login.Evil.example/x">here</a> or www.other.example/page.'
        self.assertEqual(junk_rules.link_hosts(raw), {"login.evil.example", "www.other.example"})

    def test_a_quoted_printable_break_inside_a_link_is_joined(self):
        raw = "href=3D\"https://evil.exa=\r\nmple/login\""
        self.assertIn("evil.example", junk_rules.link_hosts(raw))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(junk_rules.link_hosts(""), set())


class ExpectedArrivalsTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.arrivals = ExpectedArrivals(ttl=60, clock=lambda: self.now[0])

    def test_a_message_zbox_moved_is_not_new_mail(self):
        self.arrivals.add("acct", "INBOX", ["<moved@x>"])
        kept = self.arrivals.filter_new("acct", "inbox", [_env("a@x.example", "<moved@x>"), _env("b@x.example", "<real@x>")])
        self.assertEqual([e["message-id"] for e in kept], ["<real@x>"])

    def test_each_expectation_is_used_once(self):
        self.arrivals.add("acct", "INBOX", ["<moved@x>"])
        self.arrivals.filter_new("acct", "INBOX", [_env("a@x.example", "<moved@x>")])
        kept = self.arrivals.filter_new("acct", "INBOX", [_env("a@x.example", "<moved@x>")])
        self.assertEqual(len(kept), 1)

    def test_other_accounts_and_folders_are_unaffected(self):
        self.arrivals.add("acct", "INBOX", ["<moved@x>"])
        self.assertEqual(len(self.arrivals.filter_new("other", "INBOX", [_env("a@x.example", "<moved@x>")])), 1)
        self.assertEqual(len(self.arrivals.filter_new("acct", "Archive", [_env("a@x.example", "<moved@x>")])), 1)

    def test_an_expectation_expires(self):
        self.arrivals.add("acct", "INBOX", ["<moved@x>"])
        self.now[0] += 61
        self.assertEqual(len(self.arrivals.filter_new("acct", "INBOX", [_env("a@x.example", "<moved@x>")])), 1)


if __name__ == "__main__":
    unittest.main()
