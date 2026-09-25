"""
Delete / Shift+Delete from an OPEN message, in both body views --
requested after the user pointed out these already worked from the
message list (envelope_list_panel.py) but not from a message-body
tab. A bare-A Archive shortcut was tried here too but was REMOVED
(2026-09-05): the user's own live JAWS testing confirmed bare letter
keys are claimed by the screen reader's browse-mode quicknav before
any app-level mechanism -- accelerator table included -- ever sees
them, so there was no wiring fix available. Archive stays a message-
list-only shortcut.

First attempt bound Delete/Shift+Delete via EVT_KEY_DOWN on self.body
(the Text view's control), mirroring how the code already looked for
WXK_DELETE there. Live testing (real JAWS) showed that was wrong:
only Shift+Delete worked and plain Delete did nothing. Moved both to
the SAME wx.AcceleratorTable + EVT_MENU mechanism Escape and Ctrl+L/
Ctrl+R already use in _bind_close_accelerators, so they win the race
against RichEdit's native key handling, per the user's own diagnosis
("wire them same as escape"). Live testing after THAT change still
found plain Delete not working (and Shift+Delete regressed in Text
view specifically) -- cause unconfirmed -- so Ctrl+D and Ctrl+Delete
were added as a second route to the same handler
(_on_delete_shortcut), mirroring the same addition in the HTML view's
_HOTKEY_BRIDGE_JS. The handlers are plain EVT_MENU callbacks with no
key-code logic of their own -- wx's accelerator table is what decides
which physical keys reach them, which is platform machinery outside
what tests/wxstub can exercise, so these tests just confirm each
handler calls the right main_frame method with the right arguments.

The HTML/WebView path (_HOTKEY_BRIDGE_JS -> _dispatch_hotkey) is
unrelated machinery (wx accelerator tables never reach a WebView) and
is not changed by the accelerator-table fix above -- its tests are
unchanged below, minus the removed BARE_A case.

Tests call the panel methods as plain unbound functions against a
minimal stub "self" rather than constructing a real MessageViewPanel
(which embeds a WebView, notebook plumbing and several sibling panels
not worth wiring up just to reach a few key-handling methods) -- the
same technique test_account_gate.py and test_missing_folder_error.py
already use for exercising one method in isolation.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from message_view_panel import MessageViewPanel


class _FakeMainFrame:
    def __init__(self):
        self.settings_manager = mock.Mock()
        self.delete_calls = []
        self.print_preview_calls = 0

    def _run_delete(self, account, folder, envelope, permanent, after_success=None):
        self.delete_calls.append((account, folder, envelope, permanent, after_success))

    def _on_print_preview_message(self, event):
        self.print_preview_calls += 1


def _make_stub():
    stub = mock.Mock(spec=MessageViewPanel)
    stub.main_frame = _FakeMainFrame()
    stub.account = "acct"
    stub.folder = "INBOX"
    stub.envelope = {"id": "1"}
    stub.close_tab = "CLOSE_TAB_SENTINEL"
    return stub


class AcceleratorTableShortcuts(unittest.TestCase):
    """The Text-view accelerator-table handlers -- Delete and
    Shift+Delete, bound the same way as Escape/Ctrl+L/Ctrl+R (and, for
    Delete, also reachable via Ctrl+D/Ctrl+Delete -- see the
    accelerator table itself in _bind_close_accelerators; the table's
    routing of which physical key fires which handler is platform
    machinery these tests can't exercise, so this only confirms the
    handler's own behavior once fired)."""

    def test_delete_shortcut_moves_to_trash_and_closes_tab_after(self):
        stub = _make_stub()
        MessageViewPanel._on_delete_shortcut(stub, None)
        self.assertEqual(
            stub.main_frame.delete_calls,
            [("acct", "INBOX", {"id": "1"}, False, "CLOSE_TAB_SENTINEL")],
        )

    def test_delete_permanent_shortcut_is_permanent(self):
        stub = _make_stub()
        MessageViewPanel._on_delete_permanent_shortcut(stub, None)
        self.assertEqual(
            stub.main_frame.delete_calls,
            [("acct", "INBOX", {"id": "1"}, True, "CLOSE_TAB_SENTINEL")],
        )


class HtmlViewDispatchHotkey(unittest.TestCase):
    """MessageViewPanel._dispatch_hotkey -- the HTML/WebView path,
    fed by _HOTKEY_BRIDGE_JS's keydown listener. Ctrl+D reaches this
    the same way plain Delete does -- the JS bridge reports both as
    plain "DELETE" -- so there's nothing distinct to test there."""

    def test_delete_message_moves_to_trash(self):
        stub = _make_stub()
        MessageViewPanel._dispatch_hotkey(stub, "DELETE")
        self.assertEqual(
            stub.main_frame.delete_calls,
            [("acct", "INBOX", {"id": "1"}, False, "CLOSE_TAB_SENTINEL")],
        )

    def test_shift_delete_message_is_permanent(self):
        stub = _make_stub()
        MessageViewPanel._dispatch_hotkey(stub, "SHIFT_DELETE")
        self.assertEqual(stub.main_frame.delete_calls[0][3], True)

    def test_ctrl_shift_p_opens_print_preview(self):
        # Audit finding 51: Print Preview reachable from the HTML
        # view's JS hotkey bridge the same way Ctrl+P already is.
        # _dispatch_hotkey delegates to the panel's own
        # _on_print_preview_shortcut (same one level of indirection
        # CTRL_P's own dispatch uses for _on_print_shortcut), so on a
        # Mock(spec=...) stub that call itself is what's observable
        # here -- PrintPreviewShortcut below covers what that method
        # does once actually invoked.
        stub = _make_stub()
        MessageViewPanel._dispatch_hotkey(stub, "CTRL_SHIFT_P")
        stub._on_print_preview_shortcut.assert_called_once_with(None)


class PrintPreviewShortcut(unittest.TestCase):
    """The Text-view local accelerator for Ctrl+Shift+P (audit
    finding 51), bound the same way as the existing Ctrl+P entry --
    see _bind_close_accelerators."""

    def test_shortcut_delegates_to_main_frame(self):
        stub = _make_stub()
        MessageViewPanel._on_print_preview_shortcut(stub, None)
        self.assertEqual(stub.main_frame.print_preview_calls, 1)


class HotkeysLeaveTheWebViewEventFirst(unittest.TestCase):
    """A key from the page runs after the WebView's own event has
    returned: Shift+Delete's dialog and tab close inside a WebView2
    callback crashed the process (24 September 2026)."""

    def test_hotkey_is_not_run_inside_the_webview_event(self):
        import message_view_panel as mvp

        stub = _make_stub()
        queued = []
        with mock.patch.object(mvp.wx, "CallAfter", lambda func, *a: queued.append(func), create=True):
            MessageViewPanel._queue_hotkey(stub, "SHIFT_DELETE")
        stub._dispatch_hotkey.assert_not_called()
        self.assertEqual(len(queued), 1)
        queued[0]()
        stub._dispatch_hotkey.assert_called_once_with("SHIFT_DELETE")


if __name__ == "__main__":
    unittest.main()
