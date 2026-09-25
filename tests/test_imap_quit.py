"""
Quitting must not wait on a pooled IMAP connection that is busy, and
no new connection may start once quitting has begun.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import imap_body_fetch as ibf


class QuitTests(unittest.TestCase):
    def setUp(self):
        self.saved_pool = dict(ibf._POOL)
        self.saved_closing = ibf._CLOSING
        ibf._POOL.clear()
        ibf._CLOSING = False
        self.account = SimpleNamespace(account_id="quit_test", imap_host="imap.example.com")

    def tearDown(self):
        ibf._POOL.clear()
        ibf._POOL.update(self.saved_pool)
        ibf._CLOSING = self.saved_closing

    def test_close_all_skips_a_busy_connection(self):
        busy = ibf._pooled(self.account, SimpleNamespace())
        busy._lock.acquire()
        try:
            started = time.monotonic()
            closed = ibf.close_all()
            elapsed = time.monotonic() - started
        finally:
            busy._lock.release()
        self.assertEqual(closed, 0)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(ibf._POOL, {})

    def test_close_all_closes_an_idle_connection(self):
        ibf._pooled(self.account, SimpleNamespace())
        self.assertEqual(ibf.close_all(), 1)

    def test_no_connect_after_close_all(self):
        connection = ibf._PooledConnection(self.account, SimpleNamespace())
        ibf.close_all()
        with self.assertRaises(ibf.ImapBodyFetchUnavailable):
            connection._connect()


if __name__ == "__main__":
    unittest.main()
