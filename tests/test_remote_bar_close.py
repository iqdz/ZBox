"""
Escape, Ctrl+W and Ctrl+F4 close the message from the remote content
bar and its buttons. Run in a subprocess against the real wxPython,
like test_toggle_states; skipped when wxPython is not installed.
"""

import os
import subprocess
import sys
import textwrap
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
APP = os.path.join(ROOT, "app")

SCRIPT = textwrap.dedent("""
    import sys
    try:
        import wx
    except ImportError:
        print("NO_WX")
        sys.exit(0)
    import message_view_panel as mvp

    failures = []
    is_close = mvp.RemoteContentBar.is_close_key
    for key, ctrl, expected in (
        (wx.WXK_ESCAPE, False, True),
        (ord("W"), True, True),
        (wx.WXK_F4, True, True),
        (ord("W"), False, False),
        (wx.WXK_F4, False, False),
        (ord("A"), True, False),
    ):
        if bool(is_close(key, ctrl)) != expected:
            failures.append("is_close_key(%r, %r)" % (key, ctrl))

    app = wx.App(False)
    frame = wx.Frame(None)
    closed = []
    bar = mvp.RemoteContentBar(
        frame, "2 remote images blocked.", "sender@example.com",
        lambda: None, lambda: None, on_close=lambda: closed.append(1),
    )

    def press(key, ctrl):
        event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
        event.SetKeyCode(key)
        event.SetControlDown(ctrl)
        event.SetEventObject(bar)
        bar.GetEventHandler().ProcessEvent(event)

    press(wx.WXK_ESCAPE, False)
    press(ord("W"), True)
    press(wx.WXK_F4, True)
    press(ord("A"), False)
    if len(closed) != 3:
        failures.append("bar closed %d time(s) for 3 close keys" % len(closed))

    frame.Destroy()
    if failures:
        print("FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("OK")
""")


class RemoteBarCloseKeys(unittest.TestCase):
    def test_close_keys_on_the_remote_content_bar(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = APP
        result = subprocess.run(
            [sys.executable, "-c", SCRIPT],
            capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
        )
        if "NO_WX" in result.stdout:
            self.skipTest("wxPython is not installed")
        self.assertEqual(
            result.returncode, 0,
            "remote bar close keys failed:\n%s%s" % (result.stdout, result.stderr),
        )


if __name__ == "__main__":
    unittest.main()
