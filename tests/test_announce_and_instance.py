"""
Session w's accessibility and one-instance work.

Three things, all about a keystroke telling you what it did:

1. Settings.announce_actions, the toggle behind Settings > Screen
   reader announcements control > Announce message actions. It has to
   survive a save and reload like every other setting, and default on
   for an existing settings.json that has never heard of it.

2. app/single_instance.py, the file a second launch leaves behind
   instead of starting a second ZBox. Consumed exactly once, so the
   window is raised per launch attempt rather than on every tick.

3. Nothing here touches wx: the frame's own use of these (the
   announce_action guard, _check_raise_request) is wired in
   main_frame.py and covered by the live checks.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import single_instance
from settings_manager import Settings


class AnnounceActionsSetting(unittest.TestCase):
    def test_on_by_default(self):
        self.assertTrue(Settings().announce_actions)

    def test_survives_a_round_trip(self):
        data = Settings(announce_actions=False).to_dict()
        self.assertFalse(Settings.from_dict(data).announce_actions)

    def test_an_older_settings_file_defaults_to_on(self):
        data = Settings().to_dict()
        del data["announce_actions"]
        self.assertTrue(Settings.from_dict(data).announce_actions)


class RaiseRequests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.config = self._dir.name
        self.addCleanup(self._dir.cleanup)

    def test_nothing_to_consume_when_nobody_asked(self):
        self.assertFalse(single_instance.consume_raise_request(self.config))

    def test_a_request_is_consumed_once(self):
        self.assertTrue(single_instance.request_raise(self.config))
        self.assertTrue(single_instance.consume_raise_request(self.config))
        self.assertFalse(single_instance.consume_raise_request(self.config))

    def test_the_file_is_gone_afterwards(self):
        single_instance.request_raise(self.config)
        single_instance.consume_raise_request(self.config)
        self.assertFalse(
            os.path.exists(
                os.path.join(self.config, single_instance.REQUEST_FILENAME)
            )
        )

    def test_a_missing_folder_is_created(self):
        nested = os.path.join(self.config, "config")
        self.assertTrue(single_instance.request_raise(nested))
        self.assertTrue(single_instance.consume_raise_request(nested))

    def test_two_launches_still_raise_once(self):
        single_instance.request_raise(self.config)
        single_instance.request_raise(self.config)
        self.assertTrue(single_instance.consume_raise_request(self.config))
        self.assertFalse(single_instance.consume_raise_request(self.config))


class WaitingForTheRunningCopy(unittest.TestCase):
    """Session al: the second launch stays alive until its request is
    taken, so the foreground right it was started with is still live
    when the running copy asks for it."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.config = self._dir.name
        self.addCleanup(self._dir.cleanup)

    def test_returns_true_at_once_when_nothing_is_waiting(self):
        self.assertTrue(single_instance.wait_until_consumed(self.config, timeout=0))

    def test_returns_true_once_the_request_is_taken(self):
        import threading

        single_instance.request_raise(self.config)
        timer = threading.Timer(
            0.1, single_instance.consume_raise_request, args=(self.config,)
        )
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertTrue(
            single_instance.wait_until_consumed(self.config, timeout=3.0, interval=0.01)
        )

    def test_gives_up_after_the_timeout(self):
        single_instance.request_raise(self.config)
        self.assertFalse(
            single_instance.wait_until_consumed(self.config, timeout=0.05, interval=0.01)
        )


class ForegroundCallsNeverRaise(unittest.TestCase):
    """Both run at moments where an exception would cost more than
    the window staying behind: one in a process about to exit, one
    inside a timer tick."""

    def test_grant_foreground_returns_a_bool(self):
        self.assertIn(single_instance.grant_foreground(), (True, False))

    def test_force_foreground_with_no_window_is_false(self):
        self.assertFalse(single_instance.force_foreground(0))
        self.assertFalse(single_instance.force_foreground(None))

    def test_force_foreground_with_a_bogus_handle_is_false(self):
        self.assertFalse(single_instance.force_foreground(0x7FFF0001))


if __name__ == "__main__":
    unittest.main()
