"""
Finding (c): a tree rebuild that cannot restore the previous
selection must not leave the tree with nothing selected.
AccountTreePanel wraps a real wx.TreeCtrl, so these tests skip
__init__ and hand the panel a small fake tree.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from account_tree_panel import AccountTreePanel


class _Item:
    def __init__(self, data=None, ok=True, parent=None):
        self.data = data
        self.ok = ok
        self.parent = parent

    def IsOk(self):
        return self.ok


_INVALID = _Item(ok=False)


class _FakeTree:
    def __init__(self):
        self.selection = _INVALID
        self.selected_log = []

    def GetSelection(self):
        return self.selection

    def GetItemData(self, item):
        return item.data

    def GetItemParent(self, item):
        return item.parent or _INVALID

    def SelectItem(self, item):
        self.selection = item
        self.selected_log.append(item)


def _panel(first_selectable):
    panel = AccountTreePanel.__new__(AccountTreePanel)
    panel.tree = _FakeTree()
    panel.first_selectable_item = first_selectable
    return panel


class EnsureSelection(unittest.TestCase):
    def test_selects_default_when_nothing_is_selected(self):
        inbox = _Item({"account_id": "acct_1", "folder": "INBOX"})
        panel = _panel(inbox)
        self.assertTrue(panel.ensure_selection())
        self.assertIs(panel.tree.selection, inbox)
        self.assertEqual(
            panel.selected_fetch_target(),
            {"account_id": "acct_1", "folder": "INBOX"},
        )

    def test_leaves_an_existing_selection_alone(self):
        inbox = _Item({"account_id": "acct_1", "folder": "INBOX"})
        sent = _Item({"account_id": "acct_1", "folder": "Sent"})
        panel = _panel(inbox)
        panel.tree.selection = sent
        self.assertFalse(panel.ensure_selection())
        self.assertIs(panel.tree.selection, sent)
        self.assertEqual(panel.tree.selected_log, [])

    def test_hidden_root_selection_counts_as_nothing(self):
        # With TR_HIDE_ROOT the selection can be the data-less root.
        inbox = _Item({"account_id": "acct_1", "folder": "INBOX"})
        panel = _panel(inbox)
        panel.tree.selection = _Item(data=None)
        self.assertTrue(panel.ensure_selection())
        self.assertIs(panel.tree.selection, inbox)

    def test_no_default_available_is_a_no_op(self):
        panel = _panel(None)
        self.assertFalse(panel.ensure_selection())
        self.assertEqual(panel.tree.selected_log, [])


if __name__ == "__main__":
    unittest.main()
