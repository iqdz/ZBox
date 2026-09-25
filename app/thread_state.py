"""
Persistent Watched Thread / Ignored Thread state, per account.

Scope is (account_id, folder, thread_key): a thread never crosses
accounts (see related_messages_panel.py's own reasoning for the same
limit), and Watched/Ignored is kept per folder rather than per
account, matching Thunderbird's own per-folder thread state (a
coincidentally same-subject thread sitting in two different folders
is not "the same thread" here).

thread_key (see thread_grouping.thread_key) is the thread's root
Message-ID, not Himalaya's own backend id -- so a thread stays
watched or ignored across refetches and across the root message being
marked read/flagged/moved by something other than a move out of this
folder.

Important design commitment carried over from Mute Account: setting
Ignored (or Watched) on a thread is a display-only flag for the
Threads view being built on top of this store. It must never delete,
move, or otherwise touch the underlying messages, the account, or the
folder -- exactly like muting an account never removes it from the
tree. An "Ignored Threads" filter hides rows from view; it does not
take mail out of the mailbox, and everything stays reachable by
switching the filter (or opening "All") at any time.

Storage is one small JSON file per account
(profile_dir(account_id)/thread_state.json), loaded once and
rewritten on every change -- same simple load/save-on-write shape as
settings_manager.py and account_manager.py use for their own JSON,
no locking, since nothing else in ZBox writes these files
concurrently.
"""

import json
import logging
import os

logger = logging.getLogger("zbox.threads")


class ThreadStateStore:
    def __init__(self, paths, account_id):
        self._path = os.path.join(paths.profile_dir(account_id), "thread_state.json")
        self._data = self._load()

    def _load(self):
        data = None
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            logger.warning(
                "Could not read thread state from %s; starting fresh.",
                self._path, exc_info=True,
            )
            data = {}
        if not isinstance(data, dict):
            data = {}
        watched = data.get("watched")
        ignored = data.get("ignored")
        return {
            "watched": watched if isinstance(watched, dict) else {},
            "ignored": ignored if isinstance(ignored, dict) else {},
        }

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
        except OSError:
            # Same reasoning as settings_manager/account_manager's own
            # saves not having a retry path: a failed write here loses
            # one watch/ignore toggle, never mail, and the next
            # successful save (the very next toggle) fixes it.
            logger.warning(
                "Could not save thread state to %s.", self._path, exc_info=True,
            )

    def is_watched(self, folder, key):
        return key in self._data["watched"].get(folder, [])

    def is_ignored(self, folder, key):
        return key in self._data["ignored"].get(folder, [])

    def watched_keys(self, folder):
        """Every watched thread_key in this folder, for the View >
        Threads > Watched Threads filter."""
        return set(self._data["watched"].get(folder, []))

    def ignored_keys(self, folder):
        """Every ignored thread_key in this folder, for the View >
        Threads > Ignored Threads filter."""
        return set(self._data["ignored"].get(folder, []))

    def set_watched(self, folder, key, watched):
        self._set_flag("watched", folder, key, watched)

    def set_ignored(self, folder, key, ignored):
        self._set_flag("ignored", folder, key, ignored)

    def _set_flag(self, bucket, folder, key, value):
        entries = self._data[bucket].setdefault(folder, [])
        changed = False
        if value:
            if key not in entries:
                entries.append(key)
                changed = True
        else:
            if key in entries:
                entries.remove(key)
                changed = True
            if not entries:
                self._data[bucket].pop(folder, None)
        if changed:
            self._save()
