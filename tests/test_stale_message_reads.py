"""
Stale-result guards on the message-read paths.

The bug these cover, from a real session's debug log: a single
'message read' took anywhere from 3.2s to 22.1s, and
_on_envelope_selected fires one per arrow-key press with no way to
tell a result apart from a result the user has already navigated past.
Every one of them called reader_panel.set_message, so the reader pane
spent minutes repainting itself with messages the user had left --
in whatever order the reads happened to finish. To a screen-reader
user that is the message list announcing the wrong message, which is
what "navigation glitches" actually meant.

A cold row is also DEBOUNCED now: EVT_LIST_ITEM_SELECTED fires once
per row, so holding Down through thirty rows queued thirty live reads
for messages the user had already passed. Nothing is fetched until
the cursor sits still. Cached rows wait too now: nothing at all runs
on an arrow press, so arrowing through a folder never competes with
reading and converting a message.

These call the real methods against a hand-built stand-in for the
frame, so the guard logic is tested, not a copy of it. wx.CallLater
is replaced with a recorder, so a test fires the debounce itself and
can also assert that it did NOT fire.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client
import main_frame


class _Account:
    account_id = "acct1"


class _ReaderPanel:
    def __init__(self):
        self.shown = []

    def set_message(self, message):
        self.shown.append(message)

    def show_body_text(self, text):
        # The cached path hands over text already extracted on the
        # local lane; setUp keeps the body as-is so a test can name it.
        self.shown.append(text)

    def show_loading(self):
        pass

    def show_error(self, text):
        self.shown.append(("error", text))


class _EnvelopePanel:
    """Only what the cursor prefetch asks for."""

    def __init__(self, around=()):
        self.around = list(around)

    def rows_around_selection(self, before=3, after=9):
        return list(self.around)


class _MailPanel:
    def __init__(self):
        self.reader_panel = _ReaderPanel()
        self.envelope_panel = _EnvelopePanel()


class _Contacts:
    def add_from_envelope_from(self, value):
        pass


class _Worker:
    """Records submitted jobs instead of running them, so a test can
    deliver their results in whatever order it likes -- which is the
    whole point here."""

    def __init__(self):
        self.jobs = []

    def submit(self, work_fn, on_success, on_error=None):
        self.jobs.append((work_fn, on_success, on_error))


class _CallLater:
    """Stands in for wx.CallLater: records the callback instead of
    scheduling it, so a test controls when (and whether) it runs."""

    pending = []

    def __init__(self, _millis, callback):
        self.callback = callback
        self.stopped = False
        _CallLater.pending.append(self)

    def Stop(self):
        self.stopped = True

    @classmethod
    def fire_all(cls):
        for timer in list(cls.pending):
            if not timer.stopped:
                timer.callback()
        cls.pending = []


class _RunNow:
    """The local lane, run on the spot: the cached read is quick and
    its result is what these tests are about, not its scheduling."""

    def submit(self, work_fn, on_success, on_error=None):
        try:
            result = work_fn()
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(exc)
            return
        on_success(result)


class _Lanes:
    """account_lanes.AccountLanes, routed to one recording worker,
    with the local lane running at once."""

    def __init__(self, worker):
        self.worker = worker
        self.local = _RunNow()

    def for_read(self, account_id, backend):
        return self.worker

    def for_account(self, account_id):
        return self.worker


class _Frame:
    """The slice of ZBoxMainFrame these methods actually touch."""

    _on_envelope_selected = main_frame.ZBoxMainFrame._on_envelope_selected
    _cancel_pending_preview = main_frame.ZBoxMainFrame._cancel_pending_preview
    _prefetch_around_cursor = main_frame.ZBoxMainFrame._prefetch_around_cursor

    def __init__(self):
        self._preview_timer = None
        self.sync_worker = _Worker()
        self.paths = None
        self.mail_panel = _MailPanel()
        self.contacts = _Contacts()
        self.worker = _Worker()
        self.lanes = _Lanes(self.worker)
        self._body_request_id = 0
        self._list_last_interaction = 0

    def _background_allowed(self, account):
        return True

    def _resolve_envelope_context(self, envelope):
        return (_Account(), "INBOX", envelope["id"])


class ReaderPreviewStaleGuard(unittest.TestCase):
    def setUp(self):
        self.frame = _Frame()
        self._real_cached_only = himalaya_client.read_message_cached_only
        self._real_call_later = main_frame.wx.CallLater
        # Force the slow path: a cold cache is precisely when this
        # matters, and a warm one never queues a job at all.
        himalaya_client.read_message_cached_only = lambda *a, **k: None
        # The cached path converts the message to text itself; the
        # stand-in keeps the body as-is so a test can name it.
        import message_body
        self._message_body = message_body
        self._real_extract = message_body.extract_message_body
        message_body.extract_message_body = lambda message: message["body"]
        _CallLater.pending = []
        main_frame.wx.CallLater = _CallLater

    def tearDown(self):
        himalaya_client.read_message_cached_only = self._real_cached_only
        self._message_body.extract_message_body = self._real_extract
        main_frame.wx.CallLater = self._real_call_later

    def _select(self, message_id):
        self.frame._on_envelope_selected({"id": message_id, "from": []})

    def test_a_held_arrow_key_costs_one_fetch_not_one_per_row(self):
        # The whole point: thirty rows, one read.
        for row in range(30):
            self._select(str(row))
        _CallLater.fire_all()
        self.assertEqual(len(self.frame.worker.jobs), 1)

    def test_nothing_is_fetched_until_the_cursor_settles(self):
        self._select("1")
        self.assertEqual(self.frame.worker.jobs, [])
        self.assertEqual(self.frame.mail_panel.reader_panel.shown, [])

    def test_a_settled_row_is_fetched_and_painted(self):
        self._select("1")
        _CallLater.fire_all()
        self.assertEqual(len(self.frame.worker.jobs), 1)
        self.frame.worker.jobs[0][1]({"body": "message one"})
        self.assertEqual(
            self.frame.mail_panel.reader_panel.shown, [{"body": "message one"}]
        )

    def test_a_superseded_read_never_reaches_the_reader(self):
        # Two rows that both got as far as fetching (the cursor
        # settled on each), finishing out of order.
        self._select("1")
        _CallLater.fire_all()
        self._select("2")
        _CallLater.fire_all()

        first_success = self.frame.worker.jobs[0][1]
        second_success = self.frame.worker.jobs[1][1]

        # The slow first read finishes last, which is the ordering
        # that produced the bug.
        second_success({"body": "message two"})
        first_success({"body": "message one"})

        self.assertEqual(
            self.frame.mail_panel.reader_panel.shown, [{"body": "message two"}]
        )

    def test_a_superseded_error_never_reaches_the_reader(self):
        self._select("1")
        _CallLater.fire_all()
        self._select("2")
        _CallLater.fire_all()

        first_error = self.frame.worker.jobs[0][2]
        second_success = self.frame.worker.jobs[1][1]

        second_success({"body": "message two"})
        first_error(RuntimeError("timed out"))

        # An error dialog for a message the user already left is
        # exactly as wrong as a body for one.
        self.assertEqual(
            self.frame.mail_panel.reader_panel.shown, [{"body": "message two"}]
        )

    def test_a_cache_hit_paints_once_the_cursor_settles(self):
        himalaya_client.read_message_cached_only = lambda *a, **k: {"body": "warm"}
        self._select("2")
        # Nothing runs on the arrow press itself, cached row or not.
        self.assertEqual(self.frame.mail_panel.reader_panel.shown, [])
        _CallLater.fire_all()
        self.assertEqual(self.frame.mail_panel.reader_panel.shown, ["warm"])
        self.assertEqual(self.frame.worker.jobs, [])

    def test_a_cache_hit_cancels_a_pending_cold_fetch(self):
        self._select("1")
        himalaya_client.read_message_cached_only = lambda *a, **k: {"body": "warm"}
        self._select("2")
        _CallLater.fire_all()
        # Row 1's fetch was dropped before it ever started.
        self.assertEqual(self.frame.worker.jobs, [])
        self.assertEqual(self.frame.mail_panel.reader_panel.shown, ["warm"])

    def test_a_cache_hit_also_invalidates_an_in_flight_read(self):
        self._select("1")
        _CallLater.fire_all()
        first_success = self.frame.worker.jobs[0][1]

        himalaya_client.read_message_cached_only = lambda *a, **k: {"body": "warm"}
        self._select("2")
        _CallLater.fire_all()

        first_success({"body": "message one"})

        self.assertEqual(self.frame.mail_panel.reader_panel.shown, ["warm"])


if __name__ == "__main__":
    unittest.main()
