"""
Thin wrapper around the Himalaya CLI. Every call runs the binary as a
subprocess with --config pointing at ZBox's generated himalaya.toml
and -a pointing at the specific account's TOML section, so multiple
accounts never collide. Every function here does blocking I/O, it is
the caller's job (himalaya_worker) to keep this off the UI thread.

Himalaya's --config flag splits its value on ':' to support merging
multiple config files (like a PATH variable). A Windows absolute path
such as D:\\project\\ZBox\\data\\config\\himalaya.toml gets misread as two
paths split at the drive letter's colon, "D" and the rest, which is
why Himalaya reported "No configuration found at D". To sidestep
that entirely, every call below runs with its working directory set
to the config folder and passes just the filename, so no colon ever
appears in the --config value.
"""

import datetime
import email.utils
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager

import imap_body_fetch

logger = logging.getLogger("zbox.himalaya")

# In-memory cache for parsed message reads. Reading spawns a Himalaya
# subprocess that reconnects to the IMAP server every time, which is
# the dominant cost (about a second per read in practice, regardless
# of message size). Caching the parsed result for the session makes
# re-opening the same message (select it, then press Enter) instant
# instead of running the whole subprocess again.
_read_cache = {}
_READ_CACHE_MAX = 200

# Prefetch failure cooldown: a message that just failed to read (a
# genuine server-side hiccup, a message the connection can't fetch
# quickly, etc.) used to get retried at the very start of every
# single prefetch sweep -- and since prefetch_messages tries unread
# mail first, a permanently-unreadable unread message parked sync_worker
# in an infinite loop, burning its full read_timeout on that one
# message every ~10-20 seconds, forever, and starving every other
# message (and every other account's IDLE-triggered sweep sharing the
# same worker) of any turn at all. Confirmed against a live debug log:
# two message ids alternated "Prefetch skipped ... after a read
# error" back to back with no gap for over a minute straight. A
# message that failed recently is now skipped outright -- not even
# attempted -- until its cooldown expires.
_PREFETCH_FAILURE_COOLDOWN_SECONDS = 120
_prefetch_failures = {}


class HimalayaError(Exception):
    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


def is_missing_folder_error(exc):
    """
    True when a HimalayaError (or any error whose text mirrors one)
    means the target mailbox simply does not exist on this account's
    server, e.g. "IMAP SELECT failed: NO Mailbox doesn't exist:
    Archive". This is the expected, benign case for ZBox's generic
    per-account folders (Inbox/Sent/Drafts/Trash/Junk/Archive) on any
    provider that never provisions all six -- disroot.org accounts
    have no Archive mailbox at all, unlike Gmail's naming mismatch
    (handled separately in provider_presets.SPECIAL_FOLDER_NAMES).
    Callers use this to tell "this account doesn't have that folder"
    apart from a genuine failure (auth, network, server error) that
    should still surface to the person as an error.
    """
    return "mailbox doesn't exist" in str(exc).lower()


class HimalayaBackgroundSkipped(Exception):
    """
    Raised by _run when background-priority work (message-body
    prefetch, IDLE-triggered checks, quiet offline top-ups -- anything
    running inside the background_priority() context) tried to start a
    Himalaya subprocess for an account that interactive work is
    currently using or waiting on. It means "nothing happened, try
    again later", never an error: the server was not contacted and no
    state was changed. Callers that expect this (the quiet background
    paths) treat it as a no-op or log it at debug level.
    """


class HimalayaShuttingDown(Exception):
    """
    Raised by _run_subprocess when a call could not start, or did not
    finish, because shutdown() is killing every Himalaya subprocess
    on the way out. Like HimalayaBackgroundSkipped it is not an
    error: the app is closing, nothing is left to receive a result,
    and the non-zero exit code it replaces is the kill itself.

    Deliberately not a HimalayaError subclass. The handlers scattered
    around this codebase that catch HimalayaError go on to warn, fall
    back, or retry, and every one of those is the wrong thing to do
    while the window is closing -- so this walks past them to the
    worker, which logs it at debug and stops.
    """


# Thread-local flag marking "this thread is doing background work":
# set by the background_priority() context manager around a worker
# job's work function, read by _run when it decides whether its
# subprocess call may stand down in favour of interactive work. The
# worker threads are shared between interactive and background jobs,
# so the lane a job belongs to cannot be inferred from the thread
# alone -- each background job opts in explicitly.
_BACKGROUND_LOCAL = threading.local()


@contextmanager
def background_priority():
    """
    Context manager marking the calling thread's Himalaya calls as
    background (stand-down-able) for its duration. Background work --
    message-body prefetch, IDLE-triggered envelope checks, quiet
    offline-cache top-ups -- must not run a second concurrent Himalaya
    subprocess for the same account while the user is doing something
    (opening a message, refreshing a folder, marking mail read): the
    per-account gate in _run makes those calls stand down instead.
    """
    previous = getattr(_BACKGROUND_LOCAL, "active", False)
    _BACKGROUND_LOCAL.active = True
    try:
        yield
    finally:
        _BACKGROUND_LOCAL.active = previous


class _AccountGate:
    """
    Serializes Himalaya subprocess invocations per account. The CLI
    reconnects to the IMAP server on every single invocation (login
    handshake included), and a burst of concurrent invocations for the
    same account -- e.g. a background prefetch read overlapping an
    IDLE-triggered envelope list overlapping the user's own
    open-message read -- was diagnosed (against a real user's debug
    log) as the trigger for provider-side throttling: envelope
    listings kept succeeding while body fetches hung and timed out.
    This gate allows at most one holder (one subprocess) per account
    at a time.

    Interactive work always takes priority: a background call that
    arrives while interactive work is active or waiting stands down
    immediately (never queues), and an interactive call arriving while
    a background call holds the account waits only for that one
    subprocess -- bounded by the background path's own short timeouts
    -- because every _run call releases the gate as soon as its
    single subprocess finishes.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._holder = None  # None | "interactive" | "background"
        self._interactive_waiters = 0
        self._interactive_active = 0

    def try_enter_background(self):
        """
        Non-blocking acquire for background work: succeeds only when
        no interactive work is active or waiting and nobody else is
        holding the account. Otherwise returns False and the caller
        stands down (raises HimalayaBackgroundSkipped) rather than
        queueing behind the user.
        """
        with self._condition:
            if (
                self._holder is None
                and self._interactive_waiters == 0
                and self._interactive_active == 0
            ):
                self._holder = "background"
                return True
            return False

    def enter_interactive(self):
        """
        Blocking acquire for interactive (user-triggered) work. Waits
        for the current holder -- at most one background subprocess,
        bounded by its own short timeout -- then takes the account.
        Interactive callers queue fairly among themselves.
        """
        with self._condition:
            self._interactive_waiters += 1
            try:
                while self._holder is not None:
                    self._condition.wait()
                self._holder = "interactive"
                self._interactive_active += 1
            finally:
                self._interactive_waiters -= 1

    def leave(self):
        """Releases the account after one subprocess call finishes.

        Must undo whatever enter_interactive() incremented, or
        _interactive_active only ever grows: confirmed live via a
        zbox_debug.log spanning 10+ minutes after a single interactive
        search, where try_enter_background()'s `_interactive_active ==
        0` check never passed again for either account, so every
        background prefetch/message-read-ahead call was "stood down"
        forever, not just while interactive work was genuinely active
        or waiting. Only an interactive holder ever bumped the
        counter, so only releasing one decrements it back.
        """
        with self._condition:
            if self._holder == "interactive":
                self._interactive_active -= 1
            self._holder = None
            self._condition.notify_all()


_HIMALAYA_ACCOUNT_GATES = {}
_HIMALAYA_ACCOUNT_GATES_LOCK = threading.Lock()


def _account_gate(account_id):
    """Returns the (lazily created) per-account Himalaya gate."""
    gate = _HIMALAYA_ACCOUNT_GATES.get(account_id)
    if gate is None:
        with _HIMALAYA_ACCOUNT_GATES_LOCK:
            gate = _HIMALAYA_ACCOUNT_GATES.get(account_id)
            if gate is None:
                gate = _AccountGate()
                _HIMALAYA_ACCOUNT_GATES[account_id] = gate
    return gate


# Every Himalaya subprocess currently running, so quitting can kill
# them instead of walking away. The worker threads are daemons: at
# interpreter exit they are simply frozen mid-call, and a child left
# blocked on a 90-second message read or a 60-second send keeps
# running -- with its IMAP/SMTP connection open -- after ZBox's window
# is gone. Nothing ever reaped them.
_ACTIVE_PROCESSES = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()
_SHUTTING_DOWN = False


def shutdown():
    """
    Kills every Himalaya subprocess still running and refuses to start
    any more. Called from the frame's close handler, before the app
    tears down, so a fetch or send in flight dies with the app rather
    than outliving it.

    Deliberately kill(), not terminate-then-wait: by this point the
    window is closing, nothing will consume the result, and Himalaya's
    own work is a stateless single command -- there is nothing to
    flush and nobody left to tell. Returns how many were killed, for
    the log.
    """
    global _SHUTTING_DOWN
    with _ACTIVE_PROCESSES_LOCK:
        _SHUTTING_DOWN = True
        processes = list(_ACTIVE_PROCESSES)
        _ACTIVE_PROCESSES.clear()
    for process in processes:
        try:
            process.kill()
        except Exception:  # noqa: BLE001 -- already exited, or the OS
            # refused; either way there is nothing useful to do about
            # it while quitting.
            logger.debug("Could not kill a Himalaya subprocess during shutdown.", exc_info=True)
    if processes:
        logger.debug("Killed %d Himalaya subprocess(es) during shutdown.", len(processes))
    try:
        closed = imap_body_fetch.close_all()
    except Exception:  # noqa: BLE001 - quitting; a connection that
        # refuses to close cleanly must not block the close handler.
        logger.debug("Could not close pooled IMAP fetch connections.", exc_info=True)
    else:
        if closed:
            logger.debug("Closed %d pooled IMAP fetch connection(s).", closed)
    return len(processes)


def _run(paths, account, args, timeout=30, input_text=None, backend=None):
    """
    Runs one Himalaya subprocess for 'account', serialized per account
    and with interactive work taking priority over background work:

    * At most one Himalaya invocation per account runs at a time (see
      _AccountGate) -- the CLI performs a full IMAP login on every
      invocation, and concurrent invocations for the same account
      were the diagnosed trigger for provider-side body-fetch
      throttling.

    * Calls made from inside the background_priority() context
      (prefetch, IDLE checks, quiet offline top-ups) stand down --
      raise HimalayaBackgroundSkipped without contacting the server --
      whenever interactive work for the same account is active or
      waiting, instead of queueing behind the user. They simply get
      retried on a later round.

    * Interactive calls wait for at most the one subprocess currently
      holding the account, then proceed.
    """
    background = bool(getattr(_BACKGROUND_LOCAL, "active", False))
    gate = _account_gate(account.account_id)
    if background:
        if not gate.try_enter_background():
            logger.debug(
                "Standing down background Himalaya call for %s (%s): "
                "interactive work active or waiting.",
                account.account_id, " ".join(args[:3]),
            )
            raise HimalayaBackgroundSkipped(
                f"Background Himalaya call for {account.account_id} stood down "
                "because interactive work for the same account is active."
            )
    else:
        gate.enter_interactive()
    try:
        return _run_subprocess(
            paths, account, args,
            timeout=timeout, input_text=input_text, backend=backend,
        )
    finally:
        gate.leave()


def _run_subprocess(paths, account, args, timeout=30, input_text=None, backend=None):
    config_dir = os.path.dirname(paths.himalaya_config_file)
    config_filename = os.path.basename(paths.himalaya_config_file)

    command = [
        paths.himalaya_binary,
        "--config", config_filename,
        "-a", account.toml_section_name(),
        "--json",
    ]
    if backend is not None:
        # Explicit on every shared command (mailbox/envelope/message/
        # flag/attachment) rather than trusting Himalaya's 'auto'
        # backend pick. Once an account also has maildir.root set
        # (offline mode), the account has two read-capable backends
        # configured at once, and 'auto' picks "the first configured
        # backend it supports" -- an order that isn't documented and
        # isn't worth gambling the normal online path on.
        command += ["-b", backend]
    command += args

    logger.debug("Running (cwd=%s): %s", config_dir, " ".join(command))

    started = time.monotonic()
    # Popen rather than subprocess.run so the running child has a
    # handle that shutdown() can kill -- run() gives none, which is
    # why a quit mid-fetch used to leave himalaya.exe running. Every
    # other detail is kept identical to the run() call this replaced:
    # stdin is only a pipe when there is input to send (run() does the
    # same), the same text/encoding/errors decoding, the same
    # CREATE_NO_WINDOW, and the same CompletedProcess shape handed to
    # the response handling below.
    if input_text is not None:
        # Line endings are normalised to bare LF before the text reaches
        # the child's stdin. Popen below runs in text mode with
        # newline=None, so Python translates every LF it writes into
        # the platform separator -- CRLF on Windows. A raw RFC822
        # message already carrying CRLF therefore went down the pipe as
        # CR CR LF. Measured 10 September 2026: every message in the
        # offline cache was stored that way, which broke header parsing
        # outright (see _message_id_from_raw), and the same doubling
        # reached outgoing mail and saved drafts through
        # send_message_raw and add_message_raw. Normalising here means
        # the translation produces exactly one CRLF per line.
        #
        # Only the subprocess read path hid this: reading in text mode
        # collapses CRLF back to LF, so a message fetched that way and
        # written straight back looked correct. The pooled IMAP path
        # preserves real CRLF, and it handles almost every fetch.
        input_text = input_text.replace("\r\n", "\n").replace("\r", "\n")
    stdin_pipe = subprocess.PIPE if input_text is not None else None
    with _ACTIVE_PROCESSES_LOCK:
        if _SHUTTING_DOWN:
            raise HimalayaShuttingDown(
                "ZBox is shutting down; no new Himalaya call was started."
            )
    try:
        process = subprocess.Popen(
            command,
            cwd=config_dir,
            stdin=stdin_pipe,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except FileNotFoundError as exc:
        raise HimalayaError("Himalaya binary not found.") from exc

    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(process)
    try:
        try:
            stdout_text, stderr_text = process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Kill first, then drain: whatever the process had already
            # written before it was killed is the only way to see what
            # Himalaya was doing right before a hang, since a killed
            # process never reaches the normal returncode/stdout
            # handling below. Also logs actual elapsed time so a hang
            # that's timing out right at the requested ceiling
            # (genuinely still working, just slow) is distinguishable
            # from one that was stuck well before it, doing nothing,
            # for the whole window.
            process.kill()
            try:
                partial_out, partial_err = process.communicate()
            except Exception:  # noqa: BLE001 -- draining a killed
                # process must never replace the timeout error with a
                # less useful one.
                partial_out, partial_err = "", ""
            elapsed = time.monotonic() - started
            partial_stdout = (partial_out or "").strip()
            partial_stderr = (partial_err or "").strip()
            logger.warning(
                "Himalaya timed out after %.1fs (requested timeout %ss). "
                "Partial stdout: %s | Partial stderr: %s",
                elapsed, timeout,
                partial_stdout or "(none captured)",
                partial_stderr or "(none captured)",
            )
            raise HimalayaError(f"Himalaya timed out after {timeout}s.") from exc
    finally:
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES.discard(process)

    result = subprocess.CompletedProcess(
        command, process.returncode, stdout_text, stderr_text,
    )

    elapsed = time.monotonic() - started
    if elapsed > 3:
        logger.debug("Himalaya call took %.1fs: %s", elapsed, " ".join(args[:3]))

    if result.returncode != 0:
        stdout_text = result.stdout.strip()
        stderr_text = result.stderr.strip()
        with _ACTIVE_PROCESSES_LOCK:
            killed_by_shutdown = _SHUTTING_DOWN
        if killed_by_shutdown:
            # shutdown() killed this child a moment ago, so the
            # non-zero code and the empty output are the kill, not a
            # failure. Reported through the normal path it produced a
            # WARNING here and a full traceback from the worker, one
            # per job in flight, at the exact moment someone reading
            # the log is looking for what actually went wrong.
            logger.debug(
                "Himalaya call ended with code %s while shutting down: %s",
                result.returncode, " ".join(args[:3]),
            )
            raise HimalayaShuttingDown(
                "The Himalaya subprocess was killed because ZBox is closing."
            )
        logger.warning(
            "Himalaya command failed (code %s). stderr: %s | stdout: %s",
            result.returncode, stderr_text or "(empty)", stdout_text or "(empty)",
        )
        # With --json, Himalaya sometimes writes error details as JSON
        # to stdout instead of plain text to stderr, so surface both.
        detail = stderr_text or stdout_text or "(no error output captured)"
        raise HimalayaError(
            f"Himalaya exited with code {result.returncode}: {detail}",
            stderr=detail,
        )

    if not result.stdout.strip():
        return None

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HimalayaError("Could not parse Himalaya's output as JSON.") from exc

    # Always log a preview of successful output too, not just
    # failures. A prior bug class here was a silent failure: if
    # Himalaya's JSON shape doesn't match what the caller expects
    # (e.g. wrapped in an object instead of a bare list), the crash
    # happened inside a UI-thread callback that never reached this
    # module's error logging at all, so nothing showed up in the log
    # despite a real problem. Seeing the actual shape here closes
    # that gap for good.
    logger.debug("Himalaya returned: %s", _response_preview(result.stdout.strip()))

    return parsed


def _as_list(data, wrapper_keys=("envelopes", "mailboxes", "items", "data", "results")):
    """
    Normalizes Himalaya's JSON output to a plain list. Handles the
    case where a result is wrapped in an object, e.g. {"envelopes":
    [...]}, instead of being a bare JSON array, since that shape was
    never confirmed and a wrong assumption here previously caused a
    fetch to report success while silently showing nothing.

    "mailboxes" added after live-testing 'mailbox list --json' on
    this Himalaya build, which wraps its array under that key (not
    under any of the previously-covered names) -- without it,
    list_folders always returned an empty list.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in wrapper_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        logger.warning(
            "Himalaya returned an object with none of the expected "
            "list keys %s: keys present were %s",
            wrapper_keys, list(data.keys()),
        )
        return []
    logger.warning("Himalaya returned an unexpected type: %s", type(data).__name__)
    return []


def list_folders(paths, account):
    """
    Returns a list of folder/mailbox dicts, each with at least a
    'name' key. Himalaya renamed 'folder list' to 'mailbox list' in
    recent versions; if this fails on an older Himalaya build, that's
    the first thing to check.
    """
    data = _run(paths, account, ["mailbox", "list"], backend="imap")
    return _as_list(data)


def list_all_folders(paths, account, counts=False):
    """
    Like list_folders, but includes unsubscribed mailboxes too. A
    plain 'mailbox list' is LSUB (subscribed only); '--all' is LIST
    (every mailbox) -- confirmed from the pinned himalaya.exe's own
    embedded help text via strings, the same technique used to
    confirm 'envelope search' for audit finding 39: "Lists subscribed
    mailboxes (LSUB) by default, or every mailbox (LIST) with --all."
    Used for the account tree and folder management (audit finding
    42), where an unsubscribed folder still needs to be visible to be
    managed or re-subscribed. list_folders keeps the subscribed-only
    default for Move To / Copy To, unchanged.

    counts=True adds '--counts' (audit finding 43): also confirmed via
    strings, against the shared (protocol-agnostic) MailboxListCommand
    specifically -- "Populate per-mailbox message counts (TOTAL and
    UNREAD columns)" -- distinct from the IMAP-specific 'imap mailbox
    list' subcommand's own MailboxRow (name/delimiter/attributes only,
    no counts). The shared himalaya::email::mailbox::Mailbox struct's
    own doc comments (also read via strings) confirm the resulting
    JSON fields are 'total' and 'unread', each explicitly "None when
    the backend was not asked or cannot answer cheaply" -- see
    _mailbox_unread_count, which is what actually reads them.
    """
    if counts:
        # '--counts' belongs to the shared MailboxListCommand, which
        # is the only one of the two that reports TOTAL/UNREAD.
        args = ["mailbox", "list", "--counts"]
    else:
        # '--all' does NOT belong to the shared 'mailbox list' -- it
        # is a flag on the flat, protocol-specific 'imap list'
        # subcommand, not a nested 'imap mailbox list' (that path
        # doesn't exist -- 'unrecognized subcommand 'mailbox'', the
        # same wrong-nesting bug audit finding 42's other five calls
        # had). Confirmed against the pinned himalaya.exe's own
        # --help: "himalaya.exe imap list [OPTIONS]", with
        # '-A, --all' among its options. Passing '--all' to the
        # shared command made every single folder listing fail with
        # "error: unexpected argument '--all' found" (seen for all
        # three accounts in one real session's debug log), so the
        # account tree silently fell back to its fixed folder list
        # every launch.
        args = ["imap", "list", "--all"]
    data = _run(paths, account, args, backend="imap")
    return _as_list(data)


def _mailbox_unread_count(entry):
    """
    Reads the 'unread' field from one mailbox dict returned by
    list_all_folders(..., counts=True), tolerant of it being absent,
    null, or (defensively) some non-int shape -- see that function's
    docstring for why 'unread' can legitimately be missing/null even
    on success. Returns None rather than 0 whenever the count isn't
    actually known, so a caller never shows a false "0 unread".
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get("unread")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def create_mailbox(paths, account, name):
    """
    Creates a new top-level mailbox (CREATE, RFC 3501) via 'imap
    create' -- a flat, protocol-specific subcommand, not a shared-API
    sibling of 'mailbox list'. Nested folders (a real hierarchy under
    an existing mailbox) are out of scope: the per-account hierarchy
    delimiter Himalaya reports isn't surfaced anywhere in this client
    yet, and guessing it risks silently creating
    "Parent<wrong-delimiter>Child" as one flat, oddly-named mailbox
    instead of a real child. Audit finding 42.
    """
    return _run(paths, account, ["imap", "create", name], backend="imap")


def delete_mailbox(paths, account, name):
    """
    Permanently deletes a mailbox and every message inside it
    (DELETE, RFC 3501). Himalaya itself asks for no confirmation --
    the caller must, before reaching this. Audit finding 42.
    """
    return _run(paths, account, ["imap", "delete", name], backend="imap")


def rename_mailbox(paths, account, name, new_name):
    """Renames an existing mailbox (RENAME, RFC 3501). Audit finding 42."""
    return _run(paths, account, ["imap", "rename", name, new_name], backend="imap")


def subscribe_mailbox(paths, account, name):
    """
    Subscribes to a mailbox (SUBSCRIBE, RFC 3501) so it appears again
    in a plain 'mailbox list' (LSUB). Audit finding 42.
    """
    return _run(paths, account, ["imap", "subscribe", name], backend="imap")


def unsubscribe_mailbox(paths, account, name):
    """
    Unsubscribes from a mailbox (UNSUBSCRIBE, RFC 3501). The mailbox
    and its messages are untouched; it just stops appearing in a
    plain 'mailbox list' (LSUB) until re-subscribed. Audit finding 42.
    """
    return _run(paths, account, ["imap", "unsubscribe", name], backend="imap")


def list_envelopes(paths, account, folder="INBOX", page_size=200, page=1, backend="imap", allow_pooled=True):
    """
    Returns a list of envelope dicts (id, from, subject, date, ...).
    page_size raised well above a typical inbox size as a mitigation
    for a since-ruled-out sort-order theory; kept high since it's
    harmless either way.

    page selects which page of page_size results comes back (1 is
    the most recent messages), confirmed against the pinned
    himalaya.exe's own --help text ("Page number, starting from 1.
    The most recent envelopes are on page 1"). Audit finding 11:
    every caller used to leave this at Himalaya's own default of 1
    forever, with no way to reach anything past the first page_size
    messages in a folder.

    backend defaults to the live IMAP connection; pass backend=
    "maildir" to read the local offline copy instead (used when
    Work Offline is active, or to browse a folder that was synced
    but isn't the one currently open online).

    allow_pooled=False forces the Himalaya subprocess even on the
    IMAP backend. Only one caller wants this: AccountManager's
    connection test, which exists to prove the TOML ZBox writes
    works end to end through himalaya.exe. The pooled path bypasses
    both the TOML and the binary, so a pass from it would not mean
    what the wizard's Test button claims it means.
    """
    if backend == "imap" and allow_pooled:
        # The last cold-subprocess call on the path the user feels: a
        # fresh IMAP login per folder switch, measured at 13.7s and
        # 21.8s in a real session's log. Over the pooled connection
        # it is one SEARCH plus one FETCH on an already-authenticated
        # socket. Same fall-back-on-any-failure rule as read_message,
        # and the same shape out either way -- see
        # imap_body_fetch._envelope_dict, which exists to match this
        # function's own JSON.
        try:
            return imap_body_fetch.list_envelopes(
                paths, account, folder=folder,
                page_size=page_size, page=page,
                background=bool(getattr(_BACKGROUND_LOCAL, "active", False)),
            )
        except imap_body_fetch.ImapBodyFetchUnavailable as exc:
            logger.debug(
                "Pooled IMAP envelope list unavailable for %s (%s); using the "
                "Himalaya subprocess instead.", folder, exc,
            )

    data = _run(
        paths, account,
        ["envelope", "list", "-m", folder, "--page-size", str(page_size), "--page", str(page)],
        backend=backend,
    )
    envelopes = _as_list(data)
    if backend == "maildir":
        envelopes = _unique_by_message_id(envelopes)
    return envelopes


def _unique_by_message_id(envelopes):
    """
    The offline copy's envelopes with only the first of each canonical
    Message-ID kept; envelopes without one are all kept.

    The startup list is filled from the offline maildir, and a copy an
    older sync wrote more than once showed as one row per file: live
    24 September 2026, about twelve files each for three messages
    whose Message-ID Outlook had folded onto the next line. The first
    sync removes the extra files (_remove_duplicate_copies); this keeps
    them off the list until it does.
    """
    seen = set()
    unique = []
    for envelope in envelopes:
        key = _envelope_message_id(envelope)
        if key:
            if key in seen:
                continue
            seen.add(key)
        unique.append(envelope)
    return unique


def _escape_search_pattern(term):
    """
    Escapes a user-typed search term for embedding as a <pattern> in
    Himalaya's own query DSL (see search_envelopes below). Confirmed
    live against the pinned himalaya v2.1.0 x86_64-linux release, in
    a sandboxed maildir account -- same method add_message_raw's
    finding-40 fix above used, since the vendored himalaya/himalaya.exe
    is a Windows binary this shell can't execute.

    The DSL's own tokenizer splits a pattern at the first
    unescaped space, `(` or `)` it finds, no matter which argv
    element that character arrived in -- reproduced live with the
    exact parse error a real zbox_debug.log showed for a user's
    "memory bank" search ("found 'b' expected space between
    filters, `and`, `or`, or end of input"). A double-quoted
    pattern ('"memory bank"') was tried first, since that's the
    usual way a DSL protects an embedded space, and it does stop
    the parse error -- but the quotes are then kept as literal
    characters in the match text rather than stripped as
    delimiters, so a quoted pattern silently matches nothing real
    ever again. What the grammar actually wants is a backslash
    escape: '\\ ' keeps an embedded space inside one pattern token,
    and '\\(' / '\\)' likewise protect the DSL's own grouping
    characters -- confirmed live, including round-tripping a
    message with a real 'C:\\temp (notes)' substring in its subject
    header and matching it with exactly the escaping this function
    produces. A literal backslash in the term itself is escaped to
    '\\\\' so the grammar doesn't try to read whatever follows it as
    one of its own escape targets.

    Backslash must be escaped first -- escaping it after the other
    replacements would double-escape the backslashes those had just
    inserted.
    """
    escaped = term.replace("\\", "\\\\")
    escaped = escaped.replace(" ", "\\ ")
    escaped = escaped.replace("(", "\\(")
    escaped = escaped.replace(")", "\\)")
    return escaped


def search_envelopes(paths, account, term, folder, page_size=200, backend="imap"):
    """
    Audit finding 39: searches one folder's envelopes for `term`
    against From, Subject and Body, via the pinned Himalaya build's
    'envelope search' subcommand and its own query DSL (confirmed
    directly from the vendored himalaya/himalaya.exe -- this build's
    embedded help text, not the project's public docs, which cover a
    different/older version): conditions are 'subject <pattern>',
    'from <pattern>', 'body <pattern>', ... combined with 'and'/'or'/
    'not'.

    This docstring used to claim here that each condition/pattern
    argv element is kept atomic all the way through, spaces
    included, since subprocess never re-splits argv the way a shell
    would -- that part is true, but it turned out not to matter: a
    live zbox_debug.log from a user's "memory bank" search showed
    the identical DSL parse error repeated across every folder in
    both accounts, proving Himalaya's own clap-based CLI rejoins
    every trailing positional argv element back into one string and
    re-tokenizes THAT with its own space-delimited grammar, argv
    boundaries or not. See _escape_search_pattern above for the fix
    (and the live-tested reasoning behind it); `term` is escaped
    once here and the same escaped pattern is reused for all three
    conditions below.

    All option flags (-m, --page-size) are placed before the query
    tokens, not after: a trailing catch-all positional in a clap CLI
    like this one can swallow anything placed after it starts,
    flags included.

    Returns a plain envelope list, same shape as list_envelopes --
    callers stamp _zbox_account_id / _zbox_folder onto each envelope
    themselves, the same convention _fetch_unified_envelopes already
    uses, since a caller merging results from more than one folder
    needs each row to carry its own origin.
    """
    pattern = _escape_search_pattern(term)
    query = ["subject", pattern, "or", "from", pattern, "or", "body", pattern]
    data = _run(
        paths, account,
        ["envelope", "search", "-m", folder, "--page-size", str(page_size), *query],
        backend=backend, timeout=45,
    )
    return _as_list(data)


def _sanitize_folder_name(folder):
    """Filesystem-safe version of a folder name for use as (part of)
    a cache subdirectory name (folder names can contain '/', which
    would otherwise be read as a path separator).

    Not collision-safe on its own -- every non-alphanumeric character
    maps to the same '_', so distinct real folder names can sanitize
    to the same string ("Sent Mail" and "Sent_Mail" both become
    "Sent_Mail", and so would two differently-punctuated Gmail
    labels). See _message_cache_folder_key below, which is what
    actually names the directory; this is kept only as that key's
    human-readable prefix."""
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (folder or "folder"))


def _message_cache_folder_key(folder):
    """
    Directory name for one folder's slice of the message cache --
    audit finding 38. _sanitize_folder_name alone isn't unique, so
    this appends a short hash of the real, unsanitized folder name:
    two folders that sanitize the same way ("Sent Mail" and
    "Sent_Mail") still get different directories, because their
    hashes differ even though their sanitized prefixes don't. The
    prefix is kept purely so the userdata folder stays readable
    (message_cache/Sent_Mail-3f9a2c1b0d rather than an opaque hash
    alone); only the hash suffix is load-bearing for uniqueness.
    """
    digest = hashlib.sha256((folder or "folder").encode("utf-8")).hexdigest()[:10]
    return f"{_sanitize_folder_name(folder)}-{digest}"


def _message_cache_dir(paths, account, folder):
    directory = os.path.join(
        paths.cache_dir(account.account_id), "message_cache",
        _message_cache_folder_key(folder),
    )
    os.makedirs(directory, exist_ok=True)
    return directory


def _message_cache_path(paths, account, folder, message_id, backend):
    return os.path.join(_message_cache_dir(paths, account, folder), f"{backend}_{message_id}.json")


def _load_disk_cached_message(paths, account, folder, message_id, backend):
    """
    On-disk counterpart to _read_cache: survives app restarts, unlike
    the in-memory cache, which is why opening a message can still be
    slow the very first time after launch even though it was read
    last session. Returns None on any miss or read problem -- a
    cache is never allowed to fail the caller, only the live read is.
    """
    path = _message_cache_path(paths, account, folder, message_id, backend)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _save_disk_cached_message(paths, account, folder, message_id, backend, message):
    path = _message_cache_path(paths, account, folder, message_id, backend)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(message, handle)
    except OSError:
        logger.exception("Could not write message cache to %s", path)


def _invalidate_cached_message(paths, account, folder, message_id, backend=None):
    """
    Drops one message from both the in-memory and on-disk cache.
    Call this wherever a message's identity at (account, folder, id)
    stops being valid -- moved, deleted, or permanently purged (audit
    finding 37): an IMAP id is a mailbox-scoped UID, so once a message
    leaves `folder` that id can eventually be reassigned to a
    completely different message there (a UIDVALIDITY change), and a
    stale cache entry would silently serve whichever message it was
    holding under that id -- the deleted one, or later someone else's.

    backend=None (the default) clears both the imap and maildir cache
    entries for this id, since a caller invalidating after a move or
    delete doesn't necessarily know which backend last cached it.

    Also seeds prefetch_messages' own failure cooldown
    (_prefetch_failures) for this id, so a prefetch round already
    under way -- its worklist is a snapshot taken before this
    move/delete happened -- skips the id outright on its next check
    instead of issuing a live read that can only fail. Confirmed from
    a real debug log: a background prefetch tried to read a message
    id about a second after the interactive path had just permanently
    deleted it, twice, each logged as an ERROR ("FETCH returned no
    body for the requested message" is Himalaya's shape for "that id
    doesn't exist here any more"). The seeded entry expires on the
    same _PREFETCH_FAILURE_COOLDOWN_SECONDS timer as a real failure --
    by the time it lapses, the next envelope-list poll will already
    have dropped this id from prefetch's worklist for real.
    """
    for one_backend in (backend,) if backend else ("imap", "maildir"):
        key = (account.account_id, folder, str(message_id), one_backend)
        _read_cache.pop(key, None)
        _prefetch_failures[key] = time.monotonic()
        path = _message_cache_path(paths, account, folder, message_id, one_backend)
        try:
            os.remove(path)
        except OSError:
            pass


_MESSAGE_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 200 MB per account
_MESSAGE_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days


def prune_message_cache(
    paths, account,
    max_bytes=_MESSAGE_CACHE_MAX_BYTES,
    max_age_seconds=_MESSAGE_CACHE_MAX_AGE_SECONDS,
):
    """
    Enforces a size and age cap on one account's on-disk message
    cache (audit finding 37: data/userdata/<id>/message_cache grew
    without bound -- confirmed on a real profile at 130+ MB for one
    account alone, with nothing ever removing an entry). Two passes:

      1. Delete any cached file whose mtime is older than
         max_age_seconds outright.
      2. If what's left still exceeds max_bytes, delete oldest-mtime
         files first until it doesn't.

    Meant to run occasionally in the background (see
    ZBoxMainFrame._schedule_message_cache_prune), not on every
    message read -- a mostly-empty or already-within-limits cache
    costs one cheap os.walk and returns immediately. Safe to call on
    an account with no cache directory yet. Returns the number of
    files removed, mainly for tests and logging.
    """
    cache_root = os.path.join(paths.cache_dir(account.account_id), "message_cache")
    if not os.path.isdir(cache_root):
        return 0

    surviving = []  # (mtime, size, path)
    removed = 0
    now = time.time()

    for dirpath, _dirnames, filenames in os.walk(cache_root):
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            try:
                stat_result = os.stat(full_path)
            except OSError:
                continue
            if max_age_seconds is not None and (now - stat_result.st_mtime) > max_age_seconds:
                try:
                    os.remove(full_path)
                    removed += 1
                except OSError:
                    pass
                continue
            surviving.append((stat_result.st_mtime, stat_result.st_size, full_path))

    if max_bytes is not None:
        total_bytes = sum(size for _mtime, size, _path in surviving)
        if total_bytes > max_bytes:
            surviving.sort(key=lambda entry: entry[0])  # oldest mtime first
            for _mtime, size, full_path in surviving:
                if total_bytes <= max_bytes:
                    break
                try:
                    os.remove(full_path)
                    removed += 1
                    total_bytes -= size
                except OSError:
                    pass

    return removed


# --- Cache clean up by message date -----------------------------------
#
# Tools > Cache Clean Up Configuration. prune_message_cache above judges
# a file by when it was written; this judges it by when the message
# itself was sent, which is what "old mail" means to the person
# choosing the age. It covers both local caches -- the per-message
# JSON bodies and the offline Maildir copy, which nothing else ever
# shrank -- and it removes copies of messages still in the account on
# purpose: the aim is a cache that stays bounded, and anything removed
# comes back the next time it is opened.

_DAY_SECONDS = 24 * 60 * 60


def _header_date_timestamp(value):
    """An RFC 5322 Date header value as a POSIX timestamp, or None
    when it is missing or unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _cached_message_timestamp(path):
    """
    The Date of a message held in the on-disk body cache, read from
    the headers on parts[0] -- the shape both imap_body_fetch and
    Himalaya's 'message read' produce. Header values are tagged
    ({"Text": ...}), so the first string inside the tag is used.
    None when the file is unreadable or carries no usable Date.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            message = json.load(handle)
    except (OSError, ValueError):
        return None
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return None
    for header in parts[0].get("headers") or []:
        if not isinstance(header, dict) or header.get("name") != "date":
            continue
        value = header.get("value")
        if isinstance(value, dict):
            value = next((item for item in value.values() if isinstance(item, str)), None)
        return _header_date_timestamp(value)
    return None


def _raw_date_timestamp(raw_text):
    """The Date header of a raw RFC822 message as a timestamp, or
    None. Same header-block walk as _message_id_from_raw, carriage
    returns stripped first for the same reason."""
    if not raw_text:
        return None
    for line in raw_text.replace("\r", "").split("\n"):
        if not line.strip():
            break
        if line[:1] in (" ", "\t"):
            continue
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "date":
            return _header_date_timestamp(value)
    return None


def _drop_expired_prefetch_failures(now=None):
    """
    Removes cooldown entries old enough to have expired. Without this
    _prefetch_failures only ever grew: _invalidate_cached_message
    seeds an entry for every moved or deleted id, and an id that no
    longer exists is never retried, which is the only other place an
    entry is removed. Returns how many were dropped.
    """
    now = time.monotonic() if now is None else now
    expired = [
        key for key, stamp in list(_prefetch_failures.items())
        if now - stamp >= _PREFETCH_FAILURE_COOLDOWN_SECONDS
    ]
    for key in expired:
        _prefetch_failures.pop(key, None)
    return len(expired)


def _envelope_older_than(envelope, max_age_days, now=None):
    """
    True when an envelope's own date is older than max_age_days.
    False for no limit, and for a missing or unparseable date: a
    message whose age is unknown is never skipped on that basis.
    Imported lazily -- envelope_format is a pure helper module, but
    nothing else in this file depends on it at import time.
    """
    if not max_age_days:
        return False
    from envelope_format import _parse_envelope_date
    parsed = _parse_envelope_date(envelope)
    if parsed.year == 1:
        return False
    try:
        stamp = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return False
    now = time.time() if now is None else now
    return stamp < now - int(max_age_days) * _DAY_SECONDS


def prune_caches_by_message_date(paths, max_age_days, now=None):
    """
    Deletes every cached message body and every offline Maildir copy
    whose message Date is older than max_age_days, across every
    profile under the cache root -- including a directory left behind
    by an account that was since removed, the same walk
    clear_offline_caches does. A file with no usable Date falls back
    to its modification time.

    Local copies only; no server is contacted. Also drops expired
    prefetch-cooldown entries. Returns (bodies_removed,
    offline_removed).
    """
    removed_bodies = 0
    removed_offline = 0
    _drop_expired_prefetch_failures()
    try:
        days = int(max_age_days)
    except (TypeError, ValueError):
        return removed_bodies, removed_offline
    root = getattr(paths, "cache", None)
    if days <= 0 or not root or not os.path.isdir(root):
        return removed_bodies, removed_offline
    now = time.time() if now is None else now
    cutoff = now - days * _DAY_SECONDS

    def is_old(stamp, path):
        if stamp is None:
            try:
                stamp = os.path.getmtime(path)
            except OSError:
                return False
        return stamp < cutoff

    for entry in os.listdir(root):
        profile = os.path.join(root, entry)
        if not os.path.isdir(profile):
            continue

        bodies_root = os.path.join(profile, "message_cache")
        if os.path.isdir(bodies_root):
            for current, _dirs, filenames in os.walk(bodies_root):
                for filename in filenames:
                    path = os.path.join(current, filename)
                    if not is_old(_cached_message_timestamp(path), path):
                        continue
                    try:
                        os.remove(path)
                        removed_bodies += 1
                    except OSError as exc:
                        logger.debug("Could not remove %s: %s", path, exc)

        offline_root = os.path.join(profile, "offline_mail")
        if os.path.isdir(offline_root):
            for current, _dirs, filenames in os.walk(offline_root):
                # Only delivered messages. tmp holds files still being
                # written, and a Maildir's other files are not mail.
                if os.path.basename(current) not in ("cur", "new"):
                    continue
                for filename in filenames:
                    path = os.path.join(current, filename)
                    try:
                        with open(path, "rb") as handle:
                            head = handle.read(65536)
                    except OSError:
                        continue
                    stamp = _raw_date_timestamp(head.decode("latin-1", "replace"))
                    if not is_old(stamp, path):
                        continue
                    try:
                        os.remove(path)
                        removed_offline += 1
                    except OSError as exc:
                        logger.debug("Could not remove %s: %s", path, exc)

    logger.info(
        "Cache clean up by message date (older than %d day(s)): removed %d "
        "cached body file(s) and %d offline copy file(s).",
        days, removed_bodies, removed_offline,
    )
    return removed_bodies, removed_offline


def read_message(paths, account, message_id, folder="INBOX", backend="imap", timeout=90):
    """
    Returns the message dict for one envelope id.

    Results are cached two ways: in memory for the running session
    (fastest, but gone on restart), and on disk under the account's
    userdata profile (see _message_cache_path), so a message read
    last session -- or warmed by prefetch_messages -- opens instantly
    with no subprocess at all, either now or after a restart. Keyed
    by (account, folder, id, backend) throughout.

    timeout defaults to 90s, not the 30s every other Himalaya call
    uses. A message with attachments genuinely takes this long to
    fetch over IMAP -- confirmed by running Himalaya directly,
    outside ZBox, against three messages (two attachments each) that
    consistently failed to open: 27.2s, 28.5s, and 34.3-34.5s. The
    old 30s ceiling sat right on that boundary, so opening one of
    these was a coin flip, and retrying it landed on roughly the same
    ~30s wall every time -- indistinguishable, from the screen
    reader's side, from a message that would never open at all.
    prefetch_messages passes a much shorter timeout on purpose: see
    its docstring for why a slow prefetch read shouldn't get to hold
    the single background worker thread for anywhere near that long
    -- a message that misses that shorter window simply stays
    unwarmed and pays this full, now-realistic cost when actually
    opened instead.
    """
    key = (account.account_id, folder, str(message_id), backend)
    cached = _read_cache.get(key)
    if cached is not None:
        logger.debug("Serving message %s from in-memory cache.", key)
        return cached

    disk_cached = _load_disk_cached_message(paths, account, folder, message_id, backend)
    if disk_cached is not None:
        logger.debug("Serving message %s from on-disk cache.", key)
        _read_cache[key] = disk_cached
        return disk_cached

    data = None
    if backend == "imap":
        # Fast path: a connection held open across reads, instead of
        # a fresh subprocess that logs in from scratch every time.
        # See imap_body_fetch's module docstring for the measured
        # cost this avoids. Any failure at all falls through to the
        # subprocess below, which produces the same result.
        try:
            data = imap_body_fetch.fetch_message(
                paths, account, message_id, folder=folder,
                background=bool(getattr(_BACKGROUND_LOCAL, "active", False)),
            )
        except imap_body_fetch.ImapBodyFetchUnavailable as exc:
            logger.debug(
                "Pooled IMAP fetch unavailable for %s (%s); using the Himalaya "
                "subprocess instead.", key, exc,
            )
            data = None

    if data is None:
        data = _run(
            paths, account,
            ["message", "read", str(message_id), "-m", folder],
            backend=backend,
            timeout=timeout,
        )
    if data is not None:
        _read_cache[key] = data
        if len(_read_cache) > _READ_CACHE_MAX:
            _read_cache.pop(next(iter(_read_cache)))
        _save_disk_cached_message(paths, account, folder, message_id, backend, data)
    return data


def read_message_raw(paths, account, message_id, folder="INBOX", backend="imap", timeout=90):
    """
    Returns the message's full raw RFC822 source (headers + body,
    unparsed) as a string. Two callers: File > Save As > File..., and
    sync_folder_offline, which reads every message it copies down into
    the local Maildir. The '--raw' flag is the one proven in that
    sync, where the global --json flag wraps it as {"message": "..."}.

    Not cached (unlike read_message), and deliberately so for both
    callers. Save As is an occasional action, not something opening a
    message triggers on every click; and the offline sync writes what
    it reads straight into the Maildir copy, which is its own cache --
    putting it in _read_cache as well would evict every warm
    interactive entry (_READ_CACHE_MAX is 200) on a single
    100-message pass, to hold a second copy of what is now on disk.

    timeout applies only to the subprocess fallback; the pooled path
    has its own, shorter one (imap_body_fetch.FETCH_TIMEOUT_SECONDS).
    The offline sync passes less than an interactive Save As does: a
    message that is slow through the cold path should be given up on
    and retried on the next top-up, not allowed to hold the sync
    worker for ninety seconds while the loop still has ninety-nine
    messages to go.
    """
    if backend == "imap":
        # Same fast path as read_message. This one matters most for
        # the offline sync, which calls it once per message it
        # copies: through the subprocess path that is a fresh IMAP
        # login every time, measured at 22.1s for a single message in
        # a real session's log.
        try:
            return imap_body_fetch.fetch_message_raw(
                paths, account, message_id, folder=folder,
                background=bool(getattr(_BACKGROUND_LOCAL, "active", False)),
            )
        except imap_body_fetch.ImapBodyFetchUnavailable as exc:
            logger.debug(
                "Pooled IMAP raw fetch unavailable for %s/%s (%s); using the "
                "Himalaya subprocess instead.", folder, message_id, exc,
            )

    raw = _run(
        paths, account,
        ["message", "read", str(message_id), "-m", folder, "--raw"],
        backend=backend, timeout=timeout,
    )
    raw_text = raw.get("message") if isinstance(raw, dict) else raw
    return raw_text or ""


def read_message_cached_only(paths, account, message_id, folder="INBOX", backend="imap"):
    """
    Returns a message body already sitting in the cache (memory,
    then disk) with no Himalaya subprocess call at all, or None on a
    miss. Used to open a message instantly when a background
    prefetch already warmed the cache; callers fall back to a normal
    read_message() call on a miss.
    """
    key = (account.account_id, folder, str(message_id), backend)
    cached = _read_cache.get(key)
    if cached is not None:
        return cached
    disk_cached = _load_disk_cached_message(paths, account, folder, message_id, backend)
    if disk_cached is not None:
        _read_cache[key] = disk_cached
    return disk_cached


_PREFETCH_READ_TIMEOUT = 10
"""
Per-message subprocess timeout used only by prefetch_messages, well
under the normal 30s every interactive read gets. Confirmed live
against a real user's debug log: one background-account IMAP read
took 27.2s (Running 01:33:42.183 -> returned 01:34:09.369) while its
siblings took 1-4s -- an outlier, not a fluke this app can assume
away. prefetch_messages shares ZBoxMainFrame.sync_worker -- the same
single background thread -- with the *next* prefetch round (queued
every ~20s, by the auto-refresh poll, for whatever folder is open),
which is exactly how newly-landed mail gets warmed: should_continue
already makes a superseded round bail out, but only in between
messages, so a read that runs long still blocks that handoff for its
own full duration. Cutting the per-message ceiling to 10s bounds how
late a fresher round -- carrying the mail that just arrived, and
warmed first now that prefetch prioritizes unread messages -- can be
kept waiting behind a slow prefetch attempt. A message that times out
here just stays uncached for now: opening it directly still works (a
normal foreground read, uncapped at 30s, same as before), and it gets
another prefetch attempt next round.
"""


def prefetch_messages(paths, account, envelopes, folder, backend="imap", limit=25, should_continue=None, read_timeout=_PREFETCH_READ_TIMEOUT, max_age_days=None):
    """
    Warms the message cache (memory + disk) for up to 'limit'
    envelopes from a just-loaded folder that aren't cached yet, so
    opening one of them a moment later is instant instead of paying
    Himalaya's roughly one-second-per-read subprocess cost right at
    click time. Runs entirely synchronously and does real network
    I/O per message -- callers must submit this to a background
    worker, and specifically a low-priority one (see
    ZBoxMainFrame.sync_worker), never the interactive worker that
    folder switches and message opens queue on.

    should_continue, if given, is checked before every message and
    stops the prefetch immediately once it returns False -- e.g. the
    user already moved on to a different folder, or a fresher
    prefetch round for the same folder has since been queued behind
    this one, so continuing to warm this round's cache would only
    delay real work now queued behind it on the same worker.

    read_timeout (see _PREFETCH_READ_TIMEOUT) caps each individual
    read well below the normal 30s so one slow message can't hold up
    that handoff to a fresher round for anywhere near that long.

    Read failures -- including a read_timeout expiring -- are logged
    and skipped rather than raised, since a single unreadable or slow
    message (e.g. a transient server hiccup) should never abort
    prefetching the rest of the folder; a skipped message simply
    isn't cached yet and gets a normal opening from timing here.

    max_age_days, when given, skips any envelope whose own date is
    older than that -- the Cache Clean Up Configuration age. Warming
    those would only refill what the daily clean up just removed.
    """
    warmed = 0
    for envelope in envelopes:
        if warmed >= limit:
            break
        if should_continue is not None and not should_continue():
            break
        message_id = envelope.get("id")
        if message_id is None:
            continue
        if _envelope_older_than(envelope, max_age_days):
            continue
        key = (account.account_id, folder, str(message_id), backend)
        if key in _read_cache:
            continue
        if _load_disk_cached_message(paths, account, folder, message_id, backend) is not None:
            continue
        failed_at = _prefetch_failures.get(key)
        if failed_at is not None:
            if time.monotonic() - failed_at < _PREFETCH_FAILURE_COOLDOWN_SECONDS:
                continue
            del _prefetch_failures[key]
        try:
            read_message(paths, account, message_id, folder=folder, backend=backend, timeout=read_timeout)
        except HimalayaBackgroundSkipped:
            # Interactive work for this account started (a message
            # open, a folder switch, a mark-as-read...) while this
            # warm-up round was running. Prefetch is optional work --
            # stand the whole round down rather than attempting the
            # next message, which would stand down too; the next
            # poll's sweep retries once the user is done.
            logger.debug(
                "Prefetch for %s/%s stood down: interactive work active.",
                account.account_id, folder,
            )
            break
        except HimalayaError:
            _prefetch_failures[key] = time.monotonic()
            logger.debug(
                "Prefetch skipped message %s in %s/%s after a read error "
                "(will not retry for %ss).",
                message_id, account.account_id, folder, _PREFETCH_FAILURE_COOLDOWN_SECONDS,
            )
        warmed += 1
    return warmed


def _set_flag(paths, account, message_id, folder, flag, on):
    """
    One flag change, over the pooled IMAP connection where possible.

    Through the subprocess these cost a full IMAP login to send a
    single STORE, and mark-as-read runs immediately after every
    message is opened -- so it sat right on the path the user feels.
    Any failure falls back to Himalaya, which is still correct.
    """
    try:
        imap_body_fetch.set_flag(paths, account, message_id, folder, flag, on)
        return
    except imap_body_fetch.ImapBodyFetchUnavailable as exc:
        logger.debug(
            "Pooled IMAP flag change unavailable for %s/%s (%s); using the "
            "Himalaya subprocess instead.", folder, message_id, exc,
        )
    action = "add" if on else "remove"
    _run(
        paths, account,
        ["flag", action, str(message_id), "--flag", flag, "-m", folder],
        timeout=15, backend="imap",
    )


def mark_as_read(paths, account, message_id, folder="INBOX"):
    """Sets \\Seen. The subprocess fallback's syntax is confirmed
    across multiple independent sources:
    'himalaya flag add <id> --flag seen -m <folder>'."""
    _set_flag(paths, account, message_id, folder, "seen", True)


def mark_as_unread(paths, account, message_id, folder="INBOX"):
    _set_flag(paths, account, message_id, folder, "seen", False)


def _run_with_folder_flag(paths, account, base_args, folder, flags=("--folder", "-m"), timeout=30, backend="imap"):
    """
    Himalaya 2.x/3.x is inconsistent about which flag selects the
    source folder across subcommands: envelope list, message read,
    flag add and message delete accept '-m <folder>', while message
    move rejects '-m' in favour of '--folder'. Callers pass the flag
    order proven for their subcommand; any 'unexpected argument'
    error retries the next spelling, so one build's quirk never
    hard-fails the operation.
    """
    last_error = None
    for flag in flags:
        try:
            return _run(paths, account, base_args + [flag, folder], timeout=timeout, backend=backend)
        except HimalayaError as exc:
            last_error = exc
            if "unexpected argument" not in str(exc):
                raise
    raise last_error


def move_to_trash(paths, account, message_id, folder="INBOX", trash_folder="Trash"):
    """
    Moves a message to the Trash mailbox: Himalaya's 'message move'
    copies the message into the destination folder and removes it
    from the source, which is the standard IMAP move-to-trash
    behavior. Takes the source folder as -f/--from -- unlike the
    other shared commands (envelope list, message read, flag add,
    message delete), which all use -m/--folder. See docs/decisions.md
    for the bug that mattered.

    trash_folder must be the account's REAL trash mailbox, resolved
    through provider_presets -- "[Gmail]/Trash" on Gmail, never the
    literal string "Trash"; see docs/decisions.md for why that broke
    Delete on Gmail specifically.

    Moving to Trash is also the right operation rather than a
    coincidence. On Gmail, folders are labels: deleting from a folder
    removes that label and leaves the message in All Mail, while a
    move to Trash removes every other label and starts the deletion
    grace period. That is the behaviour people expect from Delete, and
    it is exactly the approach the GTrash Thunderbird extension takes
    for the same reason.
    """
    if _same_folder(folder, trash_folder):
        # Already in Trash. Moving a message to the folder it is
        # already in is not a no-op on every server, and it is not
        # what anyone means by Delete here: in Thunderbird, deleting
        # from Trash removes the message. Himalaya's own trash-first
        # policy does exactly that for a message already in trash.
        delete_message(paths, account, message_id, folder=folder)
        return

    move_message(paths, account, message_id, folder, trash_folder)


def move_message(paths, account, message_id, from_folder, to_folder):
    """Confirmed syntax: 'himalaya message move <id> --from <src> --to <dst>'."""
    _run(
        paths, account,
        ["message", "move", str(message_id), "--from", from_folder, "--to", to_folder],
        timeout=30, backend="imap",
    )
    # The id this message had in from_folder stops meaning this
    # message the instant it moves -- audit finding 37.
    _invalidate_cached_message(paths, account, from_folder, message_id)


def copy_message(paths, account, message_id, from_folder, to_folder):
    """Confirmed syntax: 'himalaya message copy <id> --from <src> --to <dst>',
    the same shape as move_message (see its docstring)."""
    _run(
        paths, account,
        ["message", "copy", str(message_id), "--from", from_folder, "--to", to_folder],
        timeout=30, backend="imap",
    )


def set_flagged(paths, account, message_id, folder, flagged):
    """
    Sets or clears the \\Flagged ("starred") state. Uses the same
    'flag add'/'flag remove --flag flagged -m <folder>' shape already
    confirmed for mark_as_read/mark_as_unread's \\Seen handling.
    """
    _set_flag(paths, account, message_id, folder, "flagged", flagged)


def delete_message(paths, account, message_id, folder="INBOX"):
    """
    Runs Himalaya's 'message delete' once. On the pinned
    himalaya/himalaya.exe (v2.1.0), that follows a documented trash-first
    policy: the message moves to Trash, unless it's already there, in
    which case it's permanently removed ('himalaya message delete
    --help'). There is no 'expunge' subcommand on any Himalaya build.

    So a single call here is a real permanent delete ONLY when
    `folder` already IS the account's Trash mailbox (that's exactly
    what Empty Trash relies on, calling this directly). For a message
    living anywhere else, this call just moves it into Trash -- for
    an unconditional "permanently delete no matter where it lives"
    action, see permanently_delete_message() below. See
    docs/decisions.md for the fake-expunge-fallback bug this behavior
    replaced.
    """
    _run_with_folder_flag(
        paths, account,
        ["message", "delete", str(message_id)],
        folder, flags=("-m", "--folder"), timeout=30,
    )
    # Either genuinely gone, or moved to Trash under the trash-first
    # policy this docstring describes -- either way this id no longer
    # means this message in `folder` (audit finding 37).
    _invalidate_cached_message(paths, account, folder, message_id)


def purge_folder(paths, account, folder, trash_folder, envelopes):
    """
    Permanently removes every message in `envelopes` from `folder` --
    the batched engine behind both Empty Trash and Empty Junk (V21,
    and the same-shaped context-menu action beside it). Each envelope
    needs its own "id"; when `folder` is not already Trash, its
    "message-id" header is used too (see below). Missing either
    costs only that one message, not the whole batch -- see the
    return value.

    Two shapes, chosen by whether `folder` already is `trash_folder`:

    Already Trash (Empty Trash's case): one 'message delete' call
    with every id. Himalaya's trash-first policy makes that a true
    permanent delete when the messages are already there ('message
    delete --help') -- this was V21's whole point, one subprocess
    launch instead of one per message. Followed unconditionally by
    one 'imap expunge' on the folder: a server without UIDPLUS only
    flags the messages \\Deleted rather than removing them outright
    ('message delete --help'); expunge reclaims them, and is a
    harmless no-op wherever nothing was left flagged.

    Anywhere else (Empty Junk): the messages have to genuinely reach
    Trash first, and 'message delete's own trash-first "move" step is
    not trustworthy for that on Gmail -- see move_to_trash. So this
    runs the same two steps as permanently_delete_messages:
    trash_messages_batch (one UID MOVE per source folder), then
    purge_moved_from_trash, which only ever matches a Trash UID above
    Trash's highest UID from before the move. This path no longer
    runs 'imap expunge' on the whole Trash folder: with UIDPLUS the
    delete already expunges exactly its own UIDs, and a blanket
    expunge would also remove any other \\Deleted-flagged message
    sitting in Trash.

    Returns a list of (message_id, reason) for anything not confirmed
    purged, with the reasons those two steps report ("no_header",
    "not_found", "purge_failed", "flagged_only", "move_failed").
    Raises HimalayaError only when not a single message could be
    moved, so the caller's error path still covers "nothing happened
    at all".
    """
    ids = [envelope.get("id") for envelope in envelopes if envelope.get("id") is not None]
    if not ids:
        return []

    def purge_in_trash(purge_ids):
        _run_with_folder_flag(
            paths, account,
            ["message", "delete"] + [str(message_id) for message_id in purge_ids],
            trash_folder, flags=("-m", "--folder"), timeout=300,
        )
        _run(paths, account, ["imap", "expunge", trash_folder], timeout=120)
        for message_id in purge_ids:
            _invalidate_cached_message(paths, account, trash_folder, message_id)

    if _same_folder(folder, trash_folder):
        purge_in_trash(ids)
        return []

    items = [
        (envelope.get("id"), folder, envelope.get("message-id"))
        for envelope in envelopes if envelope.get("id") is not None
    ]
    pre_max, moved, failed = trash_messages_batch(paths, account, items, trash_folder)
    if failed and not moved:
        raise HimalayaError(
            "Could not move any of the %d message(s) in %s to %s."
            % (len(failed), folder, trash_folder)
        )
    unresolved = [(item[0], "move_failed") for item in failed]
    unresolved.extend(
        (message_id, reason)
        for message_id, _folder, reason
        in purge_moved_from_trash(paths, account, moved, trash_folder, pre_max)
    )
    return unresolved


def _same_folder(folder_a, folder_b):
    """Case-insensitive mailbox-name comparison. Himalaya/IMAP don't
    guarantee canonical casing, and a message's current folder and
    the tree's resolved Trash name can come from different lookups
    that don't always agree on case."""
    return (folder_a or "").strip().lower() == (folder_b or "").strip().lower()


def undo_moves(paths, moves):
    """
    Reverses a batch of prior moves recorded by
    undo_manager.UndoManager -- audit finding 48. Each item in
    `moves` is an UndoableMove-shaped object (account,
    message_id_header, from_folder, to_folder): from_folder is where
    undo puts the message back, to_folder is where the earlier action
    left it.

    Groups by (account, to_folder) and lists each such folder exactly
    once -- one listing per folder instead of one per message, since
    the id a message has in to_folder was assigned by the earlier
    move and was never recorded anywhere (audit finding 37 again).

    A Message-ID can match more than one message in to_folder: an
    older copy with the same header, a list duplicate, a copy sent to
    yourself. Each move takes the NEWEST unclaimed match, the highest
    UID, because the copy the action put there got the folder's next
    UID at move time. The old header-to-id map kept the last entry of
    a newest-first listing, which is the oldest copy, so undo moved
    the wrong message back and left the right one behind. Headers are
    compared in canonical form. No pre-move UID mark is used, unlike
    purge_moved_from_trash: Gmail's Archive only removes the Inbox
    label, so the message keeps its old UID in All Mail and a mark
    would never match it.

    Moves back go through move_messages_batch, one move per
    (to_folder, from_folder) pair, and are judged by re-reading
    to_folder rather than by exit code.

    Returns the move entries that were not reversed: no match in
    to_folder (moved, deleted or expunged again since), or still
    there after the move back. Nothing is raised for a partial
    failure: one message failing should not stop the rest.
    """
    by_account_folder = {}
    for move in moves:
        by_account_folder.setdefault((move.account, move.to_folder), []).append(move)

    unresolved = []
    for (account, to_folder), group in by_account_folder.items():
        envelopes = list_envelopes(
            paths, account, folder=to_folder, page_size=500, backend="imap"
        )
        ids_by_header = {}
        for envelope in envelopes:
            header = _envelope_message_id(envelope)
            current_id = envelope.get("id")
            if header and current_id is not None:
                ids_by_header.setdefault(header, []).append(current_id)
        for ids in ids_by_header.values():
            ids.sort(key=_uid_sort_key, reverse=True)

        by_destination = {}
        for move in group:
            ids = ids_by_header.get(_canonical_message_id(move.message_id_header))
            if not ids:
                unresolved.append(move)
                continue
            item = (ids.pop(0), to_folder, move.message_id_header)
            by_destination.setdefault(move.from_folder, []).append((item, move))

        for from_folder, pairs in by_destination.items():
            _moved, failed = move_messages_batch(
                paths, account, [item for item, _move in pairs], from_folder,
            )
            failed_ids = {str(item[0]) for item in failed}
            unresolved.extend(move for item, move in pairs if str(item[0]) in failed_ids)

    return unresolved


def _uid_sort_key(value):
    """Numeric order for IMAP UIDs; anything non-numeric sorts last
    when sorting newest first."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


# --- Batched move to Trash and permanent deletion ----------------------
#
# Session s. Deleting or permanently deleting a selection used to cost
# one himalaya.exe per message moved plus one per message purged:
# measured 18 messages, 39 seconds, 36 subprocesses, each paying its
# own IMAP login. It also let background work run in every gap between
# them, since the account gate is released after each call.
#
# Himalaya 2.1.0's 'message move' and 'message delete' both take many
# ids and send one UID MOVE / one STORE plus UID EXPUNGE for the set,
# so a batch is now a handful of calls however large it is.
#
# The part that matters most is finding the moved copies in Trash.
# IMAP gives a moved message a new UID there and Himalaya does not
# report it, so the old code matched by Message-ID against a listing.
# That listing is newest first and the map kept the LAST entry per
# header -- the oldest copy -- so when Trash already held an older
# message with the same Message-ID (an earlier Delete, a copy sent to
# yourself, a list duplicate) the recoverable one was destroyed and
# the one just moved was left behind. A UID in a folder is never
# reused, so only a Trash UID above Trash's highest UID from before
# the move can be a message this batch put there. That is the rule
# purge_moved_from_trash applies.

_BATCH_ID_CHUNK = 500


def _chunks(items, size=_BATCH_ID_CHUNK):
    """Consecutive slices of at most `size`. 500 ids stay far below
    Windows' 32,767-character command-line limit."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _delete_left_flagged(report):
    """
    Whether a 'message delete' report says the server only flagged
    the messages instead of removing them -- a server without
    UIDPLUS, where Himalaya cannot expunge just its own UIDs. The
    report's exact JSON shape is not yet confirmed live, so this
    looks for the word rather than one key.
    """
    if report is None:
        return False
    try:
        text = json.dumps(report).lower()
    except (TypeError, ValueError):
        return False
    return "flagged" in text


def folder_uids(paths, account, folder):
    """
    Every UID in `folder` as a set of ints. The pooled connection
    first (NOOP plus SEARCH ALL); if that is unavailable, a paged
    subprocess listing. Raises HimalayaError when neither can answer.
    """
    try:
        return set(imap_body_fetch.folder_uids(paths, account, folder))
    except imap_body_fetch.ImapBodyFetchUnavailable as exc:
        logger.debug(
            "Pooled UID search unavailable for %s (%s); listing through the "
            "Himalaya subprocess instead.", folder, exc,
        )
    uids = set()
    page = 1
    while True:
        batch = _as_list(_run(
            paths, account,
            ["envelope", "list", "-m", folder, "--page-size", "2000", "--page", str(page)],
            backend="imap", timeout=120,
        ))
        for envelope in batch:
            try:
                uids.add(int(envelope.get("id")))
            except (TypeError, ValueError, AttributeError):
                continue
        if len(batch) < 2000 or page >= _MAILDIR_SCAN_MAX_PAGES:
            break
        page += 1
    return uids


def move_messages(paths, account, message_ids, from_folder, to_folder):
    """
    Moves many messages with one 'message move <ids...> --from <src>
    --to <dst>' per chunk -- a single IMAP UID MOVE for the set.

    Every id is dropped from the cache, and its prefetch cooldown
    seeded, BEFORE the move rather than after: a prefetch round
    already under way would otherwise spend a read on each id the
    moment the server stopped having it.

    Raises HimalayaError if a chunk fails; earlier chunks have moved
    by then. A caller that needs to know exactly what moved re-reads
    the source folder, as trash_messages_batch does.
    """
    ids = [str(message_id) for message_id in message_ids if message_id is not None]
    if not ids:
        return
    for message_id in ids:
        _invalidate_cached_message(paths, account, from_folder, message_id)
    for chunk in _chunks(ids):
        _run(
            paths, account,
            ["message", "move", *chunk, "--from", from_folder, "--to", to_folder],
            timeout=300, backend="imap",
        )


def forget_offline_copies(paths, account, folder, message_id_headers):
    """
    Deletes the local offline Maildir copies in `folder` whose
    Message-ID is one of `message_id_headers`. Offline sync only ever
    adds, so without this a deleted or moved message stayed in the
    offline copy -- and in the cached preview -- for good. Local files
    only. Returns how many were removed.

    Every local copy with a matching Message-ID goes, so if two
    distinct copies shared one and only one left the folder, the
    survivor is simply copied down again on the next top-up.
    """
    wanted = {
        canonical for canonical in
        (_canonical_message_id(header) for header in message_id_headers or ())
        if canonical
    }
    if not wanted:
        return 0
    maildir_dir = _maildir_folder_dir(paths, account, folder)
    if maildir_dir is None:
        return 0
    removed = 0
    for sub in ("cur", "new"):
        directory = os.path.join(maildir_dir, sub)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                with open(path, "rb") as handle:
                    head = handle.read(65536)
            except OSError:
                continue
            if _message_id_from_raw(head.decode("latin-1", "replace")) not in wanted:
                continue
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:
                logger.debug("Could not remove %s: %s", path, exc)
    return removed


def resolve_live_items(paths, account, items):
    """
    `items` -- (message_id, folder, message_id_header, ...) tuples for
    one account -- with every id that is not a server UID replaced by
    the live one, matched on the Message-ID header.

    A row shown from the offline copy carries the maildir file id
    (1790188764.#0M...HSPC), not the IMAP UID, until the live listing
    replaces it. Acting on that id sent it to the server, which
    refused it as an invalid UID, and the row came back. The header
    is the one key both copies share. One live listing per folder,
    newest 500, which covers anything the offline copy can show.

    Items already carrying a UID are returned untouched. An item that
    cannot be matched (no header, not in the live listing, or the
    listing failed) is returned unchanged and fails the way it always
    did, so nothing is silently dropped. Never raises.
    """
    items = [tuple(item) for item in items]
    pending = {}
    for index, item in enumerate(items):
        if item[0] is None or _uid_of(item) is not None:
            continue
        pending.setdefault(item[1], []).append(index)
    for folder, indices in pending.items():
        try:
            envelopes = list_envelopes(
                paths, account, folder=folder, page_size=500, backend="imap",
            )
        except Exception as exc:  # noqa: BLE001 -- the action reports its own failure
            logger.warning(
                "Could not list %s to match offline-copy rows: %s", folder, exc,
            )
            continue
        live = {}
        for envelope in envelopes or []:
            key = _envelope_message_id(envelope)
            if key and envelope.get("id") is not None:
                live.setdefault(key, envelope["id"])
        for index in indices:
            item = items[index]
            key = _canonical_message_id(item[2]) if len(item) > 2 else None
            uid = live.get(key) if key else None
            if uid is None:
                logger.warning(
                    "Offline-copy row in %s has no live match by Message-ID.", folder,
                )
                continue
            items[index] = (uid,) + item[1:]
    return items


def move_messages_batch(paths, account, items, to_folder):
    """move_messages_batch after resolve_live_items, so a row still
    showing the offline copy moves like any other."""
    return _move_messages_batch_resolved(
        paths, account, resolve_live_items(paths, account, items), to_folder,
    )


def _move_messages_batch_resolved(paths, account, items, to_folder):
    """
    Moves `items` into `to_folder` with one move per source folder.
    Shared by Delete, Archive, Mark as Junk, Mark as Not Junk, Move To
    and Undo.

    `items` is an iterable of (message_id, folder, message_id_header)
    for one account, none of them already in to_folder.

    Returns (moved, failed), lists of the same item tuples, decided by
    re-reading each source folder afterwards: a message whose UID is
    still there did not move. A move that errors part way is judged
    the same way. If the source folder cannot be re-read, a move that
    errored counts its whole group as failed and one that reported
    success is trusted. Offline copies of what moved are removed from
    the source folder's local copy. Never raises for a failed move.
    """
    items = [item for item in items if item[0] is not None]
    groups = {}
    for item in items:
        groups.setdefault(item[1], []).append(item)

    account_label = getattr(account, "account_id", account)
    moved = []
    failed = []
    for folder, group in groups.items():
        move_error = None
        try:
            _move_uids(paths, account, [item[0] for item in group], folder, to_folder)
        except HimalayaError as exc:
            move_error = exc
            logger.warning(
                "Batched move of %d message(s) from %s to %s failed on %s: %s",
                len(group), folder, to_folder, account_label, exc,
            )
        try:
            remaining = folder_uids(paths, account, folder)
        except HimalayaError as exc:
            logger.warning(
                "Could not re-read %s after moving to %s on %s: %s",
                folder, to_folder, account_label, exc,
            )
            if move_error is not None:
                failed.extend(group)
                continue
            # The move itself reported success; trust it.
            remaining = set()
        if move_error is None and any(_uid_of(item) in remaining for item in group):
            # The move reported success yet some of its messages are
            # still listed. Gmail shows a MOVE made on the Himalaya
            # connection to another session a moment later: live, 16
            # September 2026, 63 messages out of [Gmail]/Spam all
            # reached Inbox while the immediate re-read still listed
            # some of them, and Mark as Not Junk reported a failure
            # that had not happened. One pause and a second read
            # before anything counts as failed.
            _pause_before_recheck()
            try:
                remaining = folder_uids(paths, account, folder)
            except HimalayaError as exc:
                logger.warning(
                    "Could not re-read %s a second time on %s: %s",
                    folder, account_label, exc,
                )
        group_moved = []
        group_failed = []
        for item in group:
            if _uid_of(item) in remaining:
                group_failed.append(item)
            else:
                group_moved.append(item)
        if group_failed and move_error is None:
            logger.warning(
                "%d of %d message(s) still listed in %s after a move to %s "
                "that reported success on %s.",
                len(group_failed), len(group), folder, to_folder, account_label,
            )
        moved.extend(group_moved)
        failed.extend(group_failed)
        forget_offline_copies(paths, account, folder, [item[2] for item in group_moved])
    return moved, failed


def _move_uids(paths, account, message_ids, folder, to_folder):
    """
    One move for _move_messages_batch_resolved: over the pooled IMAP
    connection first, which is already open and logged in, and through
    the Himalaya subprocess only when that cannot serve it. Raises
    HimalayaError exactly as move_messages does, because the fallback
    is move_messages.

    Live, 24 September 2026: every Himalaya move needed a fresh DNS
    lookup, and Windows kept failing it ("No such host is known"), so
    batch deletes failed after 12 s while the pooled connection was
    working throughout.
    """
    try:
        imap_body_fetch.move_uids(paths, account, message_ids, folder, to_folder)
    except imap_body_fetch.ImapBodyFetchUnavailable as exc:
        logger.debug(
            "Pooled move from %s to %s unavailable (%s); using the Himalaya subprocess instead.",
            folder, to_folder, exc,
        )
    except Exception:  # noqa: BLE001 -- the subprocess is still correct
        logger.debug(
            "Pooled move from %s to %s failed unexpectedly; using the Himalaya subprocess instead.",
            folder, to_folder, exc_info=True,
        )
    else:
        logger.debug(
            "Moved %d message(s) from %s to %s over the pooled connection.",
            len(message_ids), folder, to_folder,
        )
        return
    move_messages(paths, account, message_ids, folder, to_folder)


def _uid_of(item):
    """An item's message_id as an int UID, or None if it is not one."""
    try:
        return int(item[0])
    except (TypeError, ValueError):
        return None


def _pause_before_recheck(seconds=2.0):
    """The wait between move_messages_batch's two reads of a source
    folder. Its own function so tests replace it instead of sleeping."""
    import time as _time
    _time.sleep(seconds)


def trash_messages_batch(paths, account, items, trash_folder):
    """
    Step one of a batched Delete or Shift+Delete: moves `items` to
    Trash through move_messages_batch.

    `items` is an iterable of (message_id, folder, message_id_header)
    for one account, none of them already in Trash.

    Returns (pre_max, moved, failed). pre_max is Trash's highest UID
    read before anything moved, 0 for an empty Trash; step two needs
    it. moved and failed are as move_messages_batch returns them.

    Raises HimalayaError, before moving anything, if Trash's UIDs
    cannot be read -- without pre_max, step two could not tell this
    batch's copies from older ones.
    """
    items = [item for item in items if item[0] is not None]
    if not items:
        return 0, [], []
    pre_max = max(folder_uids(paths, account, trash_folder), default=0)
    moved, failed = move_messages_batch(paths, account, items, trash_folder)
    return pre_max, moved, failed


def purge_moved_from_trash(paths, account, moved, trash_folder, pre_max):
    """
    Step two of Shift+Delete: permanently removes, from Trash, only
    the copies step one just put there. Nothing else in Trash is ever
    touched, so anything deleted earlier stays recoverable.

    A Trash message qualifies only if its UID is above pre_max AND
    its Message-ID (canonical form) belongs to the batch. A message
    selected twice -- the same mail in two folders -- takes one
    qualifying copy per occurrence. Trash is read newest first, batch
    size plus 50, once more at 2000 if that came up short; a single
    batch far beyond that many messages may leave some behind in
    Trash as not_found.

    One 'message delete' per chunk. On a server with UIDPLUS that
    sets \\Deleted and expunges exactly those UIDs. A report saying
    they were only flagged means no UIDPLUS: nothing is expunged here,
    and those messages are reported as flagged_only.

    Returns a list of (message_id, folder, reason) with reason
    "no_header", "not_found", "purge_failed" or "flagged_only". Every
    one of those messages is in Trash, not gone.
    """
    unresolved = []
    wanted = {}
    for item in moved:
        header = _canonical_message_id(item[2])
        if not header:
            unresolved.append((item[0], item[1], "no_header"))
            continue
        wanted.setdefault(header, []).append(item)
    if not wanted:
        return unresolved

    needed = sum(len(group) for group in wanted.values())
    candidates = {}
    for page_size in (min(needed + 50, 2000), 2000):
        envelopes = list_envelopes(
            paths, account, folder=trash_folder, page_size=page_size, backend="imap",
        )
        candidates = {}
        for envelope in envelopes:
            try:
                uid = int(envelope.get("id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if uid <= pre_max:
                continue
            header = _envelope_message_id(envelope)
            if header in wanted:
                candidates.setdefault(header, set()).add(uid)
        found = sum(
            min(len(candidates.get(header, ())), len(group))
            for header, group in wanted.items()
        )
        if found >= needed or page_size >= 2000:
            break

    matched = []
    for header, group in wanted.items():
        uids = sorted(candidates.get(header, ()))
        for index, item in enumerate(group):
            if index < len(uids):
                matched.append((uids[index], item))
            else:
                unresolved.append((item[0], item[1], "not_found"))

    account_label = getattr(account, "account_id", account)
    for chunk in _chunks(matched):
        chunk_ids = [str(uid) for uid, _item in chunk]
        try:
            report = _run_with_folder_flag(
                paths, account, ["message", "delete", *chunk_ids],
                trash_folder, flags=("-m", "--folder"), timeout=300,
            )
        except HimalayaError as exc:
            logger.warning(
                "Permanent delete of %d message(s) from %s failed on %s: %s",
                len(chunk), trash_folder, account_label, exc,
            )
            unresolved.extend((item[0], item[1], "purge_failed") for _uid, item in chunk)
            continue
        for uid in chunk_ids:
            _invalidate_cached_message(paths, account, trash_folder, uid)
        if _delete_left_flagged(report):
            unresolved.extend((item[0], item[1], "flagged_only") for _uid, item in chunk)
            continue
        forget_offline_copies(paths, account, trash_folder, [item[2] for _uid, item in chunk])
    return unresolved


def permanently_delete_messages(paths, account, items, trash_folder):
    """
    Permanently deletes a selection from one account -- the engine
    behind Shift+Delete, singular or batched.

    `items` is an iterable of (message_id, folder, message_id_header)
    for messages all belonging to `account`; group a mixed-account
    selection by account first.

    Messages already in Trash get one batched 'message delete'.
    Everything else goes through trash_messages_batch and then
    purge_moved_from_trash -- see the section comment above for why
    the Trash match is by UID high-water mark and not by Message-ID
    alone.

    Returns a list of (message_id, folder, reason) for messages not
    confirmed gone:

      "move_failed"   never reached Trash; still in its own folder.
      "delete_failed" was already in Trash; the delete failed.
      "no_header", "not_found", "purge_failed", "flagged_only"
                      reached Trash but was not removed from it.

    Nothing is raised for a partial failure, so one unresolved
    message never stops the rest of the batch.
    """
    items = [item for item in items if item[0] is not None]
    items = resolve_live_items(paths, account, items)
    unresolved = []
    in_trash = [item for item in items if _same_folder(item[1], trash_folder)]
    elsewhere = [item for item in items if not _same_folder(item[1], trash_folder)]
    account_label = getattr(account, "account_id", account)

    for chunk in _chunks(in_trash):
        try:
            report = _run_with_folder_flag(
                paths, account,
                ["message", "delete", *[str(item[0]) for item in chunk]],
                trash_folder, flags=("-m", "--folder"), timeout=300,
            )
        except HimalayaError as exc:
            logger.warning(
                "Permanent delete of %d message(s) already in %s failed on %s: %s",
                len(chunk), trash_folder, account_label, exc,
            )
            unresolved.extend((item[0], item[1], "delete_failed") for item in chunk)
            continue
        for item in chunk:
            _invalidate_cached_message(paths, account, item[1], item[0])
        if _delete_left_flagged(report):
            unresolved.extend((item[0], item[1], "flagged_only") for item in chunk)
            continue
        forget_offline_copies(paths, account, trash_folder, [item[2] for item in chunk])

    if elsewhere:
        try:
            pre_max, moved, failed = trash_messages_batch(paths, account, elsewhere, trash_folder)
        except HimalayaError as exc:
            logger.warning(
                "Permanent delete could not start on %s, nothing was moved: %s",
                account_label, exc,
            )
            unresolved.extend((item[0], item[1], "move_failed") for item in elsewhere)
            return unresolved
        unresolved.extend((item[0], item[1], "move_failed") for item in failed)
        unresolved.extend(purge_moved_from_trash(paths, account, moved, trash_folder, pre_max))

    return unresolved


def permanently_delete_message(paths, account, message_id, folder, trash_folder, message_id_header=None):
    """
    True permanent delete of a single message, regardless of which
    folder it's currently in -- the fix for the broken 'expunge'
    fallback that used to live in delete_message() (see its
    docstring for the full story, and docs/decisions.md for more).
    Himalaya has no single command that skips Trash unconditionally,
    only the trash-first policy 'message delete' documents (move
    once, delete for real the second time once already in Trash), so
    an unconditional permanent delete means driving that policy
    through both of its steps -- see permanently_delete_messages()
    above for exactly how, and for the batched form that a
    multi-message caller should use instead of calling this in a
    loop.

    message_id_header should be the envelope's own "message-id"
    field -- already available to every caller from the envelope list
    it came from, so this never needs an extra fetch just to read
    that header back. Without one, the message still ends up moved to
    Trash (harmless -- that's what a plain Delete already does) but
    this raises HimalayaError instead of guessing at a second id, so
    the caller never reports "permanently deleted" for a message that
    only made it as far as Trash.
    """
    unresolved = permanently_delete_messages(
        paths, account, [(message_id, folder, message_id_header)], trash_folder,
    )
    if not unresolved:
        return
    _, _, reason = unresolved[0]
    if reason == "no_header":
        raise HimalayaError(
            "Message moved to Trash, but permanent deletion could not be "
            "confirmed (no Message-ID header was available to re-find it "
            "there). Delete it again from Trash to finish."
        )
    if reason == "move_failed":
        raise HimalayaError(
            "The message could not be moved to Trash, so it was not "
            "deleted. It is still in its original folder."
        )
    if reason == "flagged_only":
        raise HimalayaError(
            "The server marked the message as deleted but did not remove "
            "it. It is still in Trash."
        )
    if reason in ("delete_failed", "purge_failed"):
        raise HimalayaError(
            "Permanent deletion failed. The message is still in Trash."
        )
    raise HimalayaError(
        "Message moved to Trash, but could not be found there to finish "
        "permanent deletion. Delete it again from Trash to finish."
    )


# --- Logging: what may and may not be written to disk ----------------
#
# The debug log used to contain an 8,000 character preview of every
# Himalaya response, and data/logs/ is a plain folder that any backup or
# sync client picks up. For a message read that preview is the mail;
# for an envelope listing it is every subject, sender and address in
# a folder. Both are the mail. The log's diagnostic value is in the
# shape (which keys came back, wrapped in an object or a bare list),
# the ids, the flags, the folder names, the timings and which domain
# a call went to -- none of which needs a person's name in it.

# Headers whose content is the mail rather than plumbing.
_SENSITIVE_HEADERS = frozenset((
    "subject", "from", "to", "cc", "bcc", "reply_to", "return_path",
    "delivered_to", "message_id", "in_reply_to", "references",
))


def _mask_address(value):
    """Keeps the domain, drops who. "sender@example.com" becomes
    "***@gmail.com": enough to tell which provider a call went to,
    nothing that identifies a person. Message-ids get the same
    treatment -- they routinely embed an address, and Google Voice's
    embed a phone number."""
    text = str(value)
    if "@" not in text:
        return "<redacted>"
    domain = text.rsplit("@", 1)[1]
    return "***@" + domain


def _redact_sensitive(value, counter, parent_key=None, parent=None):
    """Walks a parsed Himalaya response removing the mail from it.

    Bodies were the obvious half. The half that got missed first time
    is an envelope listing, which has no body at all -- so redacting
    only bodies left every subject, sender name, address and
    message-id of a whole folder sitting in the log in clear text.
    Metadata is still the mail.

    What survives is what actually diagnoses a problem: ids, flags,
    dates, sizes, folder names, the response's shape, and the domain
    each address belongs to.

    One shape trap, learned the hard way: header values wear the same
    {"Text": ...} tag as bodies, so a body must be recognised by its
    parent key being "body", not by the tag alone. And "name" is both
    a header's name and a person's display name -- only the latter,
    identified by sitting beside an "email" or "address", is redacted.

    Nothing message-shaped survives in any form. A body is the words
    "text block" with no length, because a length is a fingerprint. A
    subject is a number, so two log lines can still be recognised as
    the same message without the log ever carrying its subject. An
    attachment is a number too, never its filename, which is
    sender-supplied text and routinely personal; the content type
    stays, so a PDF, an embedded image and a tracking pixel are still
    told apart.

    counter carries the running totals: [0] every redaction made, [1]
    subjects seen, [2] attachments seen. It is extended in place so
    an older caller passing [0] still works.
    """
    while len(counter) < 3:
        counter.append(0)
    if isinstance(value, dict):
        out = {}
        is_address_entry = "email" in value or "address" in value

        # A parsed header entry: {"name": <header name>, "value":
        # <tagged value>}. The sensitive ones carry the same content
        # as the envelope fields above and have to go the same way,
        # or a message read leaks what an envelope listing no longer
        # does.
        header_name = value.get("name") if "value" in value else None
        if isinstance(header_name, dict):
            header_name = header_name.get("other")
        sensitive_header = (
            isinstance(header_name, str)
            and header_name.lower().replace("-", "_") in _SENSITIVE_HEADERS
        )

        for key, item in value.items():
            if sensitive_header and key == "value":
                counter[0] += 1
                if header_name.lower() == "subject":
                    counter[1] += 1
                    out[key] = "<subject %d>" % counter[1]
                else:
                    out[key] = "<%s redacted>" % header_name.lower()
                continue
            if parent_key == "body" and key in ("Text", "Html") and isinstance(item, str):
                counter[0] += 1
                out[key] = "<text block>"
            elif key == "subject" and isinstance(item, str):
                counter[0] += 1
                counter[1] += 1
                out[key] = "<subject %d>" % counter[1]
            elif key == "filename" and isinstance(item, str):
                counter[0] += 1
                counter[2] += 1
                out[key] = "<attachment %d>" % counter[2]
            elif key in ("email", "address") and isinstance(item, str):
                counter[0] += 1
                out[key] = _mask_address(item)
            elif key == "name" and is_address_entry and isinstance(item, str):
                counter[0] += 1
                out[key] = "<name redacted>"
            elif key in ("message-id", "message_id") and isinstance(item, str):
                counter[0] += 1
                out[key] = _mask_address(item)
            elif key == "in-reply-to" and isinstance(item, list):
                counter[0] += len(item)
                out[key] = [_mask_address(entry) for entry in item]
            else:
                out[key] = _redact_sensitive(item, counter, key, value)
        return out
    if isinstance(value, list):
        return [_redact_sensitive(item, counter, parent_key, parent) for item in value]
    return value


def _response_preview(stdout, limit=1200):
    """A log-safe view of a Himalaya response.

    Falls back to a length note rather than the raw text if the output
    cannot be parsed: unparseable output is exactly when a body might
    be sitting in it unrecognised.

    The limit was 8000, chosen when the deeply-nested 'message read'
    shape had to be read out of the log to be understood at all. That
    shape is now known and handled (message_body.py), and the cost
    had become the real problem: one 40-second session produced a
    2.7 MB log, almost all of it envelope and message JSON, which
    makes finding the handful of lines that explain a bug genuinely
    hard. Enough to identify a response and see its top-level keys is
    what is actually needed day to day; Tools > Capture Next Message
    Read still writes one complete untruncated response when a full
    one is really wanted.
    """
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return "<unparseable response, %d chars, not logged>" % len(stdout or "")

    counter = [0]
    safe = _redact_sensitive(parsed, counter)
    try:
        text = json.dumps(safe, ensure_ascii=False)
    except (TypeError, ValueError):
        return "<response could not be re-serialised for logging>"

    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    if counter[0]:
        text += " [%d sensitive field(s) redacted]" % counter[0]
    return text


def download_attachments(paths, account, message_id, dest_dir, folder="INBOX"):
    """Confirmed syntax: 'himalaya attachment download <id> --dir <path> -m <folder>'."""
    _run(
        paths, account,
        ["attachment", "download", str(message_id), "--dir", dest_dir, "-m", folder],
        timeout=60,
        backend="imap",
    )


def send_message_raw(paths, account, raw_message_text):
    """
    Sends a raw RFC822 message by piping it to Himalaya's stdin, the
    pattern used consistently across Himalaya's own docs and every
    real usage example found (e.g. 'cat message.eml | himalaya
    message send', 'himalaya template send < message.txt'). A bare
    file-path argument to 'message send' was tried first and
    rejected by the CLI's own usage line, which only accepts a
    message after a literal '--' as a variadic positional, not a
    plain path; piping sidesteps that entirely.
    """
    _run(paths, account, ["message", "send"], timeout=60, input_text=raw_message_text, backend="smtp")
    _ensure_sent_copy(paths, account, raw_message_text)


def _ensure_sent_copy(paths, account, raw_message_text):
    """
    Makes sure a sent message has a copy in the account's Sent folder.

    message.send.save-copy = true is set for every account, yet a
    disroot send on 16 September 2026 left no Sent copy. The send runs
    with -b smtp, and Gmail and Microsoft save a copy server-side on
    SMTP anyway, which is why only a plain IMAP provider showed it.

    So: look for the Message-ID in Sent, once more after the usual
    short pause, and APPEND the message as seen only when it is still
    missing -- a copy saved by the server or by Himalaya is never
    duplicated. If Sent cannot be checked, nothing is added: a missing
    copy is better than a doubled one. Never raises; the send itself
    has already succeeded.
    """
    import email.parser
    import provider_presets

    if not getattr(account, "imap_host", None):
        return
    try:
        header = email.parser.HeaderParser().parsestr(raw_message_text).get("Message-ID")
    except Exception:  # noqa: BLE001 - an unparseable header means no check
        header = None
    if not header:
        return
    header = header.strip()
    folder = provider_presets.himalaya_folder_name(account.imap_host, "Sent")
    try:
        for attempt in (1, 2):
            if imap_body_fetch.sent_copy_exists(paths, account, folder, header):
                return
            if attempt == 1:
                _pause_before_recheck()
    except imap_body_fetch.ImapBodyFetchUnavailable as exc:
        logger.warning(
            "Could not check %s for the sent copy on %s: %s. Not adding one.",
            folder, account.account_id, exc,
        )
        return
    try:
        add_message_raw(paths, account, folder, raw_message_text, flags=("seen",))
        logger.info(
            "Saved the sent copy to %s on %s; neither the server nor Himalaya had.",
            folder, account.account_id,
        )
    except HimalayaError as exc:
        logger.warning(
            "Could not save the sent copy to %s on %s: %s", folder, account.account_id, exc,
        )


def add_message_raw(paths, account, folder, raw_message_text, flags=("draft",)):
    """
    Appends a raw RFC822 message to `folder` (APPEND, RFC 3501) via
    Himalaya's 'message add' -- used for saving drafts (audit finding
    40). Piped via stdin, the same pattern as send_message_raw above,
    rather than as a positional/inline argument -- keeps a large
    draft off the command line and out of process-list visibility.

    Confirmed live against the pinned himalaya v2.1.0 build (the
    matching x86_64-linux release downloaded into a throwaway
    sandbox and run against a scratch maildir account, since the
    vendored himalaya/himalaya.exe is a Windows binary this shell can't
    execute -- same constraint noted for finding 39's search DSL):

      himalaya message add --mailbox <folder> -f draft < message.eml

    returns {"id": "<new id>", "sent": false} on success.

    Important: --mailbox does NOT resolve through this app's
    folder.aliases.* config keys (folder.aliases.drafts, etc. --
    the 2.x-style keys write_himalaya_config emits). Live-tested
    with only folder.aliases.drafts set, '--mailbox drafts' looked
    for a literal subfolder named "drafts" and failed; only the
    3.x-style mailbox.alias.<name> key made the alias resolve.
    Rather than also emitting mailbox.alias.drafts, callers here
    pass the real resolved folder name (via
    envelope_format._folder_display_to_himalaya), exactly the
    pattern move_to_trash/permanently_delete_message already use for
    trash_folder.

    Returns the new message's id (confirmed a JSON string on the
    Maildir backend tested live; IMAP APPEND commonly returns a bare
    UID, so this may come back as an int there instead -- callers
    should treat it as opaque either way, same as every other id in
    this module, and pass it back only into another Himalaya call for
    the same account/folder/backend (e.g. permanently_delete_message,
    to purge a superseded draft revision)).
    """
    args = ["message", "add", "--mailbox", folder]
    for flag in flags:
        args += ["-f", flag]
    result = _run(paths, account, args, timeout=30, input_text=raw_message_text, backend="imap")
    if isinstance(result, dict) and "id" in result:
        return result["id"]
    raise HimalayaError("Himalaya did not return an id for the added draft.")


# --- Offline (Maildir) support ------------------------------------------
#
# An account configured for offline reading has maildir.root set in
# its TOML section (see AccountManager.write_himalaya_config)
# alongside its normal imap/smtp config. Syncing copies messages one
# way, IMAP -> local Maildir; nothing ever flows back, so the local
# copy is read-only from Himalaya's point of view and can't drift the
# real mailbox. list_envelopes/read_message above already accept
# backend="maildir" to read the synced copy once it exists.

def ensure_maildir_folder(paths, account, folder):
    """
    Creates the local Maildir folder if it doesn't exist yet.
    Confirmed via live test against the real binary: re-running this
    on a folder that already exists succeeds (exit 0) rather than
    erroring, so every sync can call this unconditionally.
    """
    _run(paths, account, ["maildir", "create", folder], backend="maildir", timeout=15)


def clear_offline_caches(paths):
    """Removes every profile's local mail cache.

    Two directories per account profile, and only those two:
    'offline_mail', the Maildir copy the offline view and the message
    list read, and 'message_cache', the per-message JSON bodies.
    Nothing else under a profile is touched.

    Nothing on any server is affected. Everything removed here is a
    copy, re-fetched on demand or on the next sync -- which is the
    whole reason this is safe to offer as a one-keystroke action.

    Walks the cache root's account directories rather than the
    configured account list, so a directory left behind by an account
    that was removed is cleared too instead of sitting on disk
    forever. Only the cache root is ever enumerated -- userdata,
    which holds contacts, blocklists and per-account thread state, is
    never touched by this.

    Returns (files_removed, bytes_removed).
    """
    files_removed = 0
    bytes_removed = 0
    root = getattr(paths, "cache", None)
    if not root or not os.path.isdir(root):
        return files_removed, bytes_removed

    for entry in os.listdir(root):
        profile = os.path.join(root, entry)
        if not os.path.isdir(profile):
            continue
        for name in ("offline_mail", "message_cache"):
            target = os.path.join(profile, name)
            if not os.path.isdir(target):
                continue
            # Bottom-up so a directory is only removed once its own
            # contents have gone.
            for current, _dirs, filenames in os.walk(target, topdown=False):
                for filename in filenames:
                    path = os.path.join(current, filename)
                    try:
                        bytes_removed += os.path.getsize(path)
                    except OSError:
                        pass
                    try:
                        os.remove(path)
                        files_removed += 1
                    except OSError as exc:
                        logger.debug("Could not remove %s: %s", path, exc)
                try:
                    os.rmdir(current)
                except OSError:
                    # Still holding something, or open elsewhere. The
                    # files are what matter; an empty directory left
                    # behind is harmless and is reused on next sync.
                    pass

    logger.info(
        "Cleared offline caches: %d file(s), %d byte(s).",
        files_removed, bytes_removed,
    )
    return files_removed, bytes_removed


def remove_account_cache(paths, account_id):
    """Deletes one account's whole cache directory.

    Everything under cache\\<account_id> is a copy: the offline Maildir
    and the per-message JSON bodies. Nothing on any server is
    affected. Called when an account is removed, so its downloaded
    mail does not sit on disk forever after the account it belonged to
    is gone -- an orphaned directory held 52 files for a whole session
    before this existed.

    Returns (files_removed, bytes_removed). A directory that is not
    there is not an error.
    """
    return _remove_account_tree(getattr(paths, "cache", None), account_id, "cache")


def remove_account_profile(paths, account_id):
    """Deletes one account's whole userdata directory.

    Today that holds thread_state.json and nothing else -- which
    threads were expanded, for an account that no longer exists.

    Deliberately separate from remove_account_cache above rather than
    one call covering both roots. cache and userdata mean different
    things, and a single function reaching into userdata because it
    was convenient is how the two get conflated again.

    Returns (files_removed, bytes_removed).
    """
    return _remove_account_tree(getattr(paths, "userdata", None), account_id, "userdata")


def _remove_account_tree(root, account_id, label):
    """Removes root\\<account_id> entirely. Shared by the two callers
    above; not called directly from anywhere else."""
    files_removed = 0
    bytes_removed = 0
    if not root or not account_id:
        return files_removed, bytes_removed

    target = os.path.join(root, str(account_id))
    if not os.path.isdir(target):
        return files_removed, bytes_removed

    # Bottom-up so a directory is only removed once its own contents
    # have gone -- same shape as clear_offline_caches above.
    for current, _dirs, filenames in os.walk(target, topdown=False):
        for filename in filenames:
            path = os.path.join(current, filename)
            try:
                bytes_removed += os.path.getsize(path)
            except OSError:
                pass
            try:
                os.remove(path)
                files_removed += 1
            except OSError as exc:
                logger.debug("Could not remove %s: %s", path, exc)
        try:
            os.rmdir(current)
        except OSError:
            pass

    logger.info(
        "Removed %s directory for account %s: %d file(s), %d byte(s).",
        label, account_id, files_removed, bytes_removed,
    )
    return files_removed, bytes_removed


# How the local maildir is scanned when working out what is already
# stored. Read in pages because list_envelopes pages, and a folder
# that outgrows one page must still be checked to the bottom -- see
# sync_folder_offline. The page cap is a safety net against a backend
# that ignores --page and hands back the same page forever, not an
# expected limit: 20 pages of 500 is 10,000 messages.
_MAILDIR_SCAN_PAGE_SIZE = 500
_MAILDIR_SCAN_MAX_PAGES = 20


def _canonical_message_id(value):
    """
    One canonical form for an RFC5322 Message-ID, so that two
    spellings of the same message compare equal.

    The two sides of sync_folder_offline's dedup check come from
    different producers -- Himalaya's own JSON for the local Maildir,
    imap_body_fetch's dict for the live account -- and nothing makes
    them agree on angle brackets, surrounding whitespace or case.
    Measured on real stores 10 September 2026: five copies of every
    message in both accounts, at seven files and at a hundred and
    fifty alike, which is what a comparison that never matches looks
    like rather than a window that is too small.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if not text:
        return None
    return text.casefold()


def _envelope_message_id(envelope):
    """
    The canonical Message-ID of an envelope dict, under either
    spelling. _redact_sensitive already checks both "message-id" and
    "message_id", which is the evidence that both occur in practice.
    """
    if not isinstance(envelope, dict):
        return None
    for key in ("message-id", "message_id"):
        found = _canonical_message_id(envelope.get(key))
        if found:
            return found
    return None


def _header_fields(raw_text, names):
    """
    The first value of each header in `names` (lower case) from a raw
    RFC822 header block, with folded continuation lines joined back
    on. Stops at the blank line that ends the headers.

    Folding matters: live 24 September 2026, Outlook wrote
    "Message-ID:" with the ID itself on the next, indented line. Read
    without unfolding, the stored copy had no Message-ID, never
    matched the server's, and was copied again on every sync pass --
    eight copies of one message.
    """
    found = {}
    current = None
    for line in (raw_text or "").replace("\r", "").split("\n"):
        if not line.strip():
            break
        if line[:1] in (" ", "\t"):
            if current is not None:
                found[current] = (found[current] + " " + line.strip()).strip()
            continue
        current = None
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip().lower()
        if key in names and key not in found:
            found[key] = value.strip()
            current = key
    return found


def _message_id_from_raw(raw_text):
    """
    The canonical Message-ID read out of a raw RFC822 message's own
    header block, folded continuation lines included (_header_fields).

    Carriage returns are stripped before splitting rather than relying
    on splitlines(). Measured 10 September 2026: every message in the
    offline cache is stored with doubled carriage returns, CR CR LF
    where there should be CR LF, so splitlines() saw a blank line
    between every real one and the header block appeared to end after
    a single line -- 210 stored files yielded nothing at all.
    """
    if not raw_text:
        return None
    return _canonical_message_id(_header_fields(raw_text, ("message-id",)).get("message-id"))


def _dedup_key_from_raw(raw_text):
    """
    The offline-sync dedup key for one raw message: its real,
    canonical Message-ID when it has one (_message_id_from_raw), or --
    when that comes back None because the header is missing, or
    present but its value reduces to nothing once normalized (some
    automated senders emit a literal empty "<>") -- a deterministic
    fallback built from From/Date/Subject instead.

    Without this, a message with a genuinely empty Message-ID can
    never be recognised as "already stored": _stored_message_ids has
    nothing to match it against, so sync_folder_offline re-copies it
    on every single pass, forever, unboundedly. Confirmed live 11
    September 2026 (open_issues V32): one such message reached 60
    copies in one account's Inbox, out of 147 files total, before
    this was diagnosed -- the header line was found exactly where
    expected, not folded, not skipped; only its value canonicalized
    to nothing.

    The fallback is prefixed ("fallback:") so it can never collide
    with a real Message-ID's own casefolded text, and is built only
    from headers that describe the message ITSELF -- who sent it,
    when, what it was about -- never anything the filesystem assigns
    (a Maildir filename, a UID), which would produce a different key
    on every sync and defeat the point entirely. Both callers
    (_stored_message_ids reading already-stored files, and
    sync_folder_offline's own backstop reading a freshly-fetched raw
    message) parse the same raw RFC822 shape through this one
    function, which is what keeps the two sides comparable.

    Returns None only when none of From/Date/Subject could be found
    either -- the true worst case, nothing stable left to key on.
    """
    real_id = _message_id_from_raw(raw_text)
    if real_id:
        return real_id

    headers = _header_fields(raw_text, ("from", "date", "subject"))

    parts = [headers.get(name, "") for name in ("from", "date", "subject")]
    if not any(parts):
        return None
    digest = hashlib.sha256(
        "|".join(parts).casefold().encode("utf-8", "replace")
    ).hexdigest()
    return "fallback:" + digest


def _maildir_folder_dir(paths, account, folder):
    """
    Locates a synced folder's Maildir directory on disk, or None.

    Same layout clear_offline_caches walks: the cache root, a
    directory named for the account id, then 'offline_mail'. The
    folder's own directory name is not something this module writes --
    Himalaya's maildir backend chooses it -- so the plain name, the
    dot-prefixed Maildir++ form and a case-insensitive match are all
    tried before giving up. Case matters here: V22 records the IDLE
    watcher saying INBOX where the message list says Inbox.

    Returns None rather than guessing, so the caller can fall back to
    asking Himalaya instead of silently treating an unfound folder as
    an empty one -- which would re-copy everything in it.
    """
    root = getattr(paths, "cache", None)
    account_id = getattr(account, "account_id", None)
    if not root or not account_id:
        return None
    base = os.path.join(root, str(account_id), "offline_mail")
    if not os.path.isdir(base):
        return None

    wanted = {folder.casefold(), ("." + folder).casefold()}
    candidates = [folder, "." + folder]
    try:
        for entry in os.listdir(base):
            if entry.casefold() in wanted:
                candidates.append(entry)
    except OSError as exc:
        logger.debug("Could not list %s: %s", base, exc)

    for name in candidates:
        candidate = os.path.join(base, name)
        if os.path.isdir(os.path.join(candidate, "cur")):
            return candidate
    return None


def _remove_duplicate_copies(maildir_dir):
    """
    Deletes extra files in a synced Maildir folder that share a dedup
    key (_dedup_key_from_raw), keeping the oldest by file name. Only
    the offline copy, which is a cache. Cleans up copies an earlier
    sync made before _header_fields unfolded Message-ID. Returns how
    many files were removed.
    """
    seen = {}
    removed = 0
    paths_found = []
    for sub in ("cur", "new"):
        directory = os.path.join(maildir_dir, sub)
        if not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError as exc:
            logger.debug("Could not list %s: %s", directory, exc)
            continue
        paths_found.extend(os.path.join(directory, name) for name in names)
    for path in sorted(paths_found, key=os.path.basename):
        try:
            with open(path, "rb") as handle:
                head = handle.read(65536)
        except OSError:
            continue
        key = _dedup_key_from_raw(head.decode("latin-1", "replace"))
        if not key:
            continue
        if key not in seen:
            seen[key] = path
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as exc:
            logger.debug("Could not remove duplicate %s: %s", path, exc)
    if removed:
        logger.info("Removed %d duplicate offline cop(ies) from %s.", removed, maildir_dir)
    return removed


def _stored_message_ids(maildir_dir):
    """
    Every message's dedup key already stored in a Maildir folder, read
    from the message files themselves -- a real Message-ID where one
    exists, or the From/Date/Subject fallback key (_dedup_key_from_raw)
    for a message whose Message-ID is missing or canonicalizes to
    nothing, so that class of message can be recognised as "already
    stored" too instead of re-syncing forever (open_issues V32).

    Deliberately not a Himalaya call. The files are on local disk, the
    header is the exact thing being matched, and reading it directly
    removes both the subprocess and any question about what the JSON
    happens to call the field. Only each file's header block is read.
    """
    stored = set()
    for sub in ("cur", "new"):
        directory = os.path.join(maildir_dir, sub)
        if not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError as exc:
            logger.debug("Could not list %s: %s", directory, exc)
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                with open(path, "rb") as handle:
                    head = handle.read(65536)
            except OSError as exc:
                logger.debug("Could not read %s: %s", path, exc)
                continue
            found = _dedup_key_from_raw(head.decode("latin-1", "replace"))
            if found:
                stored.add(found)
    return stored


def sync_folder_offline(paths, account, folder, limit=100, progress_cb=None, max_age_days=None):
    """
    Pulls up to 'limit' most recent messages in 'folder' from the
    live IMAP account down into the local Maildir copy, preserving
    the seen/flagged/answered state each message currently has on
    the server. Runs entirely synchronously (blocking network and
    disk I/O for every message); callers run this via
    HimalayaWorker.submit, never on the UI thread -- and specifically
    via a dedicated low-priority worker, not the same one interactive
    actions (opening a folder, reading a message, sending) queue on,
    since a first-time sync of 100 messages can take minutes against
    a slow account and must never make the rest of the app wait
    behind it.

    Incremental: a message already present in the local Maildir copy
    is skipped with no network call at all, matched by the RFC5322
    Message-ID header rather than Himalaya's own per-backend id --
    confirmed via live capture that a message's Message-ID header is
    identical between its IMAP and Maildir copies, while Himalaya
    assigns a brand new Maildir-internal id on every 'message add',
    so the id itself never matches across backends. Without this
    check, every periodic top-up re-downloaded the full body of all
    'limit' messages from scratch every single time it ran.

    max_age_days, when given, skips any message whose own date is
    older than that, so the offline copy is not refilled with what
    the daily Cache Clean Up just removed.

    progress_cb, if given, is called as progress_cb(done, total)
    after each message (skipped or copied) so a caller can update a
    status message. Returns the count of messages actually copied
    (not counting ones already present and skipped).
    """
    ensure_maildir_folder(paths, account, folder)

    # What is already stored, read from the maildir files themselves.
    #
    # Not a Himalaya call. The files are on local disk and the RFC5322
    # Message-ID header is the exact thing being matched, so reading
    # the header block directly removes both the subprocess and any
    # question about what the JSON happens to call the field -- which
    # mattered, because the two sides of this comparison come from
    # different producers and nothing made them agree.
    #
    # This lookup is the only thing standing between a periodic sync
    # and a second copy of every message it re-examines. Measured on
    # real stores 10 September 2026: five copies of every message in
    # both accounts, at seven files and at a hundred and fifty alike,
    # which is what a comparison that never matches looks like rather
    # than a window that is too small.
    #
    # The paged listing in the fallback branch is for the case where
    # the folder's own directory cannot be located. It walks every
    # page rather than one: it used to ask for page_size=limit, so a
    # maildir holding more than a hundred messages was only ever
    # checked one page deep and everything below the line looked new.
    maildir_dir = _maildir_folder_dir(paths, account, folder)
    if maildir_dir is not None:
        _remove_duplicate_copies(maildir_dir)
        already_synced = _stored_message_ids(maildir_dir)
        logger.debug(
            "Offline sync of %s: %d message(s) already stored locally.",
            folder, len(already_synced),
        )
    else:
        # The folder's own directory could not be located, so fall
        # back to asking Himalaya for the listing, page by page,
        # exactly as before.
        logger.debug(
            "Offline sync of %s: local maildir directory not found; "
            "listing it through Himalaya instead.", folder,
        )
        already_synced = set()
        page = 1
        while True:
            existing = list_envelopes(
                paths, account, folder=folder,
                page_size=_MAILDIR_SCAN_PAGE_SIZE, page=page,
                backend="maildir",
            )
            for entry in existing:
                rfc_id = _envelope_message_id(entry)
                if rfc_id:
                    already_synced.add(rfc_id)
            if len(existing) < _MAILDIR_SCAN_PAGE_SIZE:
                break
            page += 1
            if page > _MAILDIR_SCAN_MAX_PAGES:
                logger.warning(
                    "Local copy of %s is deeper than %d pages; messages past "
                    "that were not checked and may be copied again.",
                    folder, _MAILDIR_SCAN_MAX_PAGES,
                )
                break

    envelopes = list_envelopes(paths, account, folder=folder, page_size=limit, backend="imap")
    envelopes = envelopes[:limit]
    total = len(envelopes)
    copied = 0

    for index, envelope in enumerate(envelopes):
        message_id = envelope.get("id")
        if message_id is None:
            continue

        if _envelope_older_than(envelope, max_age_days):
            if progress_cb is not None:
                progress_cb(index + 1, total)
            continue

        rfc_message_id = _envelope_message_id(envelope)
        if rfc_message_id and rfc_message_id in already_synced:
            if progress_cb is not None:
                progress_cb(index + 1, total)
            continue

        # Through read_message_raw rather than a direct _run: that
        # function holds the pooled-IMAP fast path (imap_body_fetch),
        # which reuses one authenticated connection instead of paying
        # a full IMAP login per message. This loop is the single
        # heaviest user of that path -- a real session's log measured
        # the subprocess version at 1.1 to 1.3 seconds per message
        # with a 22.1 second outlier, essentially all of it handshake
        # rather than fetch. read_message_raw still falls back to the
        # exact subprocess call this used to make whenever the pooled
        # path is unavailable, so the old speed is the floor now, not
        # the norm.
        #
        # Do not "optimise" this into one call carrying several ids.
        # This binary's 'message read' takes exactly one <ID>
        # (confirmed 9 September 2026 against himalaya.exe's own
        # --help, correcting an earlier note that assumed otherwise),
        # and 'message copy' is documented as not crossing backends,
        # so there is no bulk IMAP -> Maildir path to reach for.
        raw_text = read_message_raw(
            paths, account, message_id, folder=folder,
            backend="imap", timeout=30,
        )
        if not raw_text:
            continue

        flags = []
        envelope_flags = envelope.get("flags")
        if isinstance(envelope_flags, list):
            # Confirmed via live capture (matches _envelope_is_unread's
            # note above): each entry is {"raw": "\\Seen", "iana":
            # "seen"}, not a bare flag string.
            iana_names = {
                f.get("iana") for f in envelope_flags
                if isinstance(f, dict) and f.get("iana")
            }
            if "seen" in iana_names:
                flags.append("seen")
            if "flagged" in iana_names:
                flags.append("flagged")
            if "answered" in iana_names:
                flags.append("answered")

        # Backstop. The envelope carried no Message-ID under either
        # spelling, so this message's identity was unknown until its
        # body arrived. Read it from the raw text rather than writing
        # a second copy of something already stored -- and, when even
        # the raw text's own Message-ID canonicalizes to nothing
        # (open_issues V32: a literal empty "<>" some senders emit),
        # fall back to the same deterministic From/Date/Subject key
        # _stored_message_ids used to register that message on disk,
        # so this class of message can be deduplicated too instead of
        # re-syncing forever.
        if not rfc_message_id:
            rfc_message_id = _dedup_key_from_raw(raw_text)
            if rfc_message_id and rfc_message_id in already_synced:
                if progress_cb is not None:
                    progress_cb(index + 1, total)
                continue

        add_args = ["message", "add", "-m", folder]
        for flag in flags:
            add_args += ["-f", flag]
        add_args += ["--"]

        _run(paths, account, add_args, backend="maildir", timeout=30, input_text=raw_text)
        copied += 1
        if rfc_message_id:
            # Within-run guard: a listing that hands back the same
            # message twice must not produce two copies of it.
            already_synced.add(rfc_message_id)

        if progress_cb is not None:
            progress_cb(index + 1, total)

    return copied
