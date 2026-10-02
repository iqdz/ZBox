"""
Encryption at rest for ZBox's own data files.

Task 3. The account passwords were already encrypted by
dpapi_secret_store; this covers everything else the data folder holds
about the person using it -- account addresses and server settings,
the address book, and later the offline mail and message caches.

The key comes from dpapi_secret_store.file_key(), derived from the
same keyset that protects the passwords. That is the whole design
decision: one data key, one master password, one machine
fingerprint, and one place for the FIDO2 hmac-secret wrap to be
added later so it covers passwords and files together.

Format. An encrypted file is still JSON, so it stays inspectable
enough to tell what it is without being readable, and .claudeignore
and the backup story do not change:

    {
      "zbox_encrypted": 1,
      "keyset": "<opaque keyset id>",
      "nonce": "<base64>",
      "blob": "<base64 ciphertext + GCM tag>"
    }

The file's own name is the AAD, so a ciphertext moved from
accounts.json to contacts.json fails its tag rather than being
decrypted into the wrong reader.

Failure behaviour, and this is the part that inverts Task 2. The
startup lock fails OPEN on purpose: a bug there that locks someone
out of their own mail is worse than a skipped gate, because the gate
is not the protection. This module is the protection, so it fails
CLOSED. There is no plaintext fallback and no empty-dict default
when a file exists but cannot be opened.

That matters most for the caller, not for here. A reader that turns
"cannot decrypt" into "no accounts configured" will look like an
empty install, and the next save will then write an empty file over
data that was merely locked. So read_json raises rather than
returning a default whenever the file exists, and every caller must
let that propagate and refuse to save afterwards.
"""

import base64
import json
import logging
import os
import secrets as _random

import dpapi_secret_store
import win_crypto

_log = logging.getLogger("zbox.crypto")

_MAGIC = "zbox_encrypted"
_FORMAT_VERSION = 1


class EncryptedDataUnavailable(RuntimeError):
    """An encrypted data file could not be opened.

    Wraps every reason it can happen -- locked on a machine that has
    not had the master password entered, a missing keyset, a failed
    tag, no pywin32 -- because the caller's response to all of them
    is the same: show the problem, and do not write anything back.
    """


def _aad(path):
    """Binds the ciphertext to the file it lives in. Lower-cased
    because Windows paths are not case-sensitive and a rename that
    only changes case must not break decryption."""
    name = os.path.basename(path).lower()
    return ("zbox-file:" + name).encode("utf-8")


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text):
    return base64.b64decode(text)


def is_encrypted_container(data):
    return isinstance(data, dict) and _MAGIC in data


def is_encrypted(path):
    """Whether the file on disk is one of ours. A file that is not
    valid JSON at all is reported as not encrypted, which sends the
    caller down its own existing corrupt-file path rather than this
    module's."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return is_encrypted_container(json.load(handle))
    except (OSError, ValueError):
        return False


# --- bytes in, bytes out ----------------------------------------------

def encrypt_bytes(config_dir, path, raw):
    """The container dict for raw, keyed for path."""
    try:
        keyset_id, key = dpapi_secret_store.file_key(config_dir)
    except (dpapi_secret_store.SecretsLocked,
            dpapi_secret_store.KeysetMissing,
            dpapi_secret_store.SecretStoreUnavailable) as exc:
        _log.warning(
            "No file key to encrypt %s: %s: %s",
            os.path.basename(path), type(exc).__name__, exc,
        )
        raise EncryptedDataUnavailable(str(exc)) from exc

    nonce = _random.token_bytes(win_crypto.NONCE_SIZE)
    try:
        blob = win_crypto.encrypt(key, nonce, raw, _aad(path))
    except win_crypto.CryptoUnavailable as exc:
        raise EncryptedDataUnavailable(str(exc)) from exc

    return {
        _MAGIC: _FORMAT_VERSION,
        "keyset": keyset_id,
        "nonce": _b64(nonce),
        "blob": _b64(blob),
    }


def decrypt_bytes(config_dir, path, container):
    """The plaintext bytes of a container written by encrypt_bytes."""
    if not is_encrypted_container(container):
        raise EncryptedDataUnavailable(
            "This file is not in ZBox's encrypted format."
        )
    if container.get(_MAGIC) != _FORMAT_VERSION:
        raise EncryptedDataUnavailable(
            "This file was written by a newer version of ZBox."
        )

    try:
        _, key = dpapi_secret_store.file_key(
            config_dir, container.get("keyset")
        )
    except (dpapi_secret_store.SecretsLocked,
            dpapi_secret_store.KeysetMissing,
            dpapi_secret_store.SecretStoreUnavailable) as exc:
        _log.warning(
            "No file key to decrypt %s: %s: %s",
            os.path.basename(path), type(exc).__name__, exc,
        )
        raise EncryptedDataUnavailable(str(exc)) from exc

    try:
        return win_crypto.decrypt(
            key,
            _unb64(container["nonce"]),
            _unb64(container["blob"]),
            _aad(path),
        )
    except (KeyError, ValueError, win_crypto.CryptoUnavailable) as exc:
        _log.error(
            "Decrypt failed for %s, the file did not verify: %s",
            os.path.basename(path), exc,
        )
        # A tag mismatch here is not a wrong password -- the key came
        # from a keyset that opened cleanly -- so it means the file
        # was altered, truncated, or renamed from another file.
        raise EncryptedDataUnavailable(
            "The file did not verify. It has been altered or is "
            "damaged: %s" % exc
        ) from exc


# --- the interface callers use ----------------------------------------

def read_json(config_dir, path, default=None):
    """The decoded contents of path, encrypted or not.

    default is returned only when the file does not exist. A file
    that exists and cannot be opened raises, so that a locked folder
    is never mistaken for an empty one.
    """
    if not os.path.isfile(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise EncryptedDataUnavailable(
            "Could not read %s: %s" % (os.path.basename(path), exc)
        ) from exc
    except ValueError:
        # Not JSON at all. Left to the caller's existing handling for
        # a corrupt file, which is a different problem with a
        # different answer.
        raise

    if not is_encrypted_container(data):
        return data

    raw = decrypt_bytes(config_dir, path, data)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EncryptedDataUnavailable(
            "The file decrypted but its contents are not valid JSON."
        ) from exc


def write_json(config_dir, path, value, indent=2):
    """Writes value to path encrypted, atomically.

    Atomically because the alternative is a truncated accounts file
    if the app is killed mid-write, and that is indistinguishable
    from a tampered one once it is encrypted.
    """
    raw = json.dumps(value, indent=indent).encode("utf-8")
    container = encrypt_bytes(config_dir, path, raw)
    _atomic_write(path, json.dumps(container, indent=2))


def _atomic_write(path, text):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temp = path + ".new"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


# --- migration --------------------------------------------------------

def encrypt_in_place(config_dir, path):
    """Encrypts an existing plaintext file, once.

    Returns True if it converted something, False if the file is
    missing or already encrypted. Raises rather than converting when
    no key is available, so a folder that is locked on this machine
    is left exactly as it was instead of being half-migrated.

    The plaintext is replaced, not copied aside. A .bak next to an
    encrypted file would undo the entire point of encrypting it.
    """
    if not os.path.isfile(path):
        return False

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        _log.warning(
            "Convert skipped, %s is unreadable or not JSON: %s",
            os.path.basename(path), exc,
        )
        # Unreadable or not JSON: leave it alone. Encrypting a file
        # that cannot be parsed would preserve the damage in a form
        # nothing can inspect.
        return False

    if is_encrypted_container(data):
        _log.debug(
            "Convert skipped, already encrypted: %s",
            os.path.basename(path),
        )
        return False

    write_json(config_dir, path, data)
    _log.info("Converted to encrypted: %s", os.path.basename(path))
    return True


def decrypt_in_place(config_dir, path):
    """Writes an encrypted file back out as plaintext.

    Here for the deliberate cases -- exporting a folder, turning the
    feature off, recovering data before a reinstall -- and not called
    anywhere automatically.
    """
    if not os.path.isfile(path):
        return False
    value = read_json(config_dir, path)
    if not is_encrypted(path):
        return False
    _atomic_write(path, json.dumps(value, indent=2))
    _log.info("Converted to plaintext: %s", os.path.basename(path))
    return True
