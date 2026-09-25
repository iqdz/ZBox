"""
Tools > Cache Clean Up Configuration.

Covers the clean up by message date across both local caches, the
once-a-day idle trigger, the two settings it adds, the dialog's pure
label helpers, and the two background paths that must stop refilling
what the clean up removes. Real temp directories; nothing here needs
wxPython or the Himalaya CLI.
"""

import datetime
import email.utils
import json
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
from cache_cleanup_dialog import (
    CLEANUP_IDLE_SECONDS,
    CLEANUP_INTERVAL_SECONDS,
    age_button_label,
    age_choice_label,
    cleanup_due,
)
from paths import Paths
from settings_manager import CACHE_AGE_CHOICES, Settings

DAY = 24 * 60 * 60


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


def _rfc_date(timestamp):
    return email.utils.formatdate(timestamp)


def _iso_date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()


def _write_body(paths, account, message_id, date_header):
    path = hc._message_cache_path(paths, account, "INBOX", message_id, "imap")
    headers = []
    if date_header is not None:
        headers.append({"name": "date", "value": {"Text": date_header}})
    message = {
        "parts": [{"headers": headers, "body": {"Text": "hello"}}],
        "text_body": [0], "html_body": [], "attachments": [],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(message, handle)
    return path


def _write_offline(paths, account, name, date_header, folder="INBOX"):
    directory = os.path.join(
        paths.cache_dir(account.account_id), "offline_mail", folder, "cur"
    )
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    lines = ["From: sender@example.com"]
    if date_header is not None:
        lines.append("Date: " + date_header)
    lines += ["Subject: test", "", "body"]
    with open(path, "wb") as handle:
        handle.write("\r\n".join(lines).encode("utf-8"))
    return path


def _age_file(path, seconds, now):
    stamp = now - seconds
    os.utime(path, (stamp, stamp))


class _TempCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = _make_paths(self._tmp.name)
        self.account = _FakeAccount()
        self.now = time.time()
        self.addCleanup(hc._read_cache.clear)
        self.addCleanup(hc._prefetch_failures.clear)


class PruneByMessageDate(_TempCache):
    def test_no_cache_root_is_a_no_op(self):
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 30, now=self.now), (0, 0))

    def test_body_older_than_the_age_is_removed_and_newer_kept(self):
        old = _write_body(self.paths, self.account, "1", _rfc_date(self.now - 40 * DAY))
        new = _write_body(self.paths, self.account, "2", _rfc_date(self.now - 1 * DAY))
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 30, now=self.now), (1, 0))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_goes_by_message_date_not_file_age(self):
        # Opened today, so the file is brand new -- but the message is
        # a year old. That is exactly the leftover this removes.
        path = _write_body(self.paths, self.account, "1", _rfc_date(self.now - 365 * DAY))
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 30, now=self.now), (1, 0))
        self.assertFalse(os.path.exists(path))

    def test_undated_body_falls_back_to_file_age(self):
        stale = _write_body(self.paths, self.account, "1", None)
        fresh = _write_body(self.paths, self.account, "2", None)
        _age_file(stale, 40 * DAY, self.now)
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 30, now=self.now), (1, 0))
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    def test_offline_copies_are_cleaned_by_their_date_header(self):
        old = _write_offline(self.paths, self.account, "old", _rfc_date(self.now - 20 * DAY))
        new = _write_offline(self.paths, self.account, "new", _rfc_date(self.now - 2 * DAY))
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 15, now=self.now), (0, 1))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_each_age_choice_draws_its_own_line(self):
        path = _write_body(self.paths, self.account, "1", _rfc_date(self.now - 10 * DAY))
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 15, now=self.now), (0, 0))
        self.assertTrue(os.path.exists(path))
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 7, now=self.now), (1, 0))
        self.assertFalse(os.path.exists(path))

    def test_a_removed_accounts_leftover_directory_is_covered(self):
        gone = _FakeAccount("acct_removed")
        path = _write_offline(self.paths, gone, "old", _rfc_date(self.now - 40 * DAY))
        self.assertEqual(hc.prune_caches_by_message_date(self.paths, 30, now=self.now), (0, 1))
        self.assertFalse(os.path.exists(path))

    def test_expired_prefetch_cooldowns_are_dropped(self):
        expired = ("acct1", "INBOX", "1", "imap")
        fresh = ("acct1", "INBOX", "2", "imap")
        hc._prefetch_failures[expired] = (
            time.monotonic() - hc._PREFETCH_FAILURE_COOLDOWN_SECONDS - 1
        )
        hc._prefetch_failures[fresh] = time.monotonic()
        hc.prune_caches_by_message_date(self.paths, 30, now=self.now)
        self.assertNotIn(expired, hc._prefetch_failures)
        self.assertIn(fresh, hc._prefetch_failures)


class CleanupDue(unittest.TestCase):
    NOW = 1_900_000_000.0

    def test_never_run_and_idle_is_due(self):
        self.assertTrue(cleanup_due(self.NOW, 0.0, CLEANUP_IDLE_SECONDS, False))

    def test_less_than_a_day_since_the_last_run_is_not_due(self):
        last = self.NOW - CLEANUP_INTERVAL_SECONDS + 60
        self.assertFalse(cleanup_due(self.NOW, last, CLEANUP_IDLE_SECONDS, False))

    def test_a_full_day_since_the_last_run_is_due(self):
        last = self.NOW - CLEANUP_INTERVAL_SECONDS
        self.assertTrue(cleanup_due(self.NOW, last, CLEANUP_IDLE_SECONDS, False))

    def test_recent_keyboard_activity_is_not_idle(self):
        self.assertFalse(cleanup_due(self.NOW, 0.0, CLEANUP_IDLE_SECONDS - 1, False))

    def test_an_open_message_is_never_idle(self):
        self.assertFalse(cleanup_due(self.NOW, 0.0, 10 * CLEANUP_IDLE_SECONDS, True))

    def test_a_clock_set_backwards_does_not_block_it(self):
        self.assertTrue(cleanup_due(self.NOW, self.NOW + 5 * DAY, CLEANUP_IDLE_SECONDS, False))

    def test_an_unreadable_last_run_counts_as_never(self):
        self.assertTrue(cleanup_due(self.NOW, "garbage", CLEANUP_IDLE_SECONDS, False))


class CacheCleanupSettings(unittest.TestCase):
    def test_the_four_offered_ages(self):
        self.assertEqual(CACHE_AGE_CHOICES, (1, 7, 15, 30))

    def test_defaults_match_the_old_prune(self):
        settings = Settings()
        self.assertEqual(settings.cache_max_message_age_days, 30)
        self.assertEqual(settings.cache_cleanup_last_run, 0.0)

    def test_an_age_the_dialog_never_offers_falls_back_to_30(self):
        self.assertEqual(Settings(cache_max_message_age_days=12).cache_max_message_age_days, 30)
        self.assertEqual(Settings(cache_max_message_age_days="x").cache_max_message_age_days, 30)

    def test_round_trip(self):
        restored = Settings.from_dict(
            Settings(cache_max_message_age_days=7, cache_cleanup_last_run=123.5).to_dict()
        )
        self.assertEqual(restored.cache_max_message_age_days, 7)
        self.assertEqual(restored.cache_cleanup_last_run, 123.5)

    def test_a_settings_file_from_before_this_uses_defaults(self):
        restored = Settings.from_dict({})
        self.assertEqual(restored.cache_max_message_age_days, 30)
        self.assertEqual(restored.cache_cleanup_last_run, 0.0)


class DialogLabels(unittest.TestCase):
    def test_singular_and_plural(self):
        self.assertEqual(age_choice_label(1), "1 day")
        self.assertEqual(age_choice_label(15), "15 days")

    def test_button_label_says_the_current_age(self):
        self.assertIn("older than 7 days", age_button_label(7))


class RefillIsSkipped(_TempCache):
    def test_envelope_age_check(self):
        old = {"date": _iso_date(self.now - 40 * DAY)}
        new = {"date": _iso_date(self.now - 1 * DAY)}
        self.assertTrue(hc._envelope_older_than(old, 30, now=self.now))
        self.assertFalse(hc._envelope_older_than(new, 30, now=self.now))
        # Unknown age is never skipped, and no limit means no skipping.
        self.assertFalse(hc._envelope_older_than({}, 30, now=self.now))
        self.assertFalse(hc._envelope_older_than(old, None, now=self.now))

    def test_prefetch_skips_messages_older_than_the_age(self):
        envelopes = [
            {"id": "1", "date": _iso_date(self.now - 1 * DAY)},
            {"id": "2", "date": _iso_date(self.now - 40 * DAY)},
            {"id": "3"},
        ]
        with patch.object(hc, "read_message") as mock_read:
            hc.prefetch_messages(
                self.paths, self.account, envelopes, "INBOX",
                backend="imap", max_age_days=30,
            )
        read_ids = [call.args[2] for call in mock_read.call_args_list]
        self.assertEqual(read_ids, ["1", "3"])

    def test_offline_top_up_skips_messages_older_than_the_age(self):
        maildir = os.path.join(self._tmp.name, "maildir")
        os.makedirs(os.path.join(maildir, "cur"))
        envelopes = [
            {"id": "1", "date": _iso_date(self.now - 1 * DAY), "message-id": "<new@example.com>"},
            {"id": "2", "date": _iso_date(self.now - 40 * DAY), "message-id": "<old@example.com>"},
        ]
        raw = "Message-ID: <new@example.com>\r\n\r\nbody"
        with patch.object(hc, "ensure_maildir_folder"), \
                patch.object(hc, "_maildir_folder_dir", return_value=maildir), \
                patch.object(hc, "list_envelopes", return_value=envelopes), \
                patch.object(hc, "read_message_raw", return_value=raw) as mock_raw, \
                patch.object(hc, "_run"):
            hc.sync_folder_offline(
                self.paths, self.account, "INBOX", limit=10, max_age_days=30,
            )
        read_ids = [call.args[2] for call in mock_raw.call_args_list]
        self.assertEqual(read_ids, ["1"])


if __name__ == "__main__":
    unittest.main()
