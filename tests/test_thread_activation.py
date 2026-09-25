"""
What Enter/Space/double-click does on a collapsed thread row.

It used to open every message in the thread as its own tab. With the
default message view set to HTML that is one WebView2 (an Edge
instance) per message, created back to back, each loading
asynchronously and each taking focus when it finished. A screen
reader read the first message and then went silent -- reported as the
app jamming on a 13th-gen CPU with 64 GB of RAM, which is the tell
that it was never a throughput problem.

It now opens the newest UNREAD message of that thread, as one
message in one tab, with the whole thread carried along so
Ctrl+Shift+Page Up/Down flip through the rest without ever creating
a second message view. That is only safe because flipping replaces
the tab instead of adding one.

An intermediate version simply expanded the thread, matching
Thunderbird. That was safe but left the user to find what was
actually new by hand, which is not what they asked for. Expanding is
still what Right arrow does, and is still the fallback when the
thread's members are not known.

Reading a whole thread moved to Message > Open Message in
Conversation (Ctrl+Shift+O) -- the same name and key as
Thunderbird's own built-in command, and the same idea: ONE tab. This
file covers the list side of it, selected_thread_envelopes, which
decides WHICH messages that tab gets. conversation_panel.py is the
tab itself.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from envelope_list_panel import EnvelopeListPanel, _RowThreadInfo


class _Panel:
    """The slice of EnvelopeListPanel these methods touch."""

    _on_item_activated = EnvelopeListPanel._on_item_activated
    selected_thread_envelopes = EnvelopeListPanel.selected_thread_envelopes

    def __init__(self, rows, infos, selected):
        self._row_envelopes = rows
        self._row_thread_info = infos
        self._expanded_thread_keys = set()
        self._thread_members_by_key = {}
        self._selected = selected
        self.expanded = []
        self.activated = []
        self._activation_handler = self.activated.append
        self.list_ctrl = self

    # -- stand-ins ----------------------------------------------------
    def GetFirstSelected(self):
        return self._selected

    def _selected_row_and_info(self):
        if self._selected == -1 or self._selected >= len(self._row_thread_info):
            return None, None
        return self._selected, self._row_thread_info[self._selected]

    def selected_envelope(self):
        if self._selected == -1 or self._selected >= len(self._row_envelopes):
            return None
        return self._row_envelopes[self._selected]

    def _expand_thread(self, thread_key):
        self.expanded.append(thread_key)


COLLAPSED = _RowThreadInfo(
    thread_key="t1", is_root=True, is_collapsed=True, depth=0, message_count=12,
)


def _expanded(depth):
    return _RowThreadInfo(
        thread_key="t1", is_root=(depth == 0), is_collapsed=False,
        depth=depth, message_count=3,
    )


READ = [{"iana": "seen", "raw": "\\Seen"}]


def _msg(id_, date, unread):
    return {"id": id_, "date": date, "flags": [] if unread else READ}


class CollapsedThreadActivation(unittest.TestCase):
    def test_enter_opens_the_newest_unread_and_not_a_tab_per_message(self):
        panel = _Panel([{"id": "1"}], [COLLAPSED], selected=0)
        panel._thread_members_by_key = {"t1": [
            _msg("1", "2026-09-01T10:00:00+00:00", unread=False),
            _msg("2", "2026-09-03T10:00:00+00:00", unread=True),
            _msg("3", "2026-09-02T10:00:00+00:00", unread=True),
        ]}
        panel._on_item_activated(None)
        self.assertEqual([e["id"] for e in panel.activated], ["2"])
        self.assertEqual(panel.expanded, [])

    def test_recency_is_by_date_not_by_position_in_the_reply_tree(self):
        # all_envelopes() walks the reply tree depth-first, so the
        # last entry is the deepest reply, not the newest message.
        panel = _Panel([{"id": "1"}], [COLLAPSED], selected=0)
        panel._thread_members_by_key = {"t1": [
            _msg("root", "2026-09-01T10:00:00+00:00", unread=True),
            _msg("late-reply-to-root", "2026-09-09T10:00:00+00:00", unread=True),
            _msg("deep", "2026-09-04T10:00:00+00:00", unread=True),
        ]}
        panel._on_item_activated(None)
        self.assertEqual([e["id"] for e in panel.activated], ["late-reply-to-root"])

    def test_an_all_read_thread_opens_its_newest_message(self):
        panel = _Panel([{"id": "1"}], [COLLAPSED], selected=0)
        panel._thread_members_by_key = {"t1": [
            _msg("1", "2026-09-01T10:00:00+00:00", unread=False),
            _msg("2", "2026-09-05T10:00:00+00:00", unread=False),
        ]}
        panel._on_item_activated(None)
        self.assertEqual([e["id"] for e in panel.activated], ["2"])

    def test_it_falls_back_to_expanding_when_the_members_are_unknown(self):
        panel = _Panel([{"id": "1"}], [COLLAPSED], selected=0)
        panel._thread_members_by_key = {}
        panel._on_item_activated(None)
        self.assertEqual(panel.expanded, ["t1"])
        self.assertEqual(panel.activated, [])

    def test_an_ordinary_message_still_opens(self):
        panel = _Panel([{"id": "9"}], [None], selected=0)
        panel._on_item_activated(None)
        self.assertEqual(panel.activated, [{"id": "9"}])
        self.assertEqual(panel.expanded, [])

    def test_a_row_inside_an_expanded_thread_opens_that_one_message(self):
        # The gesture the user confirmed working: Right arrow to
        # expand, then Enter on one reply.
        rows = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        infos = [_expanded(0), _expanded(1), _expanded(1)]
        panel = _Panel(rows, infos, selected=2)
        panel._on_item_activated(None)
        self.assertEqual(panel.activated, [{"id": "3"}])
        self.assertEqual(panel.expanded, [])


class SelectedThreadEnvelopes(unittest.TestCase):
    """What Open Message in Conversation gets handed."""

    def test_a_collapsed_row_yields_the_whole_thread(self):
        panel = _Panel([{"id": "1"}], [COLLAPSED], selected=0)
        panel._thread_members_by_key = {
            "t1": [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        }
        self.assertEqual(
            panel.selected_thread_envelopes(),
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        )

    def test_an_expanded_row_is_recovered_from_the_visible_rows(self):
        # _thread_members_by_key is only populated for COLLAPSED
        # threads and is rebuilt on every render, so an expanded
        # thread has to come from the rows on screen -- and must not
        # pick up the unrelated row sitting next to them.
        rows = [{"id": "1"}, {"id": "2"}, {"id": "3"}, {"id": "other"}]
        infos = [_expanded(0), _expanded(1), _expanded(1), None]
        panel = _Panel(rows, infos, selected=1)
        panel._thread_members_by_key = {}
        self.assertEqual(
            panel.selected_thread_envelopes(),
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        )

    def test_a_single_message_row_yields_just_that_message(self):
        panel = _Panel([{"id": "9"}], [None], selected=0)
        self.assertEqual(panel.selected_thread_envelopes(), [{"id": "9"}])

    def test_nothing_selected_yields_nothing(self):
        panel = _Panel([], [], selected=-1)
        self.assertEqual(panel.selected_thread_envelopes(), [])


if __name__ == "__main__":
    unittest.main()
