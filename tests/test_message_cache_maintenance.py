"""
Audit finding 37: the on-disk message cache (userdata/<id>/
message_cache) never expired and was never invalidated when a
message moved or was deleted -- confirmed on a real profile at
130+ MB for one account alone. Covers prune_message_cache's two
caps and _invalidate_cached_message's drop-one-entry behavior
directly against a real temp directory (Paths.profile_dir just
makes real folders; nothing here needs the Himalaya CLI).

Also covers the follow-up hardening from a real debug log: a
background prefetch tried (twice) to read a message id about a
second after the interactive path had just permanently deleted it,
each attempt logged as an ERROR. _invalidate_cached_message now also
seeds prefetch_messages' own failure cooldown for that id, so a
prefetch round already under way skips it outright instead of
issuing a live read that can only fail.
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc
from paths import Paths


class _FakeAccount:
    def __init__(self, account_id="acct1"):
        self.account_id = account_id


def _make_paths(root):
    return Paths(
        base=root, app=root, apps_files=root, himalaya=root, data=root,
        userdata=os.path.join(root, "userdata"),
        cache=os.path.join(root, "cache"),
        config=os.path.join(root, "config"),
        logs=os.path.join(root, "logs"),
    )


def _write_cache_file(paths, account, folder, message_id, backend, size, age_seconds=0):
    path = hc._message_cache_path(paths, account, folder, message_id, backend)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x" * size)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


class PruneMessageCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = _make_paths(self._tmp.name)
        self.account = _FakeAccount()

    def test_no_cache_directory_is_a_no_op(self):
        self.assertEqual(hc.prune_message_cache(self.paths, self.account), 0)

    def test_files_within_both_caps_are_left_alone(self):
        _write_cache_file(self.paths, self.account, "INBOX", "1", "imap", 100)
        removed = hc.prune_message_cache(
            self.paths, self.account, max_bytes=10_000, max_age_seconds=999_999
        )
        self.assertEqual(removed, 0)

    def test_files_older_than_max_age_are_removed_regardless_of_size(self):
        old_path = _write_cache_file(
            self.paths, self.account, "INBOX", "1", "imap", 10,
            age_seconds=40 * 24 * 60 * 60,  # 40 days
        )
        new_path = _write_cache_file(
            self.paths, self.account, "INBOX", "2", "imap", 10, age_seconds=0
        )
        removed = hc.prune_message_cache(
            self.paths, self.account, max_bytes=10_000_000,
            max_age_seconds=30 * 24 * 60 * 60,
        )
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))

    def test_over_size_cap_removes_oldest_first_until_under_it(self):
        # Three 100-byte files, cap at 250 bytes -- the single oldest
        # must go, the two newer ones must survive.
        oldest = _write_cache_file(
            self.paths, self.account, "INBOX", "1", "imap", 100, age_seconds=300
        )
        middle = _write_cache_file(
            self.paths, self.account, "INBOX", "2", "imap", 100, age_seconds=200
        )
        newest = _write_cache_file(
            self.paths, self.account, "INBOX", "3", "imap", 100, age_seconds=100
        )
        removed = hc.prune_message_cache(
            self.paths, self.account, max_bytes=250, max_age_seconds=None
        )
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(oldest))
        self.assertTrue(os.path.exists(middle))
        self.assertTrue(os.path.exists(newest))

    def test_age_and_size_caps_both_apply_in_one_pass(self):
        stale = _write_cache_file(
            self.paths, self.account, "INBOX", "1", "imap", 50,
            age_seconds=60 * 24 * 60 * 60,
        )
        oldest_survivor = _write_cache_file(
            self.paths, self.account, "INBOX", "2", "imap", 100, age_seconds=200
        )
        newest_survivor = _write_cache_file(
            self.paths, self.account, "INBOX", "3", "imap", 100, age_seconds=100
        )
        removed = hc.prune_message_cache(
            self.paths, self.account, max_bytes=150,
            max_age_seconds=30 * 24 * 60 * 60,
        )
        # The stale file goes on age alone; that alone brings the
        # survivors' total (200) over the 150-byte cap, so the
        # older of the two remaining files goes as well.
        self.assertEqual(removed, 2)
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(oldest_survivor))
        self.assertTrue(os.path.exists(newest_survivor))


class InvalidateCachedMessage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = _make_paths(self._tmp.name)
        self.account = _FakeAccount()
        self.addCleanup(hc._read_cache.clear)
        # _invalidate_cached_message also seeds _prefetch_failures
        # (the follow-up hardening below) -- clear it after every
        # test in this class, not just the ones that assert on it
        # directly, so leftover state never leaks into a later test.
        self.addCleanup(hc._prefetch_failures.clear)

    def test_drops_both_backends_by_default(self):
        imap_path = _write_cache_file(self.paths, self.account, "INBOX", "1", "imap", 5)
        maildir_path = _write_cache_file(self.paths, self.account, "INBOX", "1", "maildir", 5)
        hc._read_cache[(self.account.account_id, "INBOX", "1", "imap")] = {"a": 1}
        hc._read_cache[(self.account.account_id, "INBOX", "1", "maildir")] = {"a": 1}

        hc._invalidate_cached_message(self.paths, self.account, "INBOX", "1")

        self.assertFalse(os.path.exists(imap_path))
        self.assertFalse(os.path.exists(maildir_path))
        self.assertNotIn((self.account.account_id, "INBOX", "1", "imap"), hc._read_cache)
        self.assertNotIn((self.account.account_id, "INBOX", "1", "maildir"), hc._read_cache)

    def test_drops_only_the_named_backend_when_given(self):
        imap_path = _write_cache_file(self.paths, self.account, "INBOX", "1", "imap", 5)
        maildir_path = _write_cache_file(self.paths, self.account, "INBOX", "1", "maildir", 5)

        hc._invalidate_cached_message(self.paths, self.account, "INBOX", "1", backend="imap")

        self.assertFalse(os.path.exists(imap_path))
        self.assertTrue(os.path.exists(maildir_path))

    def test_missing_entry_is_silently_fine(self):
        # No file, no in-memory key -- must not raise.
        hc._invalidate_cached_message(self.paths, self.account, "INBOX", "does-not-exist")

    def test_seeds_the_prefetch_failure_cooldown_for_both_backends(self):
        before = time.monotonic()

        hc._invalidate_cached_message(self.paths, self.account, "INBOX", "1")

        imap_key = (self.account.account_id, "INBOX", "1", "imap")
        maildir_key = (self.account.account_id, "INBOX", "1", "maildir")
        self.assertIn(imap_key, hc._prefetch_failures)
        self.assertIn(maildir_key, hc._prefetch_failures)
        self.assertGreaterEqual(hc._prefetch_failures[imap_key], before)

    def test_named_backend_seeds_only_that_backend(self):
        hc._invalidate_cached_message(self.paths, self.account, "INBOX", "1", backend="imap")

        self.assertIn((self.account.account_id, "INBOX", "1", "imap"), hc._prefetch_failures)
        self.assertNotIn((self.account.account_id, "INBOX", "1", "maildir"), hc._prefetch_failures)


class PrefetchSkipsAJustDeletedMessage(unittest.TestCase):
    """
    The end-to-end case this hardening exists for: a message gets
    deleted (or moved), and a prefetch round whose worklist still
    contains that id must not attempt a live read for it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = _make_paths(self._tmp.name)
        self.account = _FakeAccount()
        self.addCleanup(hc._read_cache.clear)
        self.addCleanup(hc._prefetch_failures.clear)

    def test_a_message_invalidated_after_being_queued_is_not_read(self):
        envelopes = [{"id": "1"}, {"id": "2"}, {"id": "3"}]

        # Message 2 is deleted (or moved) after prefetch's worklist
        # was captured but before this round runs -- exactly the
        # real-log race: main_frame builds `envelopes` from one poll,
        # a delete happens, then the queued prefetch round starts.
        hc._invalidate_cached_message(self.paths, self.account, "INBOX", "2")

        with patch.object(hc, "read_message") as mock_read:
            hc.prefetch_messages(self.paths, self.account, envelopes, "INBOX", backend="imap")

        read_ids = [call.args[2] for call in mock_read.call_args_list]
        self.assertNotIn("2", read_ids)
        self.assertIn("1", read_ids)
        self.assertIn("3", read_ids)

    def test_without_invalidation_the_message_would_have_been_read(self):
        # Control case, so the test above is trusted to mean what it
        # says: with no invalidation, prefetch does try id "2".
        envelopes = [{"id": "1"}, {"id": "2"}, {"id": "3"}]

        with patch.object(hc, "read_message") as mock_read:
            hc.prefetch_messages(self.paths, self.account, envelopes, "INBOX", backend="imap")

        read_ids = [call.args[2] for call in mock_read.call_args_list]
        self.assertIn("2", read_ids)


if __name__ == "__main__":
    unittest.main()
