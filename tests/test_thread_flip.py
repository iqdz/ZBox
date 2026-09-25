"""
Ctrl+Shift+Page Down / Page Up: move through a thread by REPLACING
the open message tab instead of adding one.

The behaviour this replaced opened every message in a thread as its
own tab. With the HTML view as the default that is one WebView2 --
an Edge instance -- per message, each loading asynchronously and each
taking focus when it landed; a screen reader read the first message
and went silent. Flipping keeps exactly one message view alive at any
thread length, so the ordering these tests pin down is the whole
safety property: open the new tab, then close the old one, and never
have two live.

Ctrl+Page Up/Down are NOT used: those are wx.Notebook's own tab
switching on Windows, announced as such by the screen reader, so
taking them would break the way back to the Mail tab.

Running off either end of the thread ends the reading cycle -- but on
the SECOND press, not the first. The same key that has been moving
through the thread would otherwise close the message the instant it
ran out of thread, and running off the end is exactly when someone
who cannot see the list is least sure where they are.
"""

import os
import sys
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client
import main_frame


THREAD = [
    {"id": "1", "_zbox_backend": "imap"},
    {"id": "2", "_zbox_backend": "imap"},
    {"id": "3", "_zbox_backend": "imap"},
]


class _Account:
    account_id = "acct1"


class _StatusBar:
    def __init__(self):
        self.text = None

    def SetStatusText(self, text):
        self.text = text


class _Notebook:
    def __init__(self):
        self.pages = []

    def FindPage(self, page):
        return self.pages.index(page) if page in self.pages else -1


class _Panel:
    """Stands in for the open MessageViewPanel doing the flipping."""

    _end_of_thread_prompt = None

    def __init__(self, envelope, thread_envelopes):
        self.envelope = envelope
        self.thread_envelopes = list(thread_envelopes)
        self.account = _Account()
        self.folder = "INBOX"


class _Worker:
    def __init__(self):
        self.jobs = []

    def submit(self, work_fn, on_success, on_error=None):
        self.jobs.append((work_fn, on_success, on_error))


class _Lanes:
    """Every lane is the one recording worker, so the tests see each
    job whichever lane it went to."""

    def __init__(self, worker):
        self._worker = worker

    def for_account(self, account_id):
        return self._worker

    def for_read(self, account_id, backend):
        return self._worker


class _Frame:
    open_thread_neighbour = main_frame.ZBoxMainFrame.open_thread_neighbour
    _end_of_thread = main_frame.ZBoxMainFrame._end_of_thread
    _is_live_envelope = main_frame.ZBoxMainFrame._is_live_envelope

    def __init__(self):
        self.announced = []
        self.paths = None
        self.worker = _Worker()
        self.lanes = _Lanes(self.worker)
        self.status = _StatusBar()
        self.notebook = _Notebook()
        self.opened = []
        self.closed = []
        self.events = []

    def GetStatusBar(self):
        return self.status

    def _open_message_tab(self, account, folder, envelope, message, thread_envelopes=None):
        self.events.append(("open", envelope["id"]))
        self.opened.append((envelope, message, list(thread_envelopes or [])))

    def _close_tab_at(self, index):
        self.events.append(("close", index))
        self.closed.append(index)

    def announce(self, text, title="ZBox"):
        self.announced.append(text)


class ThreadFlip(unittest.TestCase):
    def setUp(self):
        self.frame = _Frame()
        self.panel = _Panel(THREAD[1], THREAD)
        self.frame.notebook.pages = [object(), self.panel]
        self._real = himalaya_client.read_message_cached_only
        himalaya_client.read_message_cached_only = lambda *a, **k: {"body": "warm"}

    def tearDown(self):
        himalaya_client.read_message_cached_only = self._real

    def test_next_opens_the_following_message(self):
        self.frame.open_thread_neighbour(self.panel, 1)
        self.assertEqual(self.frame.opened[0][0]["id"], "3")

    def test_previous_opens_the_preceding_message(self):
        self.frame.open_thread_neighbour(self.panel, -1)
        self.assertEqual(self.frame.opened[0][0]["id"], "1")

    def test_the_new_tab_opens_before_the_old_one_closes(self):
        # Closing first would move focus to whatever the notebook
        # lands on, and the screen reader would announce that instead
        # of the message the user asked for.
        self.frame.open_thread_neighbour(self.panel, 1)
        self.assertEqual(self.frame.events, [("open", "3"), ("close", 1)])

    def test_exactly_one_tab_is_closed(self):
        self.frame.open_thread_neighbour(self.panel, 1)
        self.assertEqual(self.frame.closed, [1])

    def test_the_thread_travels_with_the_new_tab(self):
        # Otherwise flipping would work once and then stop.
        self.frame.open_thread_neighbour(self.panel, 1)
        self.assertEqual(self.frame.opened[0][2], THREAD)

    def test_the_position_is_announced(self):
        self.frame.open_thread_neighbour(self.panel, 1)
        self.assertEqual(self.frame.status.text, "Message 3 of 3 in this thread.")

    def test_past_the_end_warns_and_does_not_close_yet(self):
        panel = _Panel(THREAD[2], THREAD)
        self.frame.notebook.pages = [object(), panel]
        self.frame.open_thread_neighbour(panel, 1)
        self.assertEqual(self.frame.opened, [])
        self.assertEqual(self.frame.closed, [])
        self.assertEqual(
            self.frame.announced, ["Last message in this thread. Press again to close."]
        )

    def test_before_the_start_warns_and_does_not_close_yet(self):
        panel = _Panel(THREAD[0], THREAD)
        self.frame.notebook.pages = [object(), panel]
        self.frame.open_thread_neighbour(panel, -1)
        self.assertEqual(self.frame.opened, [])
        self.assertEqual(self.frame.closed, [])
        self.assertEqual(
            self.frame.announced, ["First message in this thread. Press again to close."]
        )

    def test_a_second_press_at_the_end_closes_the_message(self):
        panel = _Panel(THREAD[2], THREAD)
        self.frame.notebook.pages = [object(), panel]
        self.frame.open_thread_neighbour(panel, 1)
        self.frame.open_thread_neighbour(panel, 1)
        self.assertEqual(self.frame.closed, [1])
        self.assertEqual(self.frame.status.text, "Finished this thread (3 messages).")

    def test_the_announcement_is_not_repeated_on_the_closing_press(self):
        panel = _Panel(THREAD[2], THREAD)
        self.frame.notebook.pages = [object(), panel]
        self.frame.open_thread_neighbour(panel, 1)
        self.frame.open_thread_neighbour(panel, 1)
        self.assertEqual(len(self.frame.announced), 1)

    def test_turning_round_at_the_end_moves_instead_of_confirming(self):
        # Page Down at the end, then Page Up, is navigation -- not
        # "yes, close it". Note that a normal flip closes the old tab
        # too, so "did a tab close" cannot tell the two apart; what
        # distinguishes them is whether a message was opened.
        panel = _Panel(THREAD[2], THREAD)
        self.frame.notebook.pages = [object(), panel]
        self.frame.open_thread_neighbour(panel, 1)
        self.frame.open_thread_neighbour(panel, -1)
        self.assertEqual(self.frame.opened[0][0]["id"], "2")
        self.assertNotEqual(self.frame.status.text, "Finished this thread (3 messages).")

    def test_an_expired_prompt_does_not_close(self):
        panel = _Panel(THREAD[2], THREAD)
        self.frame.notebook.pages = [object(), panel]
        self.frame.open_thread_neighbour(panel, 1)
        # Armed a long time ago: the user has moved on and forgotten.
        step, _stamp = panel._end_of_thread_prompt
        panel._end_of_thread_prompt = (
            step, time.monotonic() - main_frame._END_OF_THREAD_CONFIRM_SECONDS - 1,
        )
        self.frame.open_thread_neighbour(panel, 1)
        self.assertEqual(self.frame.closed, [])
        self.assertEqual(len(self.frame.announced), 2)

    def test_moving_within_the_thread_disarms_a_pending_prompt(self):
        panel = _Panel(THREAD[2], THREAD)
        self.frame.notebook.pages = [object(), panel]
        self.frame.open_thread_neighbour(panel, 1)   # arms
        self.frame.open_thread_neighbour(panel, -1)  # moves to "2"
        self.assertIsNone(panel._end_of_thread_prompt)

    def test_a_message_with_no_thread_says_so(self):
        panel = _Panel(THREAD[0], [THREAD[0]])
        self.frame.open_thread_neighbour(panel, 1)
        self.assertEqual(self.frame.opened, [])
        self.assertEqual(
            self.frame.status.text, "This message is not part of a thread."
        )


class ThreadFlipColdCache(unittest.TestCase):
    def setUp(self):
        self.frame = _Frame()
        self.panel = _Panel(THREAD[0], THREAD)
        self.frame.notebook.pages = [object(), self.panel]
        self._real = himalaya_client.read_message_cached_only
        himalaya_client.read_message_cached_only = lambda *a, **k: None

    def tearDown(self):
        himalaya_client.read_message_cached_only = self._real

    def test_a_cold_message_is_read_on_the_worker_and_then_swapped(self):
        self.frame.open_thread_neighbour(self.panel, 1)
        # Nothing opened or closed yet -- the read is still in flight.
        self.assertEqual(self.frame.events, [])
        self.assertEqual(len(self.frame.worker.jobs), 1)

        _work, on_success, _on_error = self.frame.worker.jobs[0]
        on_success({"body": "cold"})
        self.assertEqual(self.frame.events, [("open", "2"), ("close", 1)])

    def test_a_failed_read_leaves_the_current_message_open(self):
        self.frame.open_thread_neighbour(self.panel, 1)
        _work, _on_success, on_error = self.frame.worker.jobs[0]
        on_error(RuntimeError("timed out"))
        self.assertEqual(self.frame.events, [])
        self.assertEqual(self.frame.status.text, "Could not open that message.")


if __name__ == "__main__":
    unittest.main()
