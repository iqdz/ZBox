"""
Audit finding 39 (Search): search_envelopes() builds the 'envelope
search' subprocess call against the pinned Himalaya build's query
DSL, confirmed directly from himalaya/himalaya.exe's own embedded help
text (see the function's docstring) rather than the project's public
docs, which describe a different/older Himalaya version. These tests
mock hc._run entirely -- the point is verifying the exact argv this
function hands to Himalaya, not Himalaya's own behavior, since a
wrong argv here would search a live IMAP server incorrectly with no
local way to notice.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc


class SearchEnvelopesArgvConstruction(unittest.TestCase):
    def test_query_matches_subject_from_or_body(self):
        # Every condition/pattern must be its own argv element -- the
        # DSL's QUERY argument is a trailing variadic positional, so
        # a joined string here would be silently wrong.
        with mock.patch.object(hc, "_run") as run:
            run.return_value = []
            hc.search_envelopes("paths", "acct", "invoice", "INBOX")

        args, kwargs = run.call_args
        paths, account, argv = args[0], args[1], args[2]
        self.assertEqual(paths, "paths")
        self.assertEqual(account, "acct")
        self.assertEqual(argv[:2], ["envelope", "search"])
        query = argv[argv.index("--page-size") + 2:]
        self.assertEqual(
            query,
            ["subject", "invoice", "or", "from", "invoice", "or", "body", "invoice"],
        )

    def test_folder_and_page_size_flags_precede_the_query(self):
        # A trailing catch-all positional in a clap CLI can swallow
        # anything placed after it starts -- flags included -- so
        # -m/--page-size must come before the query tokens, not after.
        with mock.patch.object(hc, "_run") as run:
            run.return_value = []
            hc.search_envelopes("paths", "acct", "term", "Archive", page_size=50)

        argv = run.call_args.args[2]
        self.assertEqual(
            argv,
            ["envelope", "search", "-m", "Archive", "--page-size", "50",
             "subject", "term", "or", "from", "term", "or", "body", "term"],
        )

    def test_backend_and_timeout_are_passed_through(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = []
            hc.search_envelopes("paths", "acct", "term", "INBOX", backend="maildir")

        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("backend"), "maildir")
        self.assertEqual(kwargs.get("timeout"), 45)

    def test_a_search_term_containing_spaces_is_escaped_not_split(self):
        # Himalaya's own query DSL rejoins every trailing positional
        # argv element back into one string and re-tokenizes THAT
        # with its own space-delimited grammar -- confirmed live via
        # a real zbox_debug.log where an unescaped multi-word term
        # produced the identical DSL parse error across every folder
        # in both accounts (finding-39 follow-up). Escaping the space
        # (see EscapeSearchPatternHelper below) is what keeps a
        # multi-word term as one pattern token instead of ending it
        # early.
        with mock.patch.object(hc, "_run") as run:
            run.return_value = []
            hc.search_envelopes("paths", "acct", "quarterly report", "INBOX")

        argv = run.call_args.args[2]
        self.assertEqual(argv.count("quarterly\\ report"), 3)

    def test_returns_plain_list_from_as_list(self):
        with mock.patch.object(hc, "_run") as run:
            run.return_value = {"envelopes": [{"id": "1"}, {"id": "2"}]}
            result = hc.search_envelopes("paths", "acct", "term", "INBOX")

        self.assertEqual(result, [{"id": "1"}, {"id": "2"}])


class EscapeSearchPatternHelper(unittest.TestCase):
    """
    _escape_search_pattern's own unit tests -- pure Python, no
    subprocess/wx involved, so unlike SearchTabPanel/ZBoxMainFrame
    this is directly testable (same reasoning as
    tests/test_account_gate.py for _AccountGate). Every case here
    was confirmed against the pinned himalaya v2.1.0 x86_64-linux
    release in a sandboxed maildir account before being hardcoded --
    see _escape_search_pattern's own docstring for that evidence.
    """

    def test_word_with_no_special_characters_is_unchanged(self):
        self.assertEqual(hc._escape_search_pattern("invoice"), "invoice")

    def test_space_is_escaped(self):
        self.assertEqual(hc._escape_search_pattern("memory bank"), "memory\\ bank")

    def test_parens_are_escaped(self):
        self.assertEqual(hc._escape_search_pattern("(notes)"), "\\(notes\\)")

    def test_backslash_is_escaped_before_the_other_characters(self):
        # Confirmed live: an unescaped backslash makes Himalaya's DSL
        # parser choke on whatever character follows it, and escaping
        # backslash after the space/paren replacements would
        # double-escape the backslashes those had just inserted -- so
        # backslash has to go first.
        self.assertEqual(
            hc._escape_search_pattern("C:\\temp (notes)"),
            "C:\\\\temp\\ \\(notes\\)",
        )


if __name__ == "__main__":
    unittest.main()
