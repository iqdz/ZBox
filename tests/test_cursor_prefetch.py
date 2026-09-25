"""
The sliding window of messages warmed around the message list's
cursor.

The folder-load sweep warms the first 25 unread-first and stops, so
in a 200-message folder every row past that was a cold read at the
moment it was selected. This follows the cursor instead, which is
where the next read actually comes from.

Ordering is the whole point of rows_around_selection: the caller
warms only as many as its budget allows, so the row directly below
the cursor must not lose its turn to one nine rows away.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from envelope_list_panel import EnvelopeListPanel

ROWS = [{"id": str(n)} for n in range(20)]


class _Panel:
    rows_around_selection = EnvelopeListPanel.rows_around_selection

    def __init__(self, rows, selected):
        self._row_envelopes = list(rows)
        self._selected = selected
        self.list_ctrl = self

    def GetFirstSelected(self):
        return self._selected


def _ids(envelopes):
    return [e["id"] for e in envelopes]


class RowsAroundSelection(unittest.TestCase):
    def test_nearest_rows_come_first(self):
        panel = _Panel(ROWS, selected=10)
        self.assertEqual(_ids(panel.rows_around_selection())[:4], ["11", "9", "12", "8"])

    def test_it_reaches_further_down_than_up(self):
        # People arrow down through a folder far more than up.
        panel = _Panel(ROWS, selected=10)
        ids = _ids(panel.rows_around_selection(before=3, after=9))
        self.assertEqual(sorted(int(i) for i in ids if int(i) > 10), list(range(11, 20)))
        self.assertEqual(sorted(int(i) for i in ids if int(i) < 10), [7, 8, 9])

    def test_the_top_of_the_list_does_not_wrap_or_go_negative(self):
        panel = _Panel(ROWS, selected=0)
        ids = _ids(panel.rows_around_selection())
        self.assertEqual(ids, [str(n) for n in range(1, 10)])

    def test_the_bottom_of_the_list_stops_cleanly(self):
        panel = _Panel(ROWS, selected=19)
        self.assertEqual(_ids(panel.rows_around_selection()), ["18", "17", "16"])

    def test_nothing_selected_yields_nothing(self):
        panel = _Panel(ROWS, selected=-1)
        self.assertEqual(panel.rows_around_selection(), [])

    def test_an_empty_list_yields_nothing(self):
        panel = _Panel([], selected=0)
        self.assertEqual(panel.rows_around_selection(), [])

    def test_the_selected_row_itself_is_never_included(self):
        # It is being fetched by the preview; warming it again would
        # spend part of the budget on a message already in flight.
        panel = _Panel(ROWS, selected=10)
        self.assertNotIn("10", _ids(panel.rows_around_selection()))


if __name__ == "__main__":
    unittest.main()
