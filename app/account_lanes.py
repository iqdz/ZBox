"""
Who runs what, so no account can hold another one hostage.

Before this, every foreground job -- opening a message, listing a
folder, a preview read, a move -- for every account went through one
worker thread with one first-in, first-out queue. Live, 24 September
2026: a silent refresh of the unified Inbox queued a Disroot listing
whose connect hung for 20 seconds, and a Gmail message opened with
Enter behind it gave up after 5. The message was on disk the whole
time and reads in 35 ms.

AccountLanes gives each account its own foreground thread and gives
local reads (the offline copy, backend "maildir") a thread of their
own, so a slow server only ever delays its own account.

Reachability is the backoff for automatic work. After a connection
failure an account's automatic checks pause for 1 minute, then 2, 4
and 8, capped at 15, and resume at once on the next success. Things
the reader asks for are never held back by it.
"""

import threading
import time

from himalaya_worker import HimalayaWorker


class AccountLanes:
    """One foreground worker per account, plus one for local reads."""

    def __init__(self, factory=HimalayaWorker):
        self._factory = factory
        self._lanes = {}
        self._lock = threading.Lock()
        self.local = factory()

    def for_account(self, account_id):
        """The account's own foreground worker, created on first use."""
        lane = self._lanes.get(account_id)
        if lane is None:
            with self._lock:
                lane = self._lanes.get(account_id)
                if lane is None:
                    lane = self._factory()
                    self._lanes[account_id] = lane
        return lane

    def for_read(self, account_id, backend):
        """Local reads never queue behind the network."""
        if backend == "maildir":
            return self.local
        return self.for_account(account_id)

    def stop(self):
        with self._lock:
            lanes = list(self._lanes.values())
        for lane in lanes + [self.local]:
            lane.stop()


# Text that marks a failure as "the server could not be reached", as
# opposed to a refusal or a missing folder. Lower case.
_NETWORK_MARKERS = (
    "10060", "10061", "10065", "11001", "11004",
    "timed out", "timeout", "no such host", "getaddrinfo",
    "failed to respond", "network is unreachable", "connection refused",
    "connection reset", "connection aborted", "connect ",
)


def is_network_error(exc):
    """True when an error means the server could not be reached."""
    text = str(exc).lower()
    return any(marker in text for marker in _NETWORK_MARKERS)


class Reachability:
    """Per-account backoff for automatic work after connect failures."""

    BASE_SECONDS = 60
    MAX_SECONDS = 15 * 60

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._state = {}  # account_id -> (failures, resume_at)
        self._lock = threading.Lock()

    def allowed(self, account_id):
        """Whether automatic work may contact this account now."""
        with self._lock:
            state = self._state.get(account_id)
        return state is None or self._clock() >= state[1]

    def failed(self, account_id):
        """Records a connection failure. Returns the pause in seconds."""
        with self._lock:
            failures = self._state.get(account_id, (0, 0.0))[0] + 1
            delay = min(self.BASE_SECONDS * (2 ** (failures - 1)), self.MAX_SECONDS)
            self._state[account_id] = (failures, self._clock() + delay)
        return delay

    def succeeded(self, account_id):
        """Clears the backoff. Returns True if the account had one."""
        with self._lock:
            return self._state.pop(account_id, None) is not None
