"""
himalaya_client._AccountGate serializes Himalaya subprocess calls per
account and lets background work (prefetch, IDLE checks, quiet
offline top-ups) stand down whenever interactive (user-triggered)
work is active or waiting for the same account, rather than queueing
behind it.

Real bug this catches: leave() used to reset only `_holder`, never
decrementing `_interactive_active` back down after
enter_interactive() incremented it. Confirmed live via a
zbox_debug.log spanning 10+ minutes after a single interactive
search completed -- background Himalaya calls for both accounts kept
being stood down every ~20s poll with no let-up, because
try_enter_background()'s `_interactive_active == 0` check could never
pass again for the rest of that run. These tests exercise the gate
directly (no subprocess, no wx) rather than through _run, since the
gate's own state machine is the thing that was wrong.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc


class AccountGateInteractiveThenBackground(unittest.TestCase):
    def test_background_allowed_again_after_interactive_leaves(self):
        gate = hc._AccountGate()

        gate.enter_interactive()
        gate.leave()

        # This is the exact bug: before the fix, _interactive_active
        # stayed at 1 forever after the single interactive call above,
        # so background work could never re-enter for the rest of the
        # process's life.
        self.assertTrue(gate.try_enter_background())

    def test_background_still_stood_down_while_interactive_holds(self):
        gate = hc._AccountGate()

        gate.enter_interactive()
        try:
            self.assertFalse(gate.try_enter_background())
        finally:
            gate.leave()

    def test_repeated_interactive_cycles_dont_accumulate(self):
        gate = hc._AccountGate()

        for _ in range(5):
            gate.enter_interactive()
            gate.leave()

        self.assertEqual(gate._interactive_active, 0)
        self.assertTrue(gate.try_enter_background())

    def test_background_hold_releases_cleanly_too(self):
        # A background holder's own leave() must not touch
        # _interactive_active at all -- only "interactive" holders
        # ever incremented it.
        gate = hc._AccountGate()

        self.assertTrue(gate.try_enter_background())
        gate.leave()

        self.assertEqual(gate._interactive_active, 0)
        self.assertTrue(gate.try_enter_background())


if __name__ == "__main__":
    unittest.main()
