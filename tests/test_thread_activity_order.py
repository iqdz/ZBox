"""
Sorted by date, a thread sits where its newest message would.

A thread that gets a reply must move to the top of a descending date
list (and to the bottom of an ascending one), not stay where its
first message was. Subject and From sorts keep the root's position.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from envelope_list_panel import EnvelopeListPanel


def _msg(id_, message_id, day, in_reply_to=None, subject="Topic"):
    return {
        "id": id_,
        "message-id": message_id,
        "in-reply-to": [in_reply_to] if in_reply_to else [],
        "subject": subject,
        "date": "2026-09-%02dT10:00:00+00:00" % day,
        "flags": [],
    }


class _Panel:
    _build_threaded_rows = EnvelopeListPanel._build_threaded_rows

    def __init__(self, key, ascending):
        self._sort_key = key
        self._sort_ascending = ascending
        self._expanded_thread_keys = set()
        self._thread_members_by_key = {}
        self._thread_filter = "all"
        self._enable_threading = True

    def rows(self, envelopes):
        return [e["id"] for e, _info in self._build_threaded_rows(envelopes)]


# Already in descending date order by each message's own date, as
# _apply_sort hands them over: the single message (day 5) comes
# before the thread's root (day 1), but the thread's reply is day 9.
DESCENDING = [
    _msg("reply", "reply@example.com", 9, "root@example.com"),
    _msg("single", "single@example.com", 5),
    _msg("root", "root@example.com", 1),
]


class ThreadMovesWithItsNewestMessage(unittest.TestCase):
    def test_a_replied_thread_is_first_when_descending(self):
        panel = _Panel("date", False)
        self.assertEqual(panel.rows(DESCENDING), ["root", "single"])

    def test_a_replied_thread_is_last_when_ascending(self):
        panel = _Panel("date", True)
        ascending = list(reversed(DESCENDING))
        self.assertEqual(panel.rows(ascending), ["single", "root"])

    def test_a_thread_without_new_replies_keeps_its_place(self):
        panel = _Panel("date", False)
        envelopes = [
            _msg("single", "single@example.com", 5),
            _msg("reply", "reply@example.com", 3, "root@example.com"),
            _msg("root", "root@example.com", 1),
        ]
        self.assertEqual(panel.rows(envelopes), ["single", "root"])

    def test_subject_sort_keeps_the_root_position(self):
        panel = _Panel("subject", False)
        self.assertEqual(panel.rows(DESCENDING), ["single", "root"])


if __name__ == "__main__":
    unittest.main()
