"""
Small JSON records of the user's own decisions (thread state, junk
origins, junk rules, pending deletes), encrypted at rest like
contacts.json, with data_crypto's key from the secret store.

Given no config folder (tests, or a caller that has none), a file is read
and written as plain JSON, exactly as before. A plain file is encrypted in
place the first time it is read with a config folder.

Fails closed, as contacts.json does: a file that exists but cannot be
opened reads as empty and is never written over in this run, so the
records in it are not replaced by the few made since.
"""

import json
import logging
import os
import threading

import data_crypto

logger = logging.getLogger("zbox.securejson")

_LOCKED = set()
_LOCK = threading.Lock()


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def load(config_dir, path):
    """The JSON value in path, encrypted or plain. Raises
    FileNotFoundError, OSError and ValueError as open and json.load do.
    A file that cannot be decrypted reads as {} and is marked, so save()
    refuses to write over it."""
    if not config_dir:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    try:
        data = data_crypto.read_json(config_dir, path, default={})
    except data_crypto.EncryptedDataUnavailable as exc:
        with _LOCK:
            _LOCKED.add(_key(path))
        logger.error("Could not open %s: %s", os.path.basename(path), exc)
        return {}
    try:
        data_crypto.encrypt_in_place(config_dir, path)
    except (data_crypto.EncryptedDataUnavailable, OSError) as exc:
        logger.warning("%s is still in plain text: %s", os.path.basename(path), exc)
    return data


def save(config_dir, path, value, indent=2):
    """Writes value to path atomically, encrypted when config_dir is given.
    Raises OSError when the write fails. Does nothing for a file that could
    not be opened in this run."""
    with _LOCK:
        locked = _key(path) in _LOCKED
    if locked:
        logger.error("Not saving %s: it could not be opened.", os.path.basename(path))
        return
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    if not config_dir:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=indent)
        os.replace(temporary, path)
        return
    try:
        data_crypto.write_json(config_dir, path, value, indent=indent)
    except data_crypto.EncryptedDataUnavailable as exc:
        raise OSError(str(exc)) from exc


def forget_locked():
    """Clears the marks of files that could not be opened. For tests."""
    with _LOCK:
        _LOCKED.clear()
