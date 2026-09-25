"""
Audit finding 34: a bulk action (Delete/Archive/Move/Copy/flag/
mark-read on multiple selected messages) used to be one big loop
where the first failure raised out of the whole batch, silently
abandoning every message after it with no report of what happened,
and there was no way to cancel a long run. Covers run_bulk()'s
continue-on-error and cancellation control flow, and summarize()'s
text, directly -- no wx, no worker thread, no dialog.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from bulk_execution import run_bulk, summarize, failed_message_ids


def _contexts(n):
    return [
        ({"id": i}, ("account", "Inbox", i))
        for i in range(n)
    ]


class RunBulkContinuesOnError(unittest.TestCase):
    def test_all_succeed(self):
        seen = []
        result = run_bulk(_contexts(5), lambda envelope, account, folder, message_id: seen.append(message_id))
        self.assertEqual(seen, [0, 1, 2, 3, 4])
        self.assertEqual(result.completed, 5)
        self.assertEqual(result.total, 5)
        self.assertEqual(result.failures, [])
        self.assertFalse(result.cancelled)
        self.assertEqual(result.succeeded, 5)

    def test_a_failure_in_the_middle_does_not_stop_the_rest(self):
        seen = []

        def action(envelope, account, folder, message_id):
            if message_id == 2:
                raise RuntimeError("boom")
            seen.append(message_id)

        result = run_bulk(_contexts(5), action)
        # Every item was attempted, including 3 and 4 after the failure.
        self.assertEqual(seen, [0, 1, 3, 4])
        self.assertEqual(result.completed, 5)
        self.assertEqual(len(result.failures), 1)
        failed_envelope, exc = result.failures[0]
        self.assertEqual(failed_envelope["id"], 2)
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(result.succeeded, 4)

    def test_multiple_failures_are_all_collected(self):
        def action(envelope, account, folder, message_id):
            if message_id % 2 == 0:
                raise ValueError(f"bad {message_id}")

        result = run_bulk(_contexts(6), action)
        self.assertEqual(result.completed, 6)
        self.assertEqual(len(result.failures), 3)
        self.assertEqual(result.succeeded, 3)


class RunBulkCancellation(unittest.TestCase):
    def test_cancelling_before_any_item_attempts_nothing(self):
        seen = []
        result = run_bulk(_contexts(5), lambda *a: seen.append(a), is_cancelled=lambda: True)
        self.assertEqual(seen, [])
        self.assertEqual(result.completed, 0)
        self.assertTrue(result.cancelled)

    def test_cancelling_partway_through_stops_before_the_next_item(self):
        seen = []
        # Cancel takes effect once 3 items have been attempted.
        flag = {"cancel": False}

        def action(envelope, account, folder, message_id):
            seen.append(message_id)
            if message_id == 2:
                flag["cancel"] = True

        result = run_bulk(_contexts(5), action, is_cancelled=lambda: flag["cancel"])
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(result.completed, 3)
        self.assertEqual(result.total, 5)
        self.assertTrue(result.cancelled)

    def test_on_progress_is_not_called_for_an_item_skipped_by_cancellation(self):
        progress_calls = []
        result = run_bulk(
            _contexts(3), lambda *a: None,
            is_cancelled=lambda: True,
            on_progress=lambda completed, total: progress_calls.append((completed, total)),
        )
        self.assertEqual(progress_calls, [])
        self.assertEqual(result.completed, 0)


class RunBulkProgress(unittest.TestCase):
    def test_on_progress_fires_once_per_attempted_item_with_running_count(self):
        calls = []
        run_bulk(
            _contexts(3), lambda *a: None,
            on_progress=lambda completed, total: calls.append((completed, total)),
        )
        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])

    def test_on_progress_still_fires_for_a_failed_item(self):
        calls = []

        def action(envelope, account, folder, message_id):
            if message_id == 1:
                raise RuntimeError("boom")

        run_bulk(
            _contexts(3), action,
            on_progress=lambda completed, total: calls.append(completed),
        )
        self.assertEqual(calls, [1, 2, 3])


class SummarizeText(unittest.TestCase):
    def test_all_succeeded_returns_done_text_unchanged(self):
        result = run_bulk(_contexts(3), lambda *a: None)
        self.assertEqual(summarize(result, "Deleted 3 messages."), "Deleted 3 messages.")

    def test_some_failed_appends_a_failure_count(self):
        def action(envelope, account, folder, message_id):
            if message_id == 0:
                raise RuntimeError("boom")

        result = run_bulk(_contexts(3), action)
        self.assertEqual(
            summarize(result, "Deleted 3 messages."),
            "Deleted 3 messages. 1 of 3 messages failed.",
        )

    def test_one_failure_uses_singular_noun(self):
        def action(envelope, account, folder, message_id):
            raise RuntimeError("boom")

        result = run_bulk(_contexts(1), action)
        self.assertEqual(
            summarize(result, "Deleted."),
            "Deleted. 1 of 1 message failed.",
        )

    def test_cancelled_reports_progress_and_split(self):
        flag = {"cancel": False}

        def action(envelope, account, folder, message_id):
            if message_id == 1:
                raise RuntimeError("boom")
            if message_id == 2:
                flag["cancel"] = True

        result = run_bulk(_contexts(5), action, is_cancelled=lambda: flag["cancel"])
        self.assertEqual(
            summarize(result, "Deleted 5 messages."),
            "Cancelled after 3 of 5. 2 succeeded, 1 failed.",
        )


class FailedMessageIds(unittest.TestCase):
    """
    Covers the helper _toggle_flag_from_selection and
    _mark_multiple_read use to skip updating a row's local state for
    a message run_bulk actually failed on -- without this, a bulk
    flag/read-state action used to mark every selected row as updated
    regardless of which ones the server rejected.
    """

    def test_none_result_means_nothing_failed(self):
        contexts = _contexts(3)
        self.assertEqual(failed_message_ids(contexts, None), set())

    def test_no_failures_is_an_empty_set(self):
        contexts = _contexts(3)
        result = run_bulk(contexts, lambda *a: None)
        self.assertEqual(failed_message_ids(contexts, result), set())

    def test_failed_items_message_ids_are_returned(self):
        contexts = _contexts(4)

        def action(envelope, account, folder, message_id):
            if message_id in (1, 3):
                raise RuntimeError("boom")

        result = run_bulk(contexts, action)
        self.assertEqual(failed_message_ids(contexts, result), {1, 3})

    def test_succeeded_items_are_not_in_the_set(self):
        contexts = _contexts(3)

        def action(envelope, account, folder, message_id):
            if message_id == 0:
                raise RuntimeError("boom")

        result = run_bulk(contexts, action)
        failed = failed_message_ids(contexts, result)
        self.assertNotIn(1, failed)
        self.assertNotIn(2, failed)


if __name__ == "__main__":
    unittest.main()
