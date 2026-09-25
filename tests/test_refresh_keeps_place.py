"""
A silent auto-refresh must not move the user.

populate_if_changed re-applies the selection by (account, id) rather
than row index, which is right -- but it re-applies it against
whatever rows the NEXT render produces, and an expanded thread
renders through a different branch than a collapsed one. If a
refresh quietly collapsed a thread the user had open, their selected
reply would no longer be a row at all and the restore would find
nothing to match: focus lands somewhere else, silently, mid-read.
That is the "list is jumpy" complaint in its worst form.

These cover the render side of that: what rows exist after a
repopulate, and whether the expanded set survives it.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from envelope_list_panel import EnvelopeListPanel
import thread_grouping


def _msg(id_, message_id, in_reply_to=None, subject="Quarterly numbers"):
    return {
        "id": id_,
        "message-id": message_id,
        "in-reply-to": [in_reply_to] if in_reply_to else [],
        "subject": subject,
        "date": "2026-09-0%s T10:00:00+00:00".replace(" ", "") % id_,
        "flags": [],
    }


THREAD = [
    _msg("1", "root@example.com"),
    _msg("2", "reply@example.com", "root@example.com"),
    _msg("3", "reply2@example.com", "root@example.com"),
]


class _Panel:
    """Only the row-building half of EnvelopeListPanel: no list_ctrl,
    so this exercises the real _build_threaded_rows rather than a
    reimplementation of it."""

    _build_threaded_rows = EnvelopeListPanel._build_threaded_rows

    def __init__(self):
        self._expanded_thread_keys = set()
        self._thread_members_by_key = {}
        self._thread_filter = "all"
        self._enable_threading = True

    def rows(self, envelopes):
        built = self._build_threaded_rows(envelopes)
        return [envelope["id"] for envelope, _info in built]


class ExpandedThreadSurvivesARefresh(unittest.TestCase):
    def setUp(self):
        self.panel = _Panel()
        self.key = thread_grouping.thread_key(THREAD[0])

    def test_a_collapsed_thread_shows_one_row(self):
        self.assertEqual(self.panel.rows(THREAD), ["1"])

    def test_an_expanded_thread_shows_every_member(self):
        self.panel._expanded_thread_keys.add(self.key)
        self.assertEqual(self.panel.rows(THREAD), ["1", "2", "3"])

    def test_the_expansion_survives_a_repopulate(self):
        # populate() does not clear _expanded_thread_keys, so a
        # 20-second poll cannot silently collapse what the user
        # opened -- which would leave their selected reply with no
        # row to be restored onto.
        self.panel._expanded_thread_keys.add(self.key)
        self.panel.rows(THREAD)
        self.assertEqual(self.panel.rows(THREAD), ["1", "2", "3"])

    def test_a_new_reply_arriving_keeps_the_thread_expanded(self):
        self.panel._expanded_thread_keys.add(self.key)
        self.panel.rows(THREAD)
        grown = THREAD + [_msg("4", "reply3@example.com", "root@example.com")]
        self.assertEqual(self.panel.rows(grown), ["1", "2", "3", "4"])

    def test_every_expanded_row_carries_an_id_the_restore_can_match(self):
        # populate_if_changed matches on (account, id); a row without
        # one can never be restored onto.
        self.panel._expanded_thread_keys.add(self.key)
        built = self.panel._build_threaded_rows(THREAD)
        self.assertTrue(all(envelope.get("id") for envelope, _info in built))

    def test_collapsed_members_are_recorded_for_the_open_gesture(self):
        # Enter on a collapsed row picks the newest unread out of
        # this map; an empty map falls back to merely expanding.
        self.panel.rows(THREAD)
        self.assertEqual(
            [e["id"] for e in self.panel._thread_members_by_key[self.key]],
            ["1", "2", "3"],
        )

    def test_the_members_map_is_rebuilt_not_accumulated(self):
        # It is keyed by thread_key, which never expires on its own,
        # so a stale entry from a previous folder would otherwise sit
        # there forever.
        self.panel.rows(THREAD)
        self.panel._expanded_thread_keys.add(self.key)
        self.panel.rows(THREAD)
        self.assertEqual(self.panel._thread_members_by_key, {})


class _FakeListCtrl:
    """Enough of wx.ListCtrl for populate/_render to run for real.
    DeleteAllItems drops the selection AND the focused item, which is
    what the real control does and the whole point of these tests."""

    def __init__(self):
        self.items = []
        self.selected = set()
        self.focused = None
        self.ensured_visible = None

    def DeleteAllItems(self):
        self.items = []
        self.selected = set()
        self.focused = None

    def InsertItem(self, row, text):
        self.items.insert(row, [text, "", "", ""])
        return row

    def SetItem(self, row, column, text):
        self.items[row][column] = text

    def GetItemCount(self):
        return len(self.items)

    def GetSelectedItemCount(self):
        return len(self.selected)

    def GetFirstSelected(self):
        return min(self.selected) if self.selected else -1

    def GetNextSelected(self, row):
        later = [r for r in sorted(self.selected) if r > row]
        return later[0] if later else -1

    def Select(self, row, on=1):
        if on:
            self.selected.add(row)
        else:
            self.selected.discard(row)

    def Focus(self, row):
        self.focused = row

    def EnsureVisible(self, row):
        self.ensured_visible = row


class _ListPanel:
    """The real populate/_render against a fake control, so this
    exercises the shipped methods rather than a description of them."""

    populate = EnvelopeListPanel.populate
    _render = EnvelopeListPanel._render
    _apply_sort = EnvelopeListPanel._apply_sort
    _without_hidden = EnvelopeListPanel._without_hidden
    drop_hidden_rows = EnvelopeListPanel.drop_hidden_rows
    show_placeholder = EnvelopeListPanel.show_placeholder
    selected_envelope = EnvelopeListPanel.selected_envelope
    has_content = EnvelopeListPanel.has_content
    has_listing = EnvelopeListPanel.has_listing

    def __init__(self):
        self.list_ctrl = _FakeListCtrl()
        self._row_envelopes = []
        self._row_thread_info = []
        self._thread_members_by_key = {}
        self._all_envelopes = []
        self._expanded_thread_keys = set()
        self._signature = ()
        self._sort_key = "date"
        self._sort_ascending = False
        self._enable_threading = False
        self._thread_filter = "all"

    def row_ids(self):
        return [envelope["id"] for envelope in self._row_envelopes]

    def select_id(self, wanted):
        row = self.row_ids().index(wanted)
        self.list_ctrl.Select(row)
        self.list_ctrl.Focus(row)
        return row


def _env(id_, day, account="acct1"):
    return {
        "id": id_,
        "_zbox_account_id": account,
        "message-id": "%s@example.com" % id_,
        "in-reply-to": [],
        "subject": "Message %s" % id_,
        "from": [{"name": "Sender", "email": "sender@example.com"}],
        "date": "2026-09-%02dT10:00:00+00:00" % day,
        "flags": [],
    }


FOLDER = [_env("1", 1), _env("2", 2), _env("3", 3)]


class AnEmptyFolderIsStillPolled(unittest.TestCase):
    """An empty folder shows a placeholder row, so has_content is
    False. The silent poll checks has_listing instead, or mail arriving
    in an empty folder never appears until it is reselected."""

    def test_an_empty_listing_counts(self):
        panel = _ListPanel()
        panel.populate([])
        self.assertFalse(panel.has_content())
        self.assertTrue(panel.has_listing())

    def test_a_loading_placeholder_does_not(self):
        panel = _ListPanel()
        panel.populate([])
        panel.show_placeholder("Loading...")
        self.assertFalse(panel.has_listing())

    def test_a_folder_emptied_by_a_delete_counts(self):
        hidden = set()
        panel = _ListPanel()
        panel._hidden_keys_provider = lambda: hidden
        panel.populate(FOLDER)
        hidden.update(("acct1", envelope["id"]) for envelope in FOLDER)
        panel.drop_hidden_rows()
        self.assertFalse(panel.has_content())
        self.assertTrue(panel.has_listing())


class HiddenRowsStayOffTheList(unittest.TestCase):
    """A batched Delete takes rows off the list before the server move
    lands. A silent refresh that races the move must not put them
    back, and the reader must land on the row that moved up."""

    def setUp(self):
        self.hidden = set()
        self.panel = _ListPanel()
        self.panel._hidden_keys_provider = lambda: self.hidden
        self.panel.populate(FOLDER)

    def test_no_provider_hides_nothing(self):
        panel = _ListPanel()
        panel.populate(FOLDER)
        self.assertEqual(sorted(panel.row_ids()), ["1", "2", "3"])

    def test_populate_leaves_out_a_hidden_unified_row(self):
        self.hidden = {("acct1", "2")}
        self.panel.populate(FOLDER)
        self.assertEqual(sorted(self.panel.row_ids()), ["1", "3"])

    def test_a_single_folder_row_is_matched_with_account_none(self):
        rows = [dict(envelope, _zbox_account_id=None) for envelope in FOLDER]
        self.hidden = {(None, "2")}
        self.panel.populate(rows)
        self.assertEqual(sorted(self.panel.row_ids()), ["1", "3"])

    def test_the_same_id_on_another_account_is_not_hidden(self):
        self.hidden = {("acct2", "2")}
        self.panel.populate(FOLDER)
        self.assertEqual(sorted(self.panel.row_ids()), ["1", "2", "3"])

    def test_drop_focuses_the_row_that_moved_up(self):
        self.panel.select_id("2")
        index = self.panel.list_ctrl.GetFirstSelected()
        self.hidden = {("acct1", "2")}
        self.panel.drop_hidden_rows()
        self.assertNotIn("2", self.panel.row_ids())
        self.assertEqual(self.panel.list_ctrl.focused, index)
        self.assertEqual(self.panel.list_ctrl.selected, {index})

    def test_drop_of_the_last_row_clamps_to_the_new_end(self):
        last = self.panel.row_ids()[-1]
        self.panel.select_id(last)
        self.hidden = {("acct1", last)}
        self.panel.drop_hidden_rows()
        self.assertEqual(self.panel.list_ctrl.focused, len(self.panel.row_ids()) - 1)

    def test_drop_with_nothing_hidden_leaves_the_selection_alone(self):
        row = self.panel.select_id("2")
        self.panel.drop_hidden_rows()
        self.assertEqual(self.panel.list_ctrl.selected, {row})


class PopulateKeepsThePlace(unittest.TestCase):
    """populate() is the path a first fetch and a folder open take,
    and it was the one repaint path with no selection restore --
    populate_if_changed, append and _mark_ids_and_refresh all had
    one. _render opens with DeleteAllItems, which destroys the
    focused item, and wx raises EVT_LIST_ITEM_ACTIVATED only when
    there is one. An Enter landing in that window raised nothing at
    all: no activation, no error, no log line. Seen live once, when
    a Unified Inbox fetch finished 151ms before the keypress."""

    def setUp(self):
        self.panel = _ListPanel()
        self.panel.populate(FOLDER)

    def test_the_selected_message_survives_a_repopulate(self):
        self.panel.select_id("2")
        self.panel.populate(FOLDER)
        self.assertEqual(self.panel.selected_envelope()["id"], "2")

    def test_the_focused_item_survives_a_repopulate(self):
        # The selection alone is not enough. wx needs a FOCUSED item
        # to raise an activation against, and that is what the lost
        # Enter was missing.
        row = self.panel.select_id("2")
        self.panel.populate(FOLDER)
        self.assertIsNotNone(self.panel.list_ctrl.focused)
        self.assertEqual(self.panel.row_ids()[self.panel.list_ctrl.focused], "2")
        del row

    def test_it_follows_the_message_not_the_row_number(self):
        # New mail sorting in above the selection shifts every row
        # index; the restore matches on (account, id) so it lands on
        # the same message rather than the same position.
        self.panel.select_id("1")
        before = self.panel.row_ids().index("1")
        self.panel.populate(FOLDER + [_env("4", 4)])
        after = self.panel.row_ids().index("1")
        self.assertNotEqual(before, after)
        self.assertEqual(self.panel.selected_envelope()["id"], "1")

    def test_a_multi_selection_is_restored_whole(self):
        self.panel.select_id("1")
        self.panel.select_id("3")
        self.panel.populate(FOLDER)
        restored = {self.panel.row_ids()[row] for row in self.panel.list_ctrl.selected}
        self.assertEqual(restored, {"1", "3"})

    def test_a_message_that_vanished_leaves_nothing_selected(self):
        # Deleted elsewhere, or moved: it has no row to be restored
        # onto, and inventing one would land the reader somewhere
        # they never asked to be.
        self.panel.select_id("2")
        self.panel.populate([_env("1", 1), _env("3", 3)])
        self.assertEqual(self.panel.list_ctrl.selected, set())
        self.assertIsNone(self.panel.selected_envelope())

    def test_one_survivor_of_a_multi_selection_still_takes_focus(self):
        self.panel.select_id("1")
        self.panel.select_id("2")
        self.panel.populate([_env("1", 1), _env("3", 3)])
        self.assertEqual(self.panel.selected_envelope()["id"], "1")
        self.assertEqual(self.panel.row_ids()[self.panel.list_ctrl.focused], "1")

    def test_a_first_load_selects_nothing_on_its_own(self):
        # Nothing was selected, so there is nothing to restore, and
        # _maybe_apply_initial_focus stays the thing that decides
        # where a fresh list starts.
        fresh = _ListPanel()
        fresh.populate(FOLDER)
        self.assertEqual(fresh.list_ctrl.selected, set())
        self.assertIsNone(fresh.list_ctrl.focused)

    def test_the_same_account_id_is_required_to_match(self):
        # Unified folders mix accounts, and two accounts can hand out
        # the same envelope id for different messages.
        self.panel.select_id("2")
        self.panel.populate([_env("2", 2, account="acct2")])
        self.assertEqual(self.panel.list_ctrl.selected, set())


if __name__ == "__main__":
    unittest.main()
