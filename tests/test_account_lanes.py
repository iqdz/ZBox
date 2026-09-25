"""
account_lanes: each account's foreground work on its own thread,
local reads on theirs, and the backoff that stops an unreachable
server from stalling automatic work. Live, 24 September 2026, one
Disroot connect held every account's Enter hostage.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import account_lanes
from account_manager import Account


class _Worker:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class Lanes(unittest.TestCase):
    def setUp(self):
        self.lanes = account_lanes.AccountLanes(factory=_Worker)

    def test_each_account_has_its_own_lane(self):
        self.assertIsNot(self.lanes.for_account("a"), self.lanes.for_account("b"))

    def test_an_account_keeps_the_same_lane(self):
        self.assertIs(self.lanes.for_account("a"), self.lanes.for_account("a"))

    def test_local_reads_never_share_a_network_lane(self):
        self.assertIs(self.lanes.for_read("a", "maildir"), self.lanes.local)
        self.assertIs(self.lanes.for_read("a", "imap"), self.lanes.for_account("a"))
        self.assertIsNot(self.lanes.local, self.lanes.for_account("a"))

    def test_stop_reaches_every_lane(self):
        a = self.lanes.for_account("a")
        self.lanes.stop()
        self.assertTrue(a.stopped)
        self.assertTrue(self.lanes.local.stopped)


class Backoff(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.reach = account_lanes.Reachability(clock=lambda: self.now[0])

    def test_a_fresh_account_is_allowed(self):
        self.assertTrue(self.reach.allowed("a"))

    def test_a_failure_pauses_for_a_minute_then_doubles_to_the_cap(self):
        delays = [self.reach.failed("a") for _ in range(6)]
        self.assertEqual(delays, [60, 120, 240, 480, 900, 900])

    def test_paused_until_the_delay_passes(self):
        self.reach.failed("a")
        self.assertFalse(self.reach.allowed("a"))
        self.now[0] += 60
        self.assertTrue(self.reach.allowed("a"))

    def test_one_account_never_pauses_another(self):
        self.reach.failed("a")
        self.assertTrue(self.reach.allowed("b"))

    def test_a_success_resumes_at_once(self):
        self.reach.failed("a")
        self.assertTrue(self.reach.succeeded("a"))
        self.assertTrue(self.reach.allowed("a"))
        self.assertFalse(self.reach.succeeded("a"))


class NetworkErrors(unittest.TestCase):
    def test_the_live_disroot_failure_counts(self):
        self.assertTrue(account_lanes.is_network_error(
            'Himalaya exited with code 1: {"error":"connect disroot.org:993",'
            '"sources":["A connection attempt failed because the connected party '
            'did not properly respond (os error 10060)"]}'
        ))
        self.assertTrue(account_lanes.is_network_error(TimeoutError("[WinError 10060]")))
        self.assertTrue(account_lanes.is_network_error("No such host is known"))

    def test_a_server_refusal_does_not(self):
        self.assertFalse(account_lanes.is_network_error("NO [CANNOT] move refused"))
        self.assertFalse(account_lanes.is_network_error("Mailbox doesn't exist: Archive"))


class AutoCheckSetting(unittest.TestCase):
    def test_on_by_default_and_kept_across_save(self):
        self.assertTrue(Account().auto_check)
        self.assertTrue(Account.from_dict({}).auto_check)
        paused = Account(auto_check=False)
        self.assertFalse(Account.from_dict(paused.to_dict()).auto_check)


if __name__ == "__main__":
    unittest.main()
