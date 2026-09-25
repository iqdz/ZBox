"""
Audit finding 20: the Keyboard Shortcuts text had drifted from the
app's actual bindings. This just asserts the entries the audit named
as missing are present, and a couple of stale/misleading ones are
gone or corrected -- not a full inventory of every shortcut, since
that's exactly the kind of thing that silently drifts again.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from shortcuts_dialog import SHORTCUTS_TEXT


class PreviouslyMissingShortcuts(unittest.TestCase):
    def test_named_missing_entries_are_all_present(self):
        # Audit finding 20's exact list of what the old MessageBox
        # string was missing.
        for expected in [
            "F5:",
            "Shift+F5:",
            "Ctrl+P:",
            "Escape:",
            "Ctrl+Shift+B:",
            "Ctrl+A:",
            "Ctrl+Shift+Q:",
        ]:
            self.assertIn(expected, SHORTCUTS_TEXT)

    def test_bare_s_and_a_are_documented_as_configurable(self):
        self.assertIn("S: Flag", SHORTCUTS_TEXT)
        self.assertIn("A: Archive", SHORTCUTS_TEXT)
        self.assertIn("Settings", SHORTCUTS_TEXT)

    def test_both_ctrl_s_meanings_are_distinguished(self):
        # Ctrl+S is Save Message As at the frame level (an open
        # message) and Save Draft inside compose -- two different
        # commands sharing a chord, both real, neither a duplicate.
        self.assertIn("Ctrl+S: Save Message As", SHORTCUTS_TEXT)
        self.assertIn("Ctrl+S: Save Draft", SHORTCUTS_TEXT)


class NewerBindingsAreDocumented(unittest.TestCase):
    """
    Audit finding 44 added Ctrl+Shift+T (Show Related Messages) and
    finding 46 added bare J/Shift+J (Mark as Junk/Not Junk) after
    PreviouslyMissingShortcuts above was written; this pins down that
    both actually made it into this file rather than drifting the
    way the original audit finding 20 complained about.
    """

    def test_show_related_messages_is_documented(self):
        self.assertIn("Ctrl+Shift+T: Show Related Messages", SHORTCUTS_TEXT)

    def test_bare_j_and_shift_j_are_documented_as_configurable(self):
        self.assertIn("J: Mark the selected message(s) as junk", SHORTCUTS_TEXT)
        self.assertIn("Shift+J: Mark the selected message(s) as not junk", SHORTCUTS_TEXT)
        # W (Watch Thread) and K (Ignore Thread) joined the same
        # configurable bare-key group after this test was written.
        self.assertIn("S, A, J, W and K", SHORTCUTS_TEXT)

    def test_undo_is_documented(self):
        # Audit finding 48 added Ctrl+Z (Undo) after the classes
        # above were written.
        self.assertIn("Ctrl+Z: Undo", SHORTCUTS_TEXT)

    def test_clear_offline_cache_key_is_documented(self):
        # The key lost its menu label when Clear Offline Message Cache
        # moved into Tools > Cache Clean Up Configuration, so this text
        # is now the only place a screen reader user can find it.
        self.assertIn("Ctrl+Shift+Delete: Clear Offline Message Cache", SHORTCUTS_TEXT)


if __name__ == "__main__":
    unittest.main()
