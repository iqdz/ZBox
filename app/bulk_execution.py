"""
Runs a bulk per-message action (Delete, Archive, Move To, Copy To,
flag toggle, Mark Read/Unread) with continue-on-error and cooperative
cancellation -- audit finding 34. Before this, one big work() closure
ran every context in a single try, so the first failure raised out of
the whole batch: on_error fired once and every remaining message was
silently abandoned with no report of what had actually happened, and
there was no way to stop a long run early.

Pure and wx-free on purpose: the control flow that matters here (stop
before the next item once cancelled, catch one item's failure without
losing the rest, keep an accurate completed count) is what audit
finding 34 was actually about, and it needs no wx widget to be
correct or to test. main_frame.py supplies the wx side -- a progress
dialog's Cancel button setting a threading.Event, and a status-bar/
summary presentation of the result.
"""

from dataclasses import dataclass, field

import lang


@dataclass
class BulkResult:
    completed: int
    total: int
    failures: list = field(default_factory=list)  # [(envelope, exception), ...]
    cancelled: bool = False

    @property
    def succeeded(self):
        return self.completed - len(self.failures)


def run_bulk(contexts, action, is_cancelled=None, on_progress=None):
    """
    contexts: list of (envelope, (account, folder, message_id)).
    action: callable(envelope, account, folder, message_id) -- may
            raise; one item's exception does not stop the others.
    is_cancelled: optional zero-arg callable, checked before each
                  item. Once it returns True the loop stops without
                  attempting any further item.
    on_progress: optional callable(completed, total), called after
                 each attempted item, success or failure alike --
                 never called for an item skipped by cancellation.

    Returns a BulkResult. 'completed' counts every item actually
    attempted (successes and failures both); 'failures' pairs each
    failed envelope with the exception action() raised for it.
    """
    total = len(contexts)
    failures = []
    completed = 0
    cancelled = False

    for envelope, (account, folder, message_id) in contexts:
        if is_cancelled is not None and is_cancelled():
            cancelled = True
            break
        try:
            action(envelope, account, folder, message_id)
        except Exception as exc:  # noqa: BLE001 -- one bad message must not sink the rest
            failures.append((envelope, exc))
        completed += 1
        if on_progress is not None:
            on_progress(completed, total)

    return BulkResult(completed=completed, total=total, failures=failures, cancelled=cancelled)


def failed_message_ids(contexts, result):
    """
    Given the same contexts list passed to run_bulk() and the
    BulkResult it returned, returns the set of message_ids whose
    action() call raised -- callers use this to skip updating a
    row's local state (a flag, read/unread) for a message the server
    actually rejected, instead of marking every selected row as
    updated regardless of what run_bulk reported.

    result may be None, meaning "nothing failed": the single-message,
    all-or-nothing bulk path only reaches its success callback when
    its one action() call succeeded, so there is nothing to filter.

    Matches by envelope identity (id()), not equality, since
    run_bulk's failures list pairs the exact envelope objects drawn
    from contexts, not copies.
    """
    if result is None or not result.failures:
        return set()
    envelope_to_message_id = {
        id(envelope): message_id for envelope, (_, _, message_id) in contexts
    }
    return {
        envelope_to_message_id[id(envelope)]
        for envelope, _exc in result.failures
        if id(envelope) in envelope_to_message_id
    }


def summarize(result, done_text):
    """
    One-line status text for a finished (or cancelled) bulk run.
    done_text is the caller's normal all-succeeded message (e.g.
    "Deleted 5 messages."), used as-is when nothing went wrong.
    """
    if result.cancelled:
        return lang.t(
            "actions_announcements", "bulk_cancelled",
            default=(
                f"Cancelled after {result.completed} of {result.total}. "
                f"{result.succeeded} succeeded, {len(result.failures)} failed."
            ),
            completed=result.completed, total=result.total,
            succeeded=result.succeeded, failed=len(result.failures),
        )
    if result.failures:
        if result.total == 1:
            return lang.t(
                "actions_announcements", "bulk_failed_one",
                default=f"{done_text} {len(result.failures)} of 1 message failed.",
                done=done_text, failed=len(result.failures), total=result.total,
            )
        return lang.t(
            "actions_announcements", "bulk_failed_many",
            default=(
                f"{done_text} {len(result.failures)} of {result.total} "
                f"messages failed."
            ),
            done=done_text, failed=len(result.failures), total=result.total,
        )
    return done_text
