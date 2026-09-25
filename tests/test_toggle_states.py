"""
Toggle controls report their real role and checked state to screen
readers, whatever the theme draws. See accessible.install_toggle_states.

Run in a subprocess against the real wxPython: other tests put the
tests\\wxstub stand-in in place of wx for the whole run, and this check
needs real controls. Skipped when wxPython is not installed.
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
    import accessible

    failures = []

    def check(label, condition):
        if not condition:
            failures.append(label)

    app = wx.App(False)
    accessible.install_toggle_states()
    frame = wx.Frame(None)

    def state(control):
        status, flags = control._zbox_toggle_state.GetState(0)
        check("status ok", status == wx.ACC_OK)
        return flags

    box = wx.CheckBox(frame, label="Minimize to tray")
    check("check role", box._zbox_toggle_state.GetRole(0) == (wx.ACC_OK, wx.ROLE_SYSTEM_CHECKBUTTON))
    check("unchecked", not state(box) & accessible._STATE_CHECKED)
    box.SetValue(True)
    check("checked", state(box) & accessible._STATE_CHECKED)
    box.Disable()
    check("unavailable", state(box) & accessible._STATE_UNAVAILABLE)

    mixed = wx.CheckBox(frame, label="Some", style=wx.CHK_3STATE)
    mixed.Set3StateValue(wx.CHK_UNDETERMINED)
    check("mixed", state(mixed) & accessible._STATE_MIXED)
    check("mixed not checked", not state(mixed) & accessible._STATE_CHECKED)

    radio = wx.RadioButton(frame, label="Dark", style=wx.RB_GROUP)
    wx.RadioButton(frame, label="Light")
    check("radio role", radio._zbox_toggle_state.GetRole(0) == (wx.ACC_OK, wx.ROLE_SYSTEM_RADIOBUTTON))
    radio.SetValue(True)
    check("radio checked", state(radio) & accessible._STATE_CHECKED)

    toggle = wx.ToggleButton(frame, label="Bold")
    toggle.SetValue(True)
    check("toggle pressed", state(toggle) & accessible._STATE_PRESSED)

    frame.Destroy()
    if failures:
        print("FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("OK")
""")

DARK_SCRIPT = textwrap.dedent("""
    import ctypes, sys
    try:
        import wx
    except ImportError:
        print("NO_WX")
        sys.exit(0)
    import accessible

    BM_GETCHECK = 0x00F0
    user32 = ctypes.WinDLL("user32")
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

    app = wx.App(False)
    app.MSWEnableDarkMode(getattr(wx.App, "DarkMode_Always", 1))
    accessible.install_toggle_states()
    frame = wx.Frame(None)
    failures = []

    def windows_type(control):
        return user32.GetWindowLongW(ctypes.c_void_p(control.GetHandle()), -16) & 0xF

    def windows_checked(control):
        return user32.SendMessageW(control.GetHandle(), BM_GETCHECK, 0, 0)

    box = wx.CheckBox(frame, label="Minimize to tray")
    if windows_type(box) == 0xB:
        failures.append("check box still owner-drawn")
    box.SetValue(True)
    if windows_checked(box) != 1 or not box.GetValue():
        failures.append("check box state not reported by Windows")
    box.SetValue(False)
    if windows_checked(box) != 0 or box.GetValue():
        failures.append("check box clear not reported by Windows")

    radio = wx.RadioButton(frame, label="Dark", style=wx.RB_GROUP)
    other = wx.RadioButton(frame, label="Light")
    if windows_type(radio) == 0xB:
        failures.append("radio still owner-drawn")
    other.SetValue(True)
    if windows_checked(other) != 1 or windows_checked(radio) != 0:
        failures.append("radio state not reported by Windows")

    frame.Destroy()
    if failures:
        print("FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("OK")
""")


class ToggleStateTests(unittest.TestCase):
    def test_dark_mode_toggles_are_real_windows_controls(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = APP
        result = subprocess.run(
            [sys.executable, "-c", DARK_SCRIPT],
            capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
        )
        if "NO_WX" in result.stdout:
            self.skipTest("wxPython is not installed")
        self.assertEqual(
            result.returncode, 0,
            "dark mode toggle check failed:\n%s%s" % (result.stdout, result.stderr),
        )

    def test_toggle_controls_report_role_and_state(self):
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
            "toggle state check failed:\n%s%s" % (result.stdout, result.stderr),
        )


if __name__ == "__main__":
    unittest.main()
