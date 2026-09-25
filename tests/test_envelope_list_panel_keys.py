"""
Audit finding 19: bare S and A in the message list claim the
keypress ahead of wx.ListCtrl's own type-ahead, and that has to stay
configurable. Covers EnvelopeListPanel._on_key_down's decision logic
directly against the wx stand-in -- not a real ListCtrl, but the
same branch a real one would hit.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import wx
from envelope_list_panel import EnvelopeListPanel


class _FakeKeyEvent:
    def __init__(self, key_code, ctrl=False, alt=False):
        self._key_code = key_code
        self._ctrl = ctrl
        self._alt = alt
        self.skipped = False

    def GetKeyCode(self):
        return self._key_code

    def ControlDown(self):
        return self._ctrl

    def AltDown(self):
        return self._alt

    def ShiftDown(self):
        return False

    def Skip(self):
        self.skipped = True


def _make_panel(bare_key_shortcuts_enabled=lambda: True):
    calls = []
    panel = EnvelopeListPanel(
        None,
        context_menu_handler=lambda *a: None,
        selection_handler=lambda *a: None,
        activation_handler=lambda *a: None,
        key_handler=lambda action, shift: calls.append((action, shift)),
        bare_key_shortcuts_enabled=bare_key_shortcuts_enabled,
    )
    return panel, calls


class BareKeyShortcutsEnabled(unittest.TestCase):
    def test_bare_s_flags_when_enabled(self):
        panel, calls = _make_panel(lambda: True)
        panel._on_key_down(_FakeKeyEvent(ord("S")))
        self.assertEqual(calls, [("flag", False)])

    def test_bare_a_archives_when_enabled(self):
        panel, calls = _make_panel(lambda: True)
        panel._on_key_down(_FakeKeyEvent(ord("A")))
        self.assertEqual(calls, [("archive", False)])

    def test_ctrl_s_is_never_claimed_as_bare_flag(self):
        # Ctrl+S is Save Message As elsewhere; this handler must not
        # also fire the flag toggle for it.
        panel, calls = _make_panel(lambda: True)
        event = _FakeKeyEvent(ord("S"), ctrl=True)
        panel._on_key_down(event)
        self.assertEqual(calls, [])
        self.assertTrue(event.skipped)


class BareKeyShortcutsDisabled(unittest.TestCase):
    def test_bare_s_falls_through_to_type_ahead_when_disabled(self):
        panel, calls = _make_panel(lambda: False)
        event = _FakeKeyEvent(ord("S"))
        panel._on_key_down(event)
        self.assertEqual(calls, [])
        self.assertTrue(event.skipped)

    def test_bare_a_falls_through_to_type_ahead_when_disabled(self):
        panel, calls = _make_panel(lambda: False)
        event = _FakeKeyEvent(ord("A"))
        panel._on_key_down(event)
        self.assertEqual(calls, [])
        self.assertTrue(event.skipped)

    def test_delete_key_still_works_when_bare_shortcuts_disabled(self):
        # Delete is a separate, unconditional branch -- disabling the
        # bare S/A shortcuts must not touch it.
        panel, calls = _make_panel(lambda: False)
        event = _FakeKeyEvent(wx.WXK_DELETE)
        panel._on_key_down(event)
        self.assertEqual(calls, [("delete", False)])

    def test_default_is_enabled_when_no_callable_given(self):
        # EnvelopeListPanel's default parameter (lambda: True) --
        # matches bare_key_shortcuts_enabled's True default in
        # Settings, so behavior is unchanged for anyone who hasn't
        # touched the new setting.
        calls = []
        panel = EnvelopeListPanel(
            None,
            context_menu_handler=lambda *a: None,
            selection_handler=lambda *a: None,
            activation_handler=lambda *a: None,
            key_handler=lambda action, shift: calls.append((action, shift)),
        )
        panel._on_key_down(_FakeKeyEvent(ord("A")))
        self.assertEqual(calls, [("archive", False)])


if __name__ == "__main__":
    unittest.main()
