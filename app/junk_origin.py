"""
Persistent record of the folder a message was in immediately before
Mark as Junk moved it to Junk -- so Mark as Not Junk can send it back
where it came from instead of always defaulting to Inbox.

Keyed by the message's stable Message-ID header (see undo_manager.py's
own reasoning -- a message's id is not the same before and after a
folder move, so anything meant to survive a move keys off the header
instead), not Himalaya's backend id.

A message with no recorded origin here was never moved to Junk by
ZBox itself -- delivered there directly by the server's own spam
filtering, moved there by another mail client, or already there from
before this file existed. Mark as Not Junk treats a missing entry the
same way it always has: falls back to Inbox.

Storage is one small JSON file per account
(profile_dir(account_id)/junk_origin.json), loaded once and rewritten
on every change -- same simple load/save-on-write shape thread_state.py
and settings_manager.py already use, no locking, since nothing else in
ZBox writes this file concurrently. Encrypted at rest like
contacts.json (secure_json), since Message-IDs and folder names are
the user's own records.
"""

import json
import logging
import os

import secure_json

logger = logging.getLogger("zbox.junkorigin")


class JunkOriginStore:
    def __init__(self, paths, account_id):
        self._path = os.path.join(paths.profile_dir(account_id), "junk_origin.json")
        # Encrypted at rest when the paths carry a config folder
        # (secure_json), like contacts.json.
        self._config = getattr(paths, "config", None)
        self._data = self._load()

    def _load(self):
        data = None
        try:
            data = secure_json.load(self._config, self._path)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            logger.warning(
                "Could not read junk-origin state from %s; starting fresh.",
                self._path, exc_info=True,
            )
            data = {}
        if not isinstance(data, dict):
            data = {}
        return data

    def _save(self):
        try:
            secure_json.save(self._config, self._path, self._data)
        except OSError:
            # Same reasoning as thread_state.py's own save: a failed
            # write here loses one origin record, never mail, and the
            # next successful save (the very next Mark as Junk or
            # Mark as Not Junk) fixes it.
            logger.warning(
                "Could not save junk-origin state to %s.", self._path, exc_info=True,
            )

    def get_origin(self, message_id_header):
        """The folder this message was moved out of by Mark as Junk,
        or None if nothing is recorded for it -- either it was never
        moved to Junk by ZBox, or the record has already been used
        and cleared."""
        if not message_id_header:
            return None
        return self._data.get(message_id_header)

    def set_origin(self, message_id_header, folder):
        """Records `folder` as where this message came from. Called
        by Mark as Junk at the moment it moves a message, and by Undo
        when reversing a Mark as Not Junk puts a message back into
        Junk."""
        if not message_id_header or not folder:
            return
        if self._data.get(message_id_header) != folder:
            self._data[message_id_header] = folder
            self._save()

    def clear_origin(self, message_id_header):
        """Drops the record for this message. Called by Mark as Not
        Junk once it has used the origin, and by Undo when reversing a
        Mark as Junk takes a message back out of Junk entirely."""
        if not message_id_header:
            return
        if self._data.pop(message_id_header, None) is not None:
            self._save()
