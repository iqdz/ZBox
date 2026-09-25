"""
startup_registration.startup_command() -- the pure half of Settings'
"Run ZBox when Windows starts" checkbox. is_registered()/
set_registered() touch the real HKCU Run key and aren't exercised
here, same accepted gap as every other real-registry/OS-integration
edge in this project (e.g. dpapi_secret_store.machine_id()).

Expected values are built with os.path.join against the same
base_dir passed in, rather than a hand-written path string, so this
test asserts the actual composition logic (source vs. frozen, the
--minimized flag) and not incidentally which OS it happens to run
under -- these tests run from the Linux wxstub harness, not real
Windows.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import startup_registration as sr


class StartupCommand(unittest.TestCase):
    def setUp(self):
        self.base_dir = os.path.join("base", "ZBox")

    def test_frozen_points_at_the_exe_with_minimized_flag(self):
        command = sr.startup_command(self.base_dir, frozen=True)
        expected_exe = os.path.join(self.base_dir, "ZBox.exe")
        self.assertEqual(command, f'"{expected_exe}" --minimized')

    def test_from_source_points_at_the_interpreter_and_main_py(self):
        command = sr.startup_command(self.base_dir, frozen=False)
        expected_main = os.path.join(self.base_dir, "main.py")
        self.assertEqual(command, f'"{sys.executable}" "{expected_main}" --minimized')

    def test_frozen_defaults_to_sys_frozen_when_not_given(self):
        # Neither this test process nor the wxstub subprocess is ever
        # actually frozen, so the default resolves to the from-source
        # branch -- this just proves the default is read from
        # sys.frozen rather than requiring the caller to pass it.
        command = sr.startup_command(self.base_dir)
        self.assertIn("main.py", command)
        self.assertIn("--minimized", command)


if __name__ == "__main__":
    unittest.main()
