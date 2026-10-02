"""
Delete and Shift+Delete, run in the background until they are done.

Why this exists
---------------
Every earlier delete path ran as one job on the shared interactive
worker, through a fresh Himalaya connection per step. Live, 24
September 2026, that failed in three ways at once: a DNS hiccup made
each step fail after 12 s; a dead Disroot account held the shared
worker for 21 s per call, so a Gmail delete waited 59 s to start; and
a batch of rows still showing the offline copy could not be matched
to the server, so the move never ran and the user was told to delete
from Trash by hand.

What it does instead
--------------------
A delete is a batch saved to data/userdata/pending_deletes.json the
moment it is confirmed. Each account has its own thread, so one
account that cannot connect never delays another, and none of this
touches the interactive worker. Every step runs on the account's
pooled IMAP connection (imap_body_fetch), which stays logged in.

Each message is found by the server itself: a SEARCH on its
Message-ID, or its UID when it has no Message-ID. Nothing depends on
the list's row id, so an offline-copy row deletes like any other.
Every step is safe to repeat, so a batch interrupted by a dropped
connection, or by ZBox closing, is simply run again from the start --
after a restart too.

Delete moves to Trash. Shift+Delete on a normal server marks the
messages deleted and expunges exactly those UIDs where they are. On
Gmail it moves them to Trash first and expunges them there, because
expunging from any other Gmail folder only removes a label.

A batch finishes only when a fresh search shows each message gone.
Network trouble is retried, 5 s rising to 60 s, without a word to the
user. Only a refusal from the server itself, or MAX_ATTEMPTS failed
tries, ends a batch with messages still unresolved.
"""

import json
import logging
import os
import threading
import time
import uuid

import himalaya_client
import imap_body_fetch

logger = logging.getLogger("zbox.deletequeue")

QUEUE_FILE_NAME = "pending_deletes.json"
BACKOFF_SECONDS = (5, 10, 20, 40, 60)
# About half an hour of retries at the 60 s ceiling before a batch is
# reported as failed and its rows shown again.
MAX_ATTEMPTS = 30
# Gmail can take a moment to show a message in [Gmail]/Trash after a
# MOVE; looked for this many times, PAUSE_SECONDS apart.
TRASH_LOOKUPS = 4
PAUSE_SECONDS = 1.5


def is_gmail_folder(folder):
    """True for a Gmail system folder ([Gmail]/... or the German
    [Google Mail]/...), which is how a Gmail account is told apart."""
    text = str(folder or "")
    return text.startswith("[Gmail]/") or text.startswith("[Google Mail]/")


def _as_uid(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _Locator:
    """Finds messages of one folder on the server, by Message-ID when
    they have one and by UID otherwise. The folder's UID list, needed
    only for the second case, is read once and forgotten by reset()."""

    def __init__(self, paths, account, folder):
        self._paths = paths
        self._account = account
        self._folder = folder
        self._uids = None
        # Items found only by their UID because the Message-ID search
        # missed them. Kept across reset(): the Gmail Trash step needs
        # to know which moved items it cannot search for there either.
        self.by_uid = set()

    def reset(self):
        self._uids = None

    def _listed(self, uid):
        if self._uids is None:
            self._uids = set(imap_body_fetch.folder_uids(self._paths, self._account, self._folder))
        return uid in self._uids

    def find(self, item):
        """The item's UIDs in the folder; [] when it is not there;
        None when it cannot be looked for at all (no Message-ID and
        no UID).

        A Message-ID search that finds nothing is not taken as proof
        the message is gone. Live 25 September 2026, Gmail's search
        missed eleven Inbox messages that were plainly listed: nothing
        was moved, every check agreed they were gone, and each
        Shift+Delete hid them only until the next refresh. When the
        row carries its own UID, that UID is checked against the
        folder's listing before the message counts as not there."""
        header = item[2] if len(item) > 2 else None
        uid = _as_uid(item[0])
        if imap_body_fetch.canonical_message_id(header):
            found = imap_body_fetch.find_uids(self._paths, self._account, self._folder, header)
            if found or uid is None:
                return found
            if self._listed(uid):
                logger.debug("Message-ID search missed a listed message in %s; using its UID.", self._folder)
                self.by_uid.add(tuple(item))
                return [uid]
            return []
        if uid is None:
            return None
        return [uid] if self._listed(uid) else []


def _gone_after(locator, items, pause):
    """Splits items into (gone, still_there) after a change, looking a
    second time after a pause at whatever was still listed."""
    locator.reset()
    still = [item for item in items if locator.find(item)]
    if still:
        pause(PAUSE_SECONDS)
        locator.reset()
        still = [item for item in still if locator.find(item)]
    gone = [item for item in items if item not in still]
    return gone, still


def _highest_uid(paths, account, folder):
    """The folder's highest UID, 0 when it is empty."""
    uids = imap_body_fetch.folder_uids(paths, account, folder)
    return max(uids) if uids else 0


def _move_out(paths, account, folder, group, trash_folder, pause):
    """Moves a folder's items to Trash. Returns (gone, failed,
    by_uid): by_uid is the moved items that were found only by their
    UID (see _Locator.find)."""
    locator = _Locator(paths, account, folder)
    located = []
    failed = []
    for item in group:
        uids = locator.find(item)
        if uids is None:
            failed.append(item)
        else:
            located.append((item, uids))
    uids = sorted({uid for _item, found in located for uid in found})
    if uids:
        imap_body_fetch.move_uids(paths, account, uids, folder, trash_folder)
    gone, still = _gone_after(locator, [item for item, _found in located], pause)
    by_uid = [item for item in gone if item in locator.by_uid]
    return gone, failed + still, by_uid


def _delete_in_place(paths, account, folder, group, pause):
    """Permanently removes a folder's items where they are. Returns
    the unresolved entries."""
    locator = _Locator(paths, account, folder)
    located = []
    unresolved = []
    for item in group:
        uids = locator.find(item)
        if uids is None:
            unresolved.append((item[0], folder, "delete_failed"))
        else:
            located.append((item, uids))
    uids = sorted({uid for _item, found in located for uid in found})
    status = "expunged"
    if uids:
        status = imap_body_fetch.delete_uids(paths, account, folder, uids)
    _gone, still = _gone_after(locator, [item for item, _found in located], pause)
    reason = "flagged_only" if status == "flagged_only" else "delete_failed"
    unresolved.extend((item[0], folder, reason) for item in still)
    return unresolved


def _purge_from_trash(paths, account, trash_folder, items, pause, by_uid=(), mark=None):
    """Gmail's second step: expunges from Trash the items just moved
    there. Returns the unresolved entries, all of them in Trash.

    Items in by_uid could not be found by Message-ID in their own
    folder, so they are not searched for in Trash either. Their Trash
    copies are the UIDs above mark (Trash's highest UID read before
    the move) that no searched item accounts for; they are expunged
    only when their number matches exactly, and otherwise reported as
    left in Trash rather than as gone."""
    by_uid = set(by_uid)
    uid_items = [item for item in items if item in by_uid]
    items = [item for item in items if item not in by_uid]
    unresolved = []
    targets = []
    for item in items:
        header = item[2] if len(item) > 2 else None
        if not imap_body_fetch.canonical_message_id(header):
            unresolved.append((item[0], trash_folder, "no_header"))
            continue
        found = []
        for lookup in range(TRASH_LOOKUPS):
            found = imap_body_fetch.find_uids(paths, account, trash_folder, header)
            if found:
                break
            if lookup < TRASH_LOOKUPS - 1:
                pause(PAUSE_SECONDS)
        if found:
            targets.append((item, found))
        else:
            # Not in the source folder and not in Trash: already gone.
            logger.info("A permanently deleted message never showed up in %s; treating it as gone.", trash_folder)
    header_uids = {uid for _item, found in targets for uid in found}
    uid_targets = []
    if uid_items:
        fresh = []
        if mark is not None:
            for lookup in range(TRASH_LOOKUPS):
                fresh = [
                    uid for uid in imap_body_fetch.folder_uids(paths, account, trash_folder)
                    if uid > mark and uid not in header_uids
                ]
                if len(fresh) >= len(uid_items):
                    break
                if lookup < TRASH_LOOKUPS - 1:
                    pause(PAUSE_SECONDS)
        if len(fresh) == len(uid_items):
            uid_targets = fresh
        else:
            logger.info(
                "Could not tell which copies in %s were just moved there; %d left in Trash.",
                trash_folder, len(uid_items),
            )
            unresolved.extend((item[0], trash_folder, "purge_failed") for item in uid_items)
            uid_items = []
    uids = sorted(header_uids | set(uid_targets))
    status = "expunged"
    if uids:
        status = imap_body_fetch.delete_uids(paths, account, trash_folder, uids)
    reason = "flagged_only" if status == "flagged_only" else "purge_failed"
    for item, _found in targets:
        if imap_body_fetch.find_uids(paths, account, trash_folder, item[2]):
            unresolved.append((item[0], trash_folder, reason))
    if uid_targets:
        left = set(imap_body_fetch.folder_uids(paths, account, trash_folder)) & set(uid_targets)
        if left:
            unresolved.extend((item[0], trash_folder, reason) for item in uid_items)
    return unresolved


def process_batch(paths, account, batch, pause=time.sleep):
    """
    Runs one saved batch to the end. Returns (moved, unresolved):
    moved is the items now in Trash after a plain Delete, for Undo;
    unresolved is (message_id, folder, reason) for anything the server
    refused, with the reasons _report_delete already knows.

    Raises ImapBodyFetchUnavailable (not ImapRefused) when the
    connection failed; the caller runs the whole batch again later.
    """
    trash_folder = batch["trash_folder"]
    permanent = bool(batch["permanent"])
    gmail = is_gmail_folder(trash_folder)
    if not gmail:
        # A server with no Trash gets one; a server that keeps its trash
        # under another name (Deleted Items and the like) has it used
        # instead. Silent, never raises; Gmail is left alone.
        himalaya_client.ensure_trash_folder(paths, account)
        import provider_presets

        remembered = provider_presets.remembered_trash_folder(getattr(account, "account_id", None))
        if remembered:
            trash_folder = remembered
    items = [tuple(item) for item in batch["items"]]
    groups = {}
    for item in items:
        groups.setdefault(item[1], []).append(item)

    moved = []
    unresolved = []
    for folder, group in groups.items():
        in_trash = himalaya_client._same_folder(folder, trash_folder)
        in_place = in_trash or (permanent and not gmail)
        try:
            if in_place:
                unresolved.extend(_delete_in_place(paths, account, folder, group, pause))
            else:
                mark = _highest_uid(paths, account, trash_folder) if permanent else None
                gone, failed, by_uid = _move_out(paths, account, folder, group, trash_folder, pause)
                unresolved.extend((item[0], folder, "move_failed") for item in failed)
                if permanent:
                    unresolved.extend(_purge_from_trash(
                        paths, account, trash_folder, gone, pause, by_uid=by_uid, mark=mark,
                    ))
                else:
                    moved.extend(gone)
        except imap_body_fetch.ImapRefused as exc:
            logger.warning(
                "The server refused a delete in %s on %s: %s",
                folder, account.account_id, exc,
            )
            reason = "delete_failed" if in_place else "move_failed"
            unresolved.extend((item[0], folder, reason) for item in group)

    left = {(entry[0], entry[1]) for entry in unresolved if entry[1] != trash_folder}
    for folder, group in groups.items():
        done = [item[2] for item in group if (item[0], folder) not in left and item[2]]
        if not done:
            continue
        try:
            himalaya_client.forget_offline_copies(paths, account, folder, done)
        except Exception:  # noqa: BLE001 - the offline copy catches up on its next sync
            logger.debug("Could not drop offline copies in %s.", folder, exc_info=True)
    return moved, unresolved


class DeleteQueue:
    """
    The saved batches and one worker thread per account.

    account_for(account_id) returns the live Account or None.
    on_done(batch_id, account_id, items, moved, unresolved) is handed
    to dispatch, which the frame sets to wx.CallAfter, so it always
    runs on the main thread.
    """

    def __init__(self, paths, account_for, on_done, dispatch, pause=time.sleep):
        self._paths = paths
        self._file = os.path.join(paths.userdata, QUEUE_FILE_NAME)
        self._account_for = account_for
        self._on_done = on_done
        self._dispatch = dispatch
        self._pause = pause
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._threads = {}
        self._wake = {}
        self._batches = self._load()

    # -- persistence -------------------------------------------------

    def _load(self):
        try:
            with open(self._file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.warning("Could not read %s; starting with no pending deletes.", self._file, exc_info=True)
            return []
        batches = []
        for batch in data if isinstance(data, list) else []:
            if isinstance(batch, dict) and batch.get("id") and batch.get("account_id") and batch.get("items"):
                batch.setdefault("attempts", 0)
                batches.append(batch)
        if batches:
            logger.info("%d pending delete batch(es) carried over from the last run.", len(batches))
        return batches

    def _save_locked(self):
        folder = os.path.dirname(self._file)
        try:
            os.makedirs(folder, exist_ok=True)
            temporary = self._file + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._batches, handle, indent=1)
            os.replace(temporary, self._file)
        except OSError:
            logger.warning("Could not save %s.", self._file, exc_info=True)

    # -- public ------------------------------------------------------

    def pending(self):
        """Copies of every saved batch, oldest first."""
        with self._lock:
            return [json.loads(json.dumps(batch)) for batch in self._batches]

    def add(self, account, items, permanent, trash_folder):
        """Saves a batch and wakes its account's thread. Returns its id."""
        batch = {
            "id": uuid.uuid4().hex,
            "account_id": account.account_id,
            "permanent": bool(permanent),
            "trash_folder": trash_folder,
            "items": [
                [None if item[0] is None else str(item[0]), item[1], item[2] if len(item) > 2 else None]
                for item in items
            ],
            "attempts": 0,
        }
        with self._lock:
            self._batches.append(batch)
            self._save_locked()
        self._ensure_thread(account.account_id)
        return batch["id"]

    def start(self):
        """Starts a thread for every account with a carried-over batch."""
        for account_id in {batch["account_id"] for batch in self.pending()}:
            self._ensure_thread(account_id)

    def stop(self):
        """Ends every thread at its next step. Batches still saved are
        run again at the next start."""
        self._stopping.set()
        for event in list(self._wake.values()):
            event.set()

    # -- worker ------------------------------------------------------

    def _ensure_thread(self, account_id):
        if self._stopping.is_set():
            return
        with self._lock:
            wake = self._wake.setdefault(account_id, threading.Event())
            thread = self._threads.get(account_id)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self._run, args=(account_id,),
                    name=f"zbox-delete-{account_id}", daemon=True,
                )
                self._threads[account_id] = thread
                thread.start()
        wake.set()

    def _next_batch(self, account_id):
        with self._lock:
            for batch in self._batches:
                if batch["account_id"] == account_id:
                    return json.loads(json.dumps(batch))
        return None

    def _set_attempts(self, batch_id, attempts):
        with self._lock:
            for batch in self._batches:
                if batch["id"] == batch_id:
                    batch["attempts"] = attempts
                    self._save_locked()
                    return

    def _finish(self, batch, moved, unresolved):
        with self._lock:
            self._batches = [saved for saved in self._batches if saved["id"] != batch["id"]]
            self._save_locked()
        items = [tuple(item) for item in batch["items"]]
        try:
            self._dispatch(
                self._on_done, batch["id"], batch["account_id"], items,
                [tuple(item) for item in moved], list(unresolved),
            )
        except Exception:  # noqa: BLE001 - the window may already be gone
            logger.debug("Could not report a finished delete batch.", exc_info=True)

    def _run(self, account_id):
        wake = self._wake[account_id]
        while not self._stopping.is_set():
            wake.clear()
            batch = self._next_batch(account_id)
            if batch is None:
                wake.wait()
                continue
            account = self._account_for(account_id)
            if account is None:
                logger.warning("Dropping a delete batch for missing account %s.", account_id)
                self._finish(batch, [], [(item[0], item[1], "move_failed") for item in batch["items"]])
                continue
            started = time.monotonic()
            try:
                moved, unresolved = process_batch(self._paths, account, batch, pause=self._pause)
            except imap_body_fetch.ImapBodyFetchUnavailable as exc:
                if self._stopping.is_set():
                    return
                attempts = int(batch.get("attempts", 0)) + 1
                if attempts >= MAX_ATTEMPTS:
                    logger.warning(
                        "Giving up a delete batch on %s after %d tries: %s",
                        account_id, attempts, exc,
                    )
                    self._finish(batch, [], [(item[0], item[1], "move_failed") for item in batch["items"]])
                    continue
                self._set_attempts(batch["id"], attempts)
                delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
                logger.info(
                    "Delete batch on %s could not reach the server (try %d): %s. Retrying in %d s.",
                    account_id, attempts, exc, delay,
                )
                wake.wait(delay)
                continue
            except Exception:  # noqa: BLE001 - a bug must not loop forever
                logger.exception("Delete batch on %s failed unexpectedly.", account_id)
                self._finish(batch, [], [(item[0], item[1], "move_failed") for item in batch["items"]])
                continue
            logger.info(
                "Delete batch on %s done in %.1f s: %d item(s), %d unresolved.",
                account_id, time.monotonic() - started, len(batch["items"]), len(unresolved),
            )
            self._finish(batch, moved, unresolved)
