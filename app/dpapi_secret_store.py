"""
Encrypted-at-rest secret store, self-contained inside ZBox's own
portable config folder instead of the OS-wide credential manager.

Two layers, and the second one is what makes the folder portable:

1. Every account secret is encrypted with a random 32-byte data key
   using AES-256-GCM (win_crypto, which calls Windows' own CNG).
2. That data key is stored wrapped, never bare -- once with Windows
   DPAPI, and optionally once more with a key derived from a master
   password (PBKDF2-HMAC-SHA256).

The DPAPI wrap is what makes the normal case silent: on the machine
and Windows account that wrote it, the data key unwraps with no
prompt, exactly as before. But DPAPI keys belong to one Windows user
on one machine, so a folder carried to a second computer cannot
unwrap it there -- which is the whole point of the master password.
Given one, ZBox asks for it once on the new machine, unwraps the data
key with it, and immediately adds a DPAPI wrap for the new machine,
so every later launch there is silent too. Given none, the secrets
simply cannot be read on the new machine and every account has to be
signed in again. That is the intended security floor: a folder alone
is never enough, and moving computers costs either the master
password or a fresh sign-in.

(Thunderbird's portable builds take the other road -- the key sits in
key4.db next to logins.json with no passphrase by default, so anyone
holding the profile folder holds the passwords. ZBox does not.)

A keyset is one data key plus the accounts it encrypts and the wraps
that open it. The file holds a list of them, because a folder that
has been round-tripped between machines can end up with secrets
written under a key this machine cannot open: rather than destroy
them, a new keyset is added alongside, and each lookup uses whichever
keyset can actually be opened here.

There is deliberately no plaintext fallback. If pywin32 isn't
installed, every function raises SecretStoreUnavailable rather than
silently storing the password unencrypted.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as _random

try:
    import win32crypt
    DPAPI_AVAILABLE = True
except ImportError:
    DPAPI_AVAILABLE = False

import win_crypto

_log = logging.getLogger("zbox.secrets")

_DESCRIPTION = "ZBox account password"
_FORMAT_VERSION = 2

# PBKDF2 rounds for the master password. Paid once, on a machine the
# folder has just arrived at, so a second of work is a fair price for
# the offline-guessing cost it buys.
_KDF_ITERATIONS = 600000
_KDF_NAME = "pbkdf2-hmac-sha256"

# How many machines one keyset remembers. Beyond this the oldest wrap
# is dropped, and that machine asks for the master password again.
_MAX_DPAPI_WRAPS = 8

_MASTER_AAD = b"zbox-master-key-wrap"

# How many machine fingerprints one keyset remembers, same reasoning
# as the wrap limit above.
_MAX_MACHINES = 8


class SecretStoreUnavailable(RuntimeError):
    """pywin32 isn't installed, so secrets can't be encrypted.
    Raised instead of falling back to plaintext storage."""


class MasterPasswordRequired(RuntimeError):
    """These secrets were written on another machine and this one has
    no DPAPI wrap for them. The master password can still open them."""


class WrongMasterPassword(ValueError):
    """The master password did not decrypt the data key."""


class SecretsLocked(RuntimeError):
    """No wrap in this keyset can be opened here."""

    def __init__(self, master_available):
        super().__init__(
            "The stored secrets were written on another computer."
        )
        self.master_available = master_available


# --- which computer is this -------------------------------------------

def machine_id():
    """A stable, opaque fingerprint of this computer and Windows
    account.

    Used as a hard gate on top of DPAPI, not as a replacement for it.
    DPAPI alone is almost the same test -- its keys belong to one
    Windows account on one machine -- with one real exception: on a
    domain or Microsoft account with a roaming profile, the DPAPI
    master key roams too, so the same user signing in on a second
    computer could silently unwrap secrets there. That is exactly the
    case ZBox is supposed to notice and lock on, so the machine has
    to be recognised in its own right.

    MachineGuid is written once at Windows install time and does not
    move with a profile. It is hashed together with the account name
    rather than stored as-is, so secrets.dat carries no machine
    identifier in the clear.
    """
    parts = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            parts.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
    except Exception:
        # No registry (not Windows) or the value is missing: fall back
        # to the computer name. Weaker, but the DPAPI wrap underneath
        # is still doing the real work.
        parts.append(os.environ.get("COMPUTERNAME", "unknown-machine"))

    parts.append(os.environ.get("USERDOMAIN", ""))
    parts.append(os.environ.get("USERNAME", ""))
    raw = "|".join(parts).lower().encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


# --- file -------------------------------------------------------------

def _secrets_file(config_dir):
    return os.path.join(config_dir, "secrets.dat")


def _require_dpapi():
    if not DPAPI_AVAILABLE:
        _log.warning(
            "pywin32 is not installed, so secrets cannot be used."
        )
        raise SecretStoreUnavailable(
            "pywin32 is required to store account passwords securely. "
            "Install it with: pip install pywin32"
        )


def _load_raw(config_dir):
    path = _secrets_file(config_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable file: treat as empty rather than
        # crash: every caller can still set a fresh password, which
        # simply overwrites this file with a good one.
        return {}


def _save(config_dir, store):
    path = _secrets_file(config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2)


def _empty_store():
    return {"version": _FORMAT_VERSION, "keysets": [], "legacy_dpapi": {}}


def _load(config_dir):
    """The store, in the current shape whatever shape it was in.

    Version 1 was a flat {account id: DPAPI blob} map with no data
    key at all. Those entries are kept as-is under legacy_dpapi and
    still read; each one moves into a keyset the next time that
    account's password is written.
    """
    data = _load_raw(config_dir)
    if not isinstance(data, dict) or not data:
        return _empty_store()

    if data.get("version") == _FORMAT_VERSION:
        store = _empty_store()
        keysets = data.get("keysets")
        if isinstance(keysets, list):
            store["keysets"] = [k for k in keysets if isinstance(k, dict)]
        legacy = data.get("legacy_dpapi")
        if isinstance(legacy, dict):
            store["legacy_dpapi"] = legacy
        return store

    store = _empty_store()
    store["legacy_dpapi"] = {
        key: value for key, value in data.items() if isinstance(value, str)
    }
    return store


# --- primitives -------------------------------------------------------

def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text):
    return base64.b64decode(text)


def _dpapi_protect(raw):
    return _b64(win32crypt.CryptProtectData(
        raw, _DESCRIPTION, None, None, None, 0
    ))


def _dpapi_unprotect(encoded):
    """The wrapped bytes, or None when this Windows account cannot
    open the blob -- which is the normal answer on another machine,
    not an error."""
    try:
        _, raw = win32crypt.CryptUnprotectData(
            _unb64(encoded), None, None, None, 0
        )
        return raw
    except Exception:
        return None


def _derive(master_password, salt, iterations):
    return hashlib.pbkdf2_hmac(
        "sha256", master_password.encode("utf-8"), salt, iterations,
        dklen=win_crypto.KEY_SIZE,
    )


def _seal(key, payload, aad):
    nonce = _random.token_bytes(win_crypto.NONCE_SIZE)
    return _b64(nonce + win_crypto.encrypt(key, nonce, payload, aad))


def _open(key, encoded, aad):
    raw = _unb64(encoded)
    nonce = raw[:win_crypto.NONCE_SIZE]
    return win_crypto.decrypt(key, nonce, raw[win_crypto.NONCE_SIZE:], aad)


def _account_aad(account_id):
    """Binds a secret to its account id, so a blob moved from one
    account's entry to another's fails to open instead of handing
    back the wrong password."""
    return ("zbox-secret:" + account_id).encode("utf-8")


# --- keysets ----------------------------------------------------------

def _wraps(keyset):
    wraps = keyset.setdefault("wraps", {})
    if not isinstance(wraps.get("dpapi"), list):
        wraps["dpapi"] = []
    return wraps


def _accounts(keyset):
    accounts = keyset.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = keyset["accounts"] = {}
    return accounts


def _machines(keyset):
    ids = keyset.setdefault("machines", [])
    if not isinstance(ids, list):
        ids = keyset["machines"] = []
    return ids


def _machine_known(keyset):
    """An empty list means a keyset written before machines were
    recorded; it is adopted by the first machine to write to it
    rather than locking everyone out of an existing store."""
    ids = _machines(keyset)
    return not ids or machine_id() in ids


def _remember_machine(keyset):
    ids = _machines(keyset)
    current = machine_id()
    if current not in ids:
        ids.append(current)
        if len(ids) > _MAX_MACHINES:
            del ids[:-_MAX_MACHINES]


def _has_master(keyset):
    master = _wraps(keyset).get("master")
    return isinstance(master, dict) and master.get("blob")


def _unlock_with_dpapi(keyset):
    # An unrecognised computer never opens silently, even if a roamed
    # DPAPI key would have obliged. This is the automatic lock: the
    # master password is the only way past it, and entering it is
    # what adds this machine to the list.
    if not _machine_known(keyset):
        return None
    for encoded in _wraps(keyset)["dpapi"]:
        data_key = _dpapi_unprotect(encoded)
        if data_key:
            return data_key
    return None


def _unlock_with_master(keyset, master_password):
    master = _wraps(keyset).get("master")
    if not isinstance(master, dict) or not master.get("blob"):
        return None
    kdf = master.get("kdf") or {}
    salt = _unb64(kdf.get("salt", ""))
    iterations = int(kdf.get("iterations", _KDF_ITERATIONS))
    kek = _derive(master_password, salt, iterations)
    try:
        return _open(kek, master["blob"], _MASTER_AAD)
    except win_crypto.DecryptionFailed:
        raise WrongMasterPassword("That master password is not correct.")


def _unlock(keyset, master_password=None):
    data_key = _unlock_with_dpapi(keyset)
    if data_key:
        return data_key
    if master_password is not None:
        data_key = _unlock_with_master(keyset, master_password)
        if data_key:
            return data_key
    raise SecretsLocked(bool(_has_master(keyset)))


def _add_dpapi_wrap(keyset, data_key):
    """Remembers this machine, so the master password is asked for
    once per computer and never again."""
    wraps = _wraps(keyset)
    wraps["dpapi"].append(_dpapi_protect(data_key))
    if len(wraps["dpapi"]) > _MAX_DPAPI_WRAPS:
        del wraps["dpapi"][:-_MAX_DPAPI_WRAPS]
    _remember_machine(keyset)


def _set_master_wrap(keyset, data_key, master_password):
    salt = _random.token_bytes(16)
    kek = _derive(master_password, salt, _KDF_ITERATIONS)
    _wraps(keyset)["master"] = {
        "kdf": {
            "name": _KDF_NAME,
            "salt": _b64(salt),
            "iterations": _KDF_ITERATIONS,
        },
        "blob": _seal(kek, data_key, _MASTER_AAD),
    }


def _new_keyset(store, master_password=None):
    keyset = {
        "id": _random.token_hex(8),
        "wraps": {"dpapi": []},
        "accounts": {},
        "machines": [],
    }
    data_key = _random.token_bytes(win_crypto.KEY_SIZE)
    _add_dpapi_wrap(keyset, data_key)
    if master_password:
        _set_master_wrap(keyset, data_key, master_password)
    store["keysets"].append(keyset)
    return keyset, data_key


def _writable_keyset(store):
    """A keyset this machine can open, for writing a new secret into.

    Preference goes to one that already carries a master password, so
    a secret added later is covered by the same portability the rest
    of the accounts have.
    """
    openable = []
    for keyset in store["keysets"]:
        data_key = _unlock_with_dpapi(keyset)
        if data_key:
            openable.append((keyset, data_key))
    for keyset, data_key in openable:
        if _has_master(keyset):
            return keyset, data_key
    if openable:
        return openable[0]

    # Everything on file was written elsewhere. Those secrets stay
    # exactly as they are -- they are still good on the machine that
    # wrote them -- and a new keyset is started for this one. Any
    # master password already in use has to be re-applied by the
    # user, since the old one only opens the old key.
    return _new_keyset(store)


def _find_keyset_for(store, account_id):
    for keyset in store["keysets"]:
        if account_id in _accounts(keyset):
            return keyset
    return None


# --- public API -------------------------------------------------------

def set_password(config_dir, account_id, password):
    _require_dpapi()
    store = _load(config_dir)
    keyset, data_key = _writable_keyset(store)

    # A stale copy under a key this machine cannot open would shadow
    # the new one on lookup, so drop it everywhere else first.
    for other in store["keysets"]:
        if other is not keyset:
            _accounts(other).pop(account_id, None)
    store["legacy_dpapi"].pop(account_id, None)

    _remember_machine(keyset)
    _accounts(keyset)[account_id] = _seal(
        data_key, password.encode("utf-8"), _account_aad(account_id)
    )
    _migrate_legacy(store, keyset, data_key)
    _save(config_dir, store)


def get_password(config_dir, account_id):
    _require_dpapi()
    store = _load(config_dir)

    legacy = store["legacy_dpapi"].get(account_id)
    if legacy:
        raw = _dpapi_unprotect(legacy)
        return raw.decode("utf-8") if raw else None

    keyset = _find_keyset_for(store, account_id)
    if keyset is None:
        return None
    try:
        data_key = _unlock(keyset)
        return _open(
            data_key, _accounts(keyset)[account_id], _account_aad(account_id)
        ).decode("utf-8")
    except (SecretsLocked, WrongMasterPassword,
            win_crypto.CryptoUnavailable, win_crypto.DecryptionFailed):
        # Locked (wrong machine, no master password entered yet) is
        # not distinguishable from "nothing stored" to the callers,
        # and both mean the same thing to them: this account cannot
        # authenticate right now. The GUI asks about the master
        # password separately, at startup, using status() below.
        return None


def delete_password(config_dir, account_id):
    store = _load(config_dir)
    changed = store["legacy_dpapi"].pop(account_id, None) is not None
    for keyset in store["keysets"]:
        if _accounts(keyset).pop(account_id, None) is not None:
            changed = True
    if changed:
        _save(config_dir, store)


def _migrate_legacy(store, keyset, data_key):
    """Moves any version-1 entry this machine can still decrypt into
    the keyset, so it gains the master password's portability too.
    Entries that cannot be read here are left untouched."""
    moved = 0
    for account_id in list(store["legacy_dpapi"]):
        raw = _dpapi_unprotect(store["legacy_dpapi"][account_id])
        if not raw:
            continue
        _accounts(keyset)[account_id] = _seal(
            data_key, raw, _account_aad(account_id)
        )
        del store["legacy_dpapi"][account_id]
        moved += 1
    if moved:
        _log.info("Migrated %d legacy secret(s) into the keyset.", moved)


# --- master password --------------------------------------------------

def status(config_dir):
    """What the GUI needs at startup, without asking for anything.

    secrets_present  -- there is at least one stored secret
    locked           -- how many keysets holding secrets cannot be
                        opened on this machine
    master_available -- at least one of those has a master wrap, so
                        asking for the master password is worth it
    master_set       -- a master password is in use at all
    """
    store = _load(config_dir)
    present = False
    locked = 0
    master_available = False
    master_set = False

    # Version-1 entries have no data key and no master wrap at all,
    # so on another machine they are simply unreadable. They still
    # count as locked, otherwise a folder that predates the master
    # password would move to a new computer and fail silently with
    # no explanation offered.
    if store["legacy_dpapi"]:
        present = True
        if all(_dpapi_unprotect(blob) is None
               for blob in store["legacy_dpapi"].values()):
            locked += 1

    new_machine = False
    for keyset in store["keysets"]:
        accounts = _accounts(keyset)
        has_master = bool(_has_master(keyset))
        if has_master:
            master_set = True
        if not accounts:
            continue
        present = True
        if not _machine_known(keyset):
            new_machine = True
        if _unlock_with_dpapi(keyset) is None:
            locked += 1
            if has_master:
                master_available = True

    result = {
        "secrets_present": present,
        "locked": locked,
        "master_available": master_available,
        "master_set": master_set,
        "new_machine": new_machine,
    }
    _log.debug("Secret store status: %s", result)
    return result


def has_master_password(config_dir):
    return status(config_dir)["master_set"]


def verify_master_password(config_dir, master_password):
    """Whether this password opens the master wrap. Nothing is
    changed either way.

    unlock_with_master below cannot answer this: it skips every
    keyset the machine can already open with DPAPI, which on the
    computer the accounts were set up on is all of them, so it
    returns False for a perfectly correct password. That is right
    for what it does -- there is nothing to unlock -- and wrong for
    the app lock, which needs to check a password on exactly that
    machine. See unlock_methods and the startup lock.
    """
    _require_dpapi()
    store = _load(config_dir)
    for keyset in store["keysets"]:
        if not _has_master(keyset):
            continue
        try:
            if _unlock_with_master(keyset, master_password):
                _log.info("Master password verified.")
                return True
        except WrongMasterPassword:
            continue
        except win_crypto.CryptoUnavailable as exc:
            _log.warning("Master password check unavailable: %s", exc)
            return False
    _log.info("Master password check failed.")
    return False


def unlock_with_master(config_dir, master_password):
    """Opens every keyset this master password fits and records a
    DPAPI wrap for this machine, so nothing prompts again here.

    Returns True if at least one keyset was unlocked. Raises
    WrongMasterPassword when a master wrap exists but the password
    does not open any of them.
    """
    _require_dpapi()
    store = _load(config_dir)
    unlocked = 0
    wrong = 0

    for keyset in store["keysets"]:
        if _unlock_with_dpapi(keyset) is not None:
            continue
        if not _has_master(keyset):
            continue
        try:
            data_key = _unlock_with_master(keyset, master_password)
        except WrongMasterPassword:
            wrong += 1
            continue
        if data_key:
            # Records both halves of "this computer is allowed now":
            # a DPAPI wrap it can open, and its fingerprint.
            _add_dpapi_wrap(keyset, data_key)
            unlocked += 1

    if unlocked:
        _save(config_dir, store)
        _log.info(
            "Unlocked %d keyset(s) with the master password.", unlocked
        )
        return True
    if wrong:
        _log.warning(
            "Master password did not open %d locked keyset(s).", wrong
        )
        raise WrongMasterPassword("That master password is not correct.")
    _log.info("Master password unlock: nothing was locked here.")
    return False


def set_master_password(config_dir, new_password, current_password=None):
    """Sets, or replaces, the master password on every keyset this
    machine can open.

    A keyset that cannot be opened here keeps whatever master wrap it
    already had -- there is no way to re-wrap a key this machine
    cannot see, and quietly dropping it would lock those secrets away
    for good.
    """
    _require_dpapi()
    if not new_password:
        _log.warning("Master password change refused: empty password.")
        raise ValueError("The master password cannot be empty.")

    store = _load(config_dir)
    if not store["keysets"] and not store["legacy_dpapi"]:
        # Nothing stored yet: start a keyset so the master password
        # is already in force for the first account added.
        _new_keyset(store, new_password)
        _save(config_dir, store)
        _log.info(
            "Master password set on a new keyset, nothing stored yet."
        )
        return True

    changed = 0
    for keyset in store["keysets"]:
        try:
            data_key = _unlock(keyset, current_password)
        except (SecretsLocked, WrongMasterPassword):
            continue
        _set_master_wrap(keyset, data_key, new_password)
        changed += 1

    if not changed:
        keyset, data_key = _new_keyset(store, new_password)
        _migrate_legacy(store, keyset, data_key)
        _save(config_dir, store)
        _log.info(
            "No existing keyset could be opened here, so the master "
            "password was set on a new one."
        )
        return True

    # Version-1 entries are not covered by any data key, so they
    # would silently stay unportable. Fold them in now.
    for keyset in store["keysets"]:
        data_key = _unlock_with_dpapi(keyset)
        if data_key and _has_master(keyset):
            _migrate_legacy(store, keyset, data_key)
            break

    _save(config_dir, store)
    _log.info("Master password set on %d keyset(s).", changed)
    return True


def clear_master_password(config_dir, current_password):
    """Removes the master password. The folder still works on this
    machine and stops working on any other -- which is the point of
    removing it."""
    _require_dpapi()
    store = _load(config_dir)
    removed = 0
    for keyset in store["keysets"]:
        if not _has_master(keyset):
            continue
        try:
            _unlock(keyset, current_password)
        except (SecretsLocked, WrongMasterPassword):
            continue
        del _wraps(keyset)["master"]
        removed += 1
    if removed:
        _save(config_dir, store)
        _log.info("Master password removed from %d keyset(s).", removed)
    else:
        _log.info("Master password removal changed nothing.")
    return bool(removed)


# --- keys for encrypting files in the data folder ---------------------
#
# Task 3 encrypts account and personal data at rest. It deliberately
# does not build its own key hierarchy: everything above already
# exists to protect one random data key with a DPAPI wrap, a master
# password wrap and a machine fingerprint, and a second hierarchy
# would mean two master passwords to keep in step and two places to
# add the FIDO2 hmac-secret wrap to later. So file encryption hangs
# off the same keyset.
#
# What it does not do is use the same key. The data key encrypts
# account passwords; files get a separate key derived from it, so a
# flaw that leaks one does not hand over the other, and neither
# ciphertext can be replayed into the other's decrypt path.

_FILE_KEY_INFO = b"zbox-file-encryption-v1"


class KeysetMissing(RuntimeError):
    """An encrypted file names a keyset that is not in secrets.dat.

    Means secrets.dat was replaced, deleted or restored from an older
    backup while the encrypted files stayed. The data is not
    recoverable without the original file, and saying so is better
    than reporting it as a wrong password the user can keep retrying.
    """


def _keyset_id(keyset):
    """A short, opaque handle for a keyset, assigned on first use.

    Written into each encrypted file so the right key is chosen on a
    folder that has been round-tripped between machines and carries
    more than one keyset. Carries no meaning and identifies nothing
    outside this file.
    """
    keyset_id = keyset.get("id")
    if not isinstance(keyset_id, str) or not keyset_id:
        keyset_id = keyset["id"] = _random.token_hex(8)
    return keyset_id


def _derive_file_key(data_key):
    """HKDF-Expand, one block. The data key is already 32 uniform
    random bytes, so the extract step has nothing to add."""
    return hmac.new(
        data_key, _FILE_KEY_INFO + b"\x01", hashlib.sha256
    ).digest()


def file_key(config_dir, keyset_id=None):
    """The key for encrypting files, as (keyset_id, key).

    Pass the keyset_id recorded in a file to read it back; pass None
    to get the key new files should be written with.

    Raises SecretsLocked when the keyset exists but cannot be opened
    on this machine, which is the same condition the startup lock
    already handles by asking for the master password. Raises
    KeysetMissing when the named keyset is gone entirely. It never
    returns None: a caller that cannot get a key must refuse to read
    or write, not fall back to plaintext.
    """
    _require_dpapi()
    store = _load(config_dir)

    if keyset_id is not None:
        for keyset in store["keysets"]:
            if keyset.get("id") == keyset_id:
                data_key = _unlock_with_dpapi(keyset)
                if data_key is None:
                    _log.warning(
                        "File key locked on this machine, keyset %s.",
                        keyset_id,
                    )
                    raise SecretsLocked(bool(_has_master(keyset)))
                return keyset_id, _derive_file_key(data_key)
        _log.error(
            "File key missing, keyset %s is not in secrets.dat.",
            keyset_id,
        )
        raise KeysetMissing(
            "The key these files were encrypted with is not in "
            "secrets.dat. It was replaced, removed, or restored from "
            "a backup older than the files."
        )

    before = len(store["keysets"])
    keyset, data_key = _writable_keyset(store)
    had_id = keyset.get("id")
    new_id = _keyset_id(keyset)
    if not had_id or len(store["keysets"]) != before:
        _save(config_dir, store)
    return new_id, _derive_file_key(data_key)


# --- private mode --------------------------------------------------------

def forget_this_machine(config_dir):
    """Removes this computer from every keyset that a master password also
    opens: the DPAPI wraps this Windows account can open, and this
    computer's fingerprint. The next start here asks for the master
    password again. A keyset without a master password is left alone, so
    nothing is ever locked out for good. True when something was removed.
    Used by private mode when ZBox closes (private_mode.close_cleanup)."""
    _require_dpapi()

    def opens_here(encoded):
        try:
            return bool(_dpapi_unprotect(encoded))
        except Exception:  # noqa: BLE001 - another computer's wrap
            return False

    store = _load(config_dir)
    current = machine_id()
    changed = False
    for keyset in store["keysets"]:
        if not _has_master(keyset):
            continue
        wraps = _wraps(keyset)
        kept = [encoded for encoded in wraps["dpapi"] if not opens_here(encoded)]
        if len(kept) != len(wraps["dpapi"]):
            wraps["dpapi"] = kept
            changed = True
        machines = _machines(keyset)
        if current in machines:
            machines.remove(current)
            changed = True
    if changed:
        _save(config_dir, store)
        _log.info("This computer was removed from the trusted list.")
    return changed
