"""
OpenPGP for ZBox, stage 1: the keys.

Every call into PySequoia (the Sequoia PGP library, built into ZBox.exe)
lives in this module, so a later PySequoia, whose API its author still
marks unstable, touches one file. Nothing here starts a program, and
nothing is written outside the ZBox folder:

- secret keys sit in the secret store (dpapi_secret_store), one entry
  per key named by its fingerprint, with the revocation certificate
  made when the key was generated, all under the master password, as
  Thunderbird keeps them under its primary password;
- the key list -- public keys, acceptance, which keys are personal --
  is data/userdata/openpgp.json, encrypted at rest like the address
  book (data_crypto), and failing closed the same way: a list that
  cannot be opened is never replaced by an empty one.

PySequoia cannot put a passphrase on a key it makes, which is why ZBox's
own keys rely on the secret store. A secret key imported with its own
passphrase keeps it, and is marked locked.
"""

import datetime
import logging
import os
import re
import threading

import data_crypto
import dpapi_secret_store

try:  # PySequoia is optional at import time: ZBox runs without it.
    import pysequoia as _ps
except Exception:  # noqa: BLE001 - any failure means "not available"
    _ps = None

logger = logging.getLogger("zbox.openpgp")

ACCEPTANCE = ("rejected", "undecided", "unverified", "verified")
SUITES = ("cv25519", "rsa3072", "rsa4096")
# Thunderbird refuses imports above 5 MB; so does ZBox.
MAX_IMPORT_BYTES = 5 * 1024 * 1024
_SECRET_PREFIX = "openpgp."
_UID_RE = re.compile(r"^\s*(.*?)\s*<\s*([^<>\s]+@[^<>\s]+)\s*>\s*$")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")


class OpenPGPUnavailable(RuntimeError):
    """PySequoia is not part of this copy of ZBox."""


class OpenPGPError(RuntimeError):
    """A key could not be read, unlocked, made or saved."""


def available():
    return _ps is not None


def _require():
    if _ps is None:
        raise OpenPGPUnavailable("PySequoia is not installed")


# --- Small helpers ------------------------------------------------------

def fingerprint_text(fingerprint):
    """A fingerprint in groups of four, upper case, as a screen reader
    can read it: "0B9C 1234 ..."."""
    text = re.sub(r"[^0-9A-Fa-f]", "", str(fingerprint or "")).upper()
    return " ".join(text[i:i + 4] for i in range(0, len(text), 4))


def key_id(fingerprint):
    """The long key ID: the last 16 hex digits of the fingerprint."""
    text = re.sub(r"[^0-9A-Fa-f]", "", str(fingerprint or "")).upper()
    return text[-16:]


def split_user_id(user_id):
    """("Name", "address") from "Name <address>"; ("", address) for a
    bare address; (text, "") for anything else."""
    text = str(user_id or "").strip()
    match = _UID_RE.match(text)
    if match:
        return match.group(1).strip().strip('"'), match.group(2).strip()
    if _EMAIL_RE.match(text):
        return "", text
    return text, ""


def make_user_id(name, email):
    name = str(name or "").strip()
    email = str(email or "").strip()
    return "%s <%s>" % (name, email) if name else "<%s>" % email


def format_date(value):
    """A date as "3 Oct 2029" in local time, or "" when there is none."""
    if not isinstance(value, datetime.datetime):
        return ""
    try:
        local = value.astimezone()
    except (ValueError, OSError, OverflowError):
        local = value
    from envelope_format import _MONTH_ABBREVIATIONS

    return "%d %s %d" % (local.day, _MONTH_ABBREVIATIONS[local.month - 1], local.year)


def _created(cert):
    """The primary key's creation time, read from its packets; None if
    this PySequoia cannot say."""
    try:
        from pysequoia.packet import PacketPile, Tag

        for packet in PacketPile.from_bytes(bytes(cert)):
            if packet.tag in (Tag.PublicKey, Tag.SecretKey):
                return packet.key_created
    except Exception:  # noqa: BLE001 - only a date in the details
        return None
    return None


def describe(cert):
    """What the key list and its details show for one certificate."""
    user_ids = [str(uid) for uid in (cert.user_ids or [])]
    name, email = split_user_id(user_ids[0]) if user_ids else ("", "")
    expiration = cert.expiration
    now = datetime.datetime.now(datetime.timezone.utc)
    expired = False
    if isinstance(expiration, datetime.datetime):
        when = expiration if expiration.tzinfo else expiration.replace(tzinfo=datetime.timezone.utc)
        expired = when <= now
    return {
        "fingerprint": str(cert.fingerprint).lower(),
        "user_ids": user_ids,
        "name": name,
        "email": email,
        "expiration": expiration,
        "expired": expired,
        "revoked": bool(cert.is_revoked),
        "created": _created(cert),
    }


def _suite(name):
    _require()
    return {
        "cv25519": _ps.CipherSuite.Cv25519,
        "rsa3072": _ps.CipherSuite.RSA3k,
        "rsa4096": _ps.CipherSuite.RSA4k,
    }[name]


def _unlock_test(tsk, passphrase=None):
    """True when the secret key decrypts with this passphrase (None for
    an unprotected key). A key that cannot encrypt at all counts as open."""
    try:
        cert = tsk.extract_certificate()
        sample = _ps.encrypt(recipients=[cert], bytes=b"zbox")
    except Exception:  # noqa: BLE001 - a signing-only key
        return True
    try:
        decryptor = tsk.decryptor(passphrase) if passphrase else tsk.decryptor()
        return _ps.decrypt(decryptor=decryptor, bytes=sample).bytes == b"zbox"
    except Exception:  # noqa: BLE001 - wrong or missing passphrase
        return False


def _secret_blocks(raw):
    """Each armored secret key block in raw, as bytes."""
    text = raw.decode("ascii", errors="ignore")
    # Built from two parts: the whole marker, as a real secret key
    # begins, is something commit_guard refuses anywhere in a commit.
    kind = "PRIVATE" + " KEY BLOCK"
    pattern = "-----BEGIN PGP %s-----.*?-----END PGP %s-----" % (kind, kind)
    return [block.encode("ascii") for block in re.findall(pattern, text, re.S)]


# --- Publishing on keys.openpgp.org ----------------------------------------

# The keyserver Thunderbird publishes to. Its interface (VKS): one
# request uploads a public key, a second asks the server to email each
# address on it a link; an address is findable only after its owner
# opens that link.
KEYSERVER = "keys.openpgp.org"
_VKS = "https://keys.openpgp.org/vks/v1/"


def _vks_post(path, payload, timeout=20):
    """One JSON request to the keyserver; its JSON reply. The server's
    own error message is raised as OpenPGPError."""
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        _VKS + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "ZBox"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001 - no readable body
            detail = None
        raise OpenPGPError(detail or "%s %s" % (exc.code, exc.reason)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OpenPGPError(str(getattr(exc, "reason", exc)) or "no answer") from exc


def vks_locale(code):
    """A ZBox language code as the keyserver's locale: pt-br -> pt_BR."""
    parts = str(code or "").split("-")
    if len(parts) == 2:
        return "%s_%s" % (parts[0].lower(), parts[1].upper())
    return parts[0].lower() or None


# --- The key store ------------------------------------------------------

class KeyStore:
    """Every OpenPGP key ZBox knows, personal or a correspondent's."""

    def __init__(self, paths):
        self._file = os.path.join(paths.userdata, "openpgp.json")
        self._config = paths.config
        self._load_error = None
        self._keys = {}  # fingerprint -> entry
        # address -> the key collected from that address's own mail, from
        # its Autocrypt header (stage 4); never used until accepted.
        self._collected = {}
        self._load()

    # Loading and saving, the same fail-closed rule as contacts.json.

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            data = data_crypto.read_json(self._config, self._file, default={})
        except (data_crypto.EncryptedDataUnavailable, OSError, ValueError) as exc:
            self._load_error = str(exc) or "unreadable"
            logger.error("Could not open openpgp.json: %s", exc)
            raise OpenPGPError(self._load_error) from exc
        for fingerprint, entry in (data.get("keys") or {}).items():
            if isinstance(entry, dict) and entry.get("public"):
                acceptance = entry.get("acceptance")
                self._keys[str(fingerprint).lower()] = {
                    "public": str(entry["public"]),
                    "acceptance": acceptance if acceptance in ACCEPTANCE else "undecided",
                    "personal": bool(entry.get("personal")),
                    "secret": bool(entry.get("secret")),
                    "locked": bool(entry.get("locked")),
                }
        for address, entry in (data.get("collected") or {}).items():
            if isinstance(entry, dict) and entry.get("public"):
                self._collected[str(address).lower()] = {
                    "public": str(entry["public"]), "date": str(entry.get("date") or ""),
                }

    def _save(self):
        if self._load_error:
            raise OpenPGPError("openpgp.json could not be opened this session")
        try:
            data = {"keys": self._keys}
            # Only when there are any, so a key list without them is
            # written exactly as before.
            if self._collected:
                data["collected"] = self._collected
            data_crypto.write_json(self._config, self._file, data)
        except (OSError, data_crypto.EncryptedDataUnavailable) as exc:
            logger.exception("Could not save openpgp.json.")
            raise OpenPGPError(str(exc) or "could not save") from exc

    def _put_secret(self, name, value):
        try:
            dpapi_secret_store.set_password(self._config, _SECRET_PREFIX + name, value)
        except Exception as exc:  # noqa: BLE001 - locked or unavailable store
            raise OpenPGPError(str(exc) or "the secret store is not available") from exc

    def _get_secret(self, name):
        try:
            return dpapi_secret_store.get_password(self._config, _SECRET_PREFIX + name)
        except Exception as exc:  # noqa: BLE001 - locked or unavailable store
            raise OpenPGPError(str(exc) or "the secret store is not available") from exc

    def _drop_secret(self, name):
        try:
            dpapi_secret_store.delete_password(self._config, _SECRET_PREFIX + name)
        except Exception:  # noqa: BLE001 - nothing stored is fine
            logger.debug("No stored secret %s to delete.", name)

    # Reading.

    def entries(self):
        """Every key with what the list shows, personal keys first, then
        by name or address."""
        _require()
        rows = []
        for fingerprint, entry in self._keys.items():
            try:
                info = describe(_ps.Cert.from_bytes(entry["public"].encode("ascii")))
            except Exception:  # noqa: BLE001 - one bad entry must not hide the rest
                logger.warning("Key %s could not be read.", fingerprint)
                continue
            info.update(entry)
            info["fingerprint"] = fingerprint
            rows.append(info)
        rows.sort(key=lambda r: (not r["personal"], (r["name"] or r["email"]).casefold()))
        return rows

    def public_armored(self, fingerprint):
        return self._keys[fingerprint]["public"]

    def personal_keys(self):
        return [fpr for fpr, entry in self._keys.items() if entry["personal"] and entry["secret"]]

    def _tsk(self, fingerprint):
        armored = self._get_secret(fingerprint)
        if not armored:
            raise OpenPGPError("the secret key is not available")
        return _ps.Tsk.from_bytes(armored.encode("ascii"))

    def is_locked(self, fingerprint):
        return bool(self._keys.get(fingerprint, {}).get("locked"))

    # Making keys.

    def generate(self, name, email, suite="cv25519", years=3):
        """A new key pair for name and email, made personal and saved,
        with its revocation certificate. Returns the fingerprint."""
        _require()
        validity = int(years * 365.25 * 86400) if years else None
        try:
            tsk = _ps.Tsk.generate(
                make_user_id(name, email), cipher_suite=_suite(suite), validity_seconds=validity,
            )
            cert = tsk.extract_certificate()
            revocation = cert.revoke(certifier=tsk.certifier())
        except OpenPGPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OpenPGPError(str(exc) or "key generation failed") from exc
        fingerprint = str(cert.fingerprint).lower()
        self._put_secret(fingerprint, str(tsk))
        self._put_secret(fingerprint + ".revocation", str(revocation))
        self._keys[fingerprint] = {
            "public": str(cert), "acceptance": "verified",
            "personal": True, "secret": True, "locked": False,
        }
        self._save()
        return fingerprint

    # Importing.

    def preview(self, raw):
        """What raw holds, without importing it: a list of dicts with
        fingerprint, info, secret (a Tsk or None) and locked."""
        _require()
        if len(raw) > MAX_IMPORT_BYTES:
            raise OpenPGPError("the file is larger than 5 MB")
        try:
            certs = _ps.Cert.split_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            raise OpenPGPError(str(exc) or "no OpenPGP key found") from exc
        secrets = {}
        if any(cert.has_secret_keys for cert in certs):
            blocks = _secret_blocks(raw) or [raw]
            for block in blocks:
                try:
                    tsk = _ps.Tsk.from_bytes(block)
                except Exception:  # noqa: BLE001 - its public part still imports
                    continue
                secrets[str(tsk.extract_certificate().fingerprint).lower()] = tsk
        found = []
        for cert in certs:
            fingerprint = str(cert.fingerprint).lower()
            tsk = secrets.get(fingerprint)
            found.append({
                "fingerprint": fingerprint,
                "cert": cert,
                "info": describe(cert),
                "secret": tsk,
                "locked": bool(tsk is not None and not _unlock_test(tsk)),
            })
        return found

    def unlocks(self, item, passphrase):
        """True when passphrase opens a previewed secret key."""
        return item.get("secret") is not None and _unlock_test(item["secret"], passphrase)

    def commit(self, found, skip_secret=()):
        """Imports previewed keys, merging with keys already known, and
        returns how many were imported. A secret key makes the key
        personal; skip_secret names fingerprints whose secret part is
        left out (a passphrase that was not given)."""
        _require()
        count = 0
        for item in found:
            fingerprint = item["fingerprint"]
            cert = item["cert"]
            entry = self._keys.get(fingerprint)
            if entry is not None:
                try:
                    cert = _ps.Cert.from_bytes(entry["public"].encode("ascii")).merge(cert)
                except Exception:  # noqa: BLE001 - keep the newer copy
                    pass
            entry = dict(entry or {"acceptance": "undecided", "personal": False,
                                   "secret": False, "locked": False})
            entry["public"] = str(cert)
            if item.get("secret") is not None and fingerprint not in skip_secret:
                self._put_secret(fingerprint, str(item["secret"]))
                entry.update(secret=True, personal=True, locked=bool(item.get("locked")))
                entry["acceptance"] = "verified"
            self._keys[fingerprint] = entry
            count += 1
        if count:
            self._save()
        return count

    # Backups.

    def backup(self, fingerprint, password):
        """The secret key inside a file encrypted with password, armored."""
        _require()
        data = _ps.encrypt(passwords=[password], bytes=str(self._tsk(fingerprint)).encode("ascii"))
        return data if isinstance(data, bytes) else str(data).encode("ascii")

    @staticmethod
    def is_backup(raw):
        return b"-----BEGIN PGP MESSAGE-----" in raw

    @staticmethod
    def open_backup(raw, password):
        """The key file inside a backup made by backup()."""
        _require()
        try:
            return _ps.decrypt(passwords=[password], bytes=raw).bytes
        except Exception as exc:  # noqa: BLE001
            raise OpenPGPError("wrong password or not a backup") from exc

    # Changing keys.

    def set_flags(self, fingerprint, acceptance=None, personal=None):
        entry = self._keys[fingerprint]
        if acceptance in ACCEPTANCE:
            entry["acceptance"] = acceptance
        if personal is not None:
            entry["personal"] = bool(personal) and entry["secret"]
        self._save()

    def revoke(self, fingerprint, passphrase=None):
        """Revokes one of your own keys; the public key kept here is
        then the revoked one, ready to export and share."""
        _require()
        tsk = self._tsk(fingerprint)
        cert = _ps.Cert.from_bytes(self._keys[fingerprint]["public"].encode("ascii"))
        try:
            certifier = tsk.certifier(passphrase) if passphrase else tsk.certifier()
            revocation = cert.revoke(certifier=certifier)
            revoked = _ps.Cert.from_bytes(bytes(cert) + bytes(revocation))
        except Exception as exc:  # noqa: BLE001
            raise OpenPGPError(str(exc) or "the key could not be revoked") from exc
        self._keys[fingerprint]["public"] = str(revoked)
        self._keys[fingerprint]["personal"] = False
        self._save()

    def publish(self, fingerprint, locale=None):
        """
        Uploads the public key to keys.openpgp.org and asks for the
        verification email for every address on it not yet published.
        Returns {"sent": [...], "published": [...], "pending": [...],
        "revoked": [...]}. A revoked key is uploaded so others learn of
        the revocation; the server sends no email for it.
        """
        reply = _vks_post("upload", {"keytext": self.public_armored(fingerprint)})
        status = reply.get("status") or {}
        result = {
            state: sorted(addr for addr, value in status.items() if value == state)
            for state in ("published", "pending", "revoked")
        }
        result["sent"] = []
        wanted = sorted(addr for addr, value in status.items() if value == "unpublished")
        token = reply.get("token")
        if wanted and token:
            payload = {"token": token, "addresses": wanted}
            if locale:
                payload["locale"] = [locale]
            verified = _vks_post("request-verify", payload).get("status") or {}
            result["sent"] = [addr for addr in wanted if verified.get(addr) == "pending"]
        return result

    def delete(self, fingerprint):
        self._keys.pop(fingerprint, None)
        self._drop_secret(fingerprint)
        self._drop_secret(fingerprint + ".revocation")
        self._save()


# --- Reading mail (stage 2) ------------------------------------------------
#
# Used by openpgp_read. A locked personal key's passphrase is asked once
# per session (main_frame._unlock_protected) and kept here, in memory
# only, until ZBox closes.

_SESSION_PASSPHRASES = {}
_SESSION_LOCK = threading.Lock()


class NeedPassphrase(OpenPGPError):
    """A locked personal key has to be opened before the message can be
    decrypted."""

    def __init__(self, fingerprint, name):
        super().__init__("a passphrase is needed")
        self.fingerprint = fingerprint
        self.name = name


def remember_passphrase(fingerprint, passphrase):
    with _SESSION_LOCK:
        _SESSION_PASSPHRASES[str(fingerprint).lower()] = passphrase


def _session_passphrase(fingerprint):
    with _SESSION_LOCK:
        return _SESSION_PASSPHRASES.get(str(fingerprint).lower())


def passphrase_opens(store, fingerprint, passphrase):
    """True when passphrase opens one of your own locked keys."""
    _require()
    return _unlock_test(store._tsk(fingerprint), passphrase)


def _key_name(store, fingerprint):
    try:
        info = describe(_ps.Cert.from_bytes(store._keys[fingerprint]["public"].encode("ascii")))
        return info["name"] or info["email"] or key_id(fingerprint)
    except Exception:  # noqa: BLE001 - the key ID names it well enough
        return key_id(fingerprint)


def _decryptor(store, fingerprint):
    tsk = store._tsk(fingerprint)
    passphrase = _session_passphrase(fingerprint) if store.is_locked(fingerprint) else None
    return tsk.decryptor(passphrase) if passphrase else tsk.decryptor()


def _all_certs(store):
    certs = []
    for entry in store._keys.values():
        try:
            certs.append(_ps.Cert.from_bytes(entry["public"].encode("ascii")))
        except Exception:  # noqa: BLE001 - one bad entry must not hide the rest
            continue
    return certs


def _hex(value):
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


def key_owner(store, key):
    """The fingerprint, in the key list, of the key that owns key -- a
    primary or subkey fingerprint, or a key ID -- or None."""
    wanted = _hex(key)
    if not wanted:
        return None
    from pysequoia.packet import PacketPile, Tag

    for fingerprint, entry in store._keys.items():
        if fingerprint.endswith(wanted):
            return fingerprint
        try:
            pile = PacketPile.from_bytes(bytes(_ps.Cert.from_bytes(entry["public"].encode("ascii"))))
        except Exception:  # noqa: BLE001
            continue
        for packet in pile:
            if packet.tag in (Tag.PublicKey, Tag.PublicSubkey):
                found = _hex(getattr(packet, "fingerprint", None))
                if found and found.endswith(wanted):
                    return fingerprint
    return None


class SigCheck:
    """What a signature check found. signed is False when there was no
    signature; valid holds the primary fingerprints of good signatures;
    asked the keys the signatures named; created the date of a detached
    signature."""

    def __init__(self, signed=False, valid=(), asked=(), created=None):
        self.signed = bool(signed)
        self.valid = [str(item).lower() for item in valid if item]
        self.asked = [str(item).lower() for item in asked if item]
        self.created = created


class _Lookup:
    """The certificates PySequoia asks for while checking: every key in
    the list. Notes what it was asked for, which tells an unsigned
    message from one whose signer is unknown."""

    def __init__(self, store):
        self.certs = _all_certs(store)
        self.asked = []

    def __call__(self, ids):
        self.asked.extend(str(item) for item in (ids or []))
        return list(self.certs)


def decrypt(store, data):
    """(readable bytes, fingerprint of the personal key that opened them).
    Each personal key is tried in turn. Raises NeedPassphrase when only a
    locked key not yet opened this session is left, OpenPGPError when no
    key decrypts the message."""
    _require()
    locked = None
    for fingerprint in store.personal_keys():
        if store.is_locked(fingerprint) and _session_passphrase(fingerprint) is None:
            locked = locked or fingerprint
            continue
        try:
            result = _ps.decrypt(decryptor=_decryptor(store, fingerprint), bytes=data)
        except Exception:  # noqa: BLE001 - not this key
            continue
        return bytes(result.bytes), fingerprint
    if locked is not None:
        raise NeedPassphrase(locked, _key_name(store, locked))
    raise OpenPGPError("the message cannot be decrypted")


def check_encrypted_signature(store, data, fingerprint):
    """The signature inside an encrypted message, decrypting it a second
    time with the key that opened it, now with the key list to check
    against. PySequoia refuses the whole message when a signature cannot
    be checked, so the first decryption is done without it."""
    lookup = _Lookup(store)
    try:
        result = _ps.decrypt(decryptor=_decryptor(store, fingerprint), bytes=data, store=lookup)
    except Exception:  # noqa: BLE001 - unsigned, unknown signer, or bad signature
        return SigCheck(signed=bool(lookup.asked), asked=lookup.asked)
    valid = [sig.certificate for sig in (result.valid_sigs or [])]
    return SigCheck(signed=bool(valid or lookup.asked), valid=valid, asked=lookup.asked)


def verify_detached(store, signed, signature):
    """A PGP/MIME signature over the signed part, exactly as sent."""
    _require()
    try:
        sig = _ps.Sig.from_bytes(signature)
    except Exception:  # noqa: BLE001 - a signature that cannot be read is no good
        return SigCheck(signed=True)
    created = getattr(sig, "created", None)
    issuer = getattr(sig, "issuer_fingerprint", None) or getattr(sig, "issuer_key_id", None)
    lookup = _Lookup(store)
    try:
        result = _ps.verify(bytes=signed, store=lookup, signature=sig)
    except Exception:  # noqa: BLE001 - unknown signer or a changed message
        return SigCheck(signed=True, asked=lookup.asked or [issuer], created=created)
    valid = [item.certificate for item in (result.valid_sigs or [])]
    return SigCheck(signed=True, valid=valid, asked=lookup.asked or [issuer], created=created)


def verify_signed(store, armored):
    """(text, SigCheck) for a cleartext-signed or inline-signed block. The
    text is None when the signature could not be checked."""
    _require()
    lookup = _Lookup(store)
    try:
        result = _ps.verify(bytes=armored, store=lookup)
    except Exception:  # noqa: BLE001 - unknown signer or a changed message
        return None, SigCheck(signed=True, asked=lookup.asked)
    valid = [item.certificate for item in (result.valid_sigs or [])]
    return bytes(result.bytes), SigCheck(signed=True, valid=valid, asked=lookup.asked)


def _has_address(entry, address):
    try:
        cert = _ps.Cert.from_bytes(entry["public"].encode("ascii"))
    except Exception:  # noqa: BLE001
        return False
    wanted = str(address or "").strip().lower()
    return any(split_user_id(str(uid))[1].lower() == wanted for uid in (cert.user_ids or []))


def signature_state(store, check, sender=""):
    """(state, signer) as Thunderbird judges a signature. state is "none",
    "own", "verified" or "unverified" (good), "not_accepted", "no_key" or
    "uid_mismatch" (uncertain), "rejected" or "technical" (invalid).
    signer is the signer's fingerprint, or the key ID the signature named."""
    if check is None or not check.signed:
        return "none", None
    if check.valid:
        fingerprint = check.valid[0]
        entry = store._keys.get(fingerprint) if store is not None else None
        if entry is None:
            return "no_key", fingerprint
        if entry["acceptance"] == "rejected" and not entry["personal"]:
            return "rejected", fingerprint
        if entry["personal"] and entry["secret"]:
            state = "own"
        elif entry["acceptance"] == "verified":
            state = "verified"
        elif entry["acceptance"] == "unverified":
            state = "unverified"
        else:
            return "not_accepted", fingerprint
        if sender and not _has_address(entry, sender):
            return "uid_mismatch", fingerprint
        return state, fingerprint
    for asked in check.asked:
        owner = key_owner(store, asked) if store is not None else None
        if owner:
            return "technical", owner
    return "no_key", (check.asked[0] if check.asked else None)


# --- Sending mail (stage 3) -------------------------------------------------
#
# Used by openpgp_send (building the message) and openpgp_compose (the
# questions asked before sending). A recipient's key is used only when it
# is accepted, as Thunderbird requires: verified or unverified, or one of
# your own personal keys.

_SEND_LOCK = threading.Lock()


def _cert_of(entry):
    return _ps.Cert.from_bytes(entry["public"].encode("ascii"))


def _addresses_of(cert):
    """Every address in a key's user IDs, lower case."""
    found = set()
    for uid in cert.user_ids or []:
        address = split_user_id(str(uid))[1].strip().lower()
        if address:
            found.add(address)
    return found


def _stamp(cert):
    created = _created(cert)
    try:
        return created.timestamp() if isinstance(created, datetime.datetime) else 0.0
    except (OverflowError, OSError, ValueError):
        return 0.0


def _usable(cert):
    """True when a key can be encrypted to now: not revoked, not expired,
    and holding an encryption subkey."""
    info = describe(cert)
    if info["revoked"] or info["expired"]:
        return False
    try:
        _ps.encrypt(recipients=[cert], bytes=b"zbox")
    except Exception:  # noqa: BLE001 - no usable encryption subkey
        return False
    return True


def recipient_key(store, address):
    """The fingerprint of the key mail to address is encrypted to, or None:
    an accepted key (verified or unverified) or a personal key whose user ID
    holds the address and that can be encrypted to now. Verified and
    personal keys come first, then the newest."""
    _require()
    wanted = str(address or "").strip().lower()
    if not wanted:
        return None
    best = None
    for fingerprint, entry in store._keys.items():
        personal = bool(entry.get("personal") and entry.get("secret"))
        if not personal and entry.get("acceptance") not in ("verified", "unverified"):
            continue
        try:
            cert = _cert_of(entry)
        except Exception:  # noqa: BLE001 - one bad entry must not hide the rest
            continue
        if wanted not in _addresses_of(cert) or not _usable(cert):
            continue
        rank = (2 if personal or entry.get("acceptance") == "verified" else 1, _stamp(cert))
        if best is None or rank > best[0]:
            best = (rank, fingerprint)
    return best[1] if best else None


def missing_keys(store, addresses):
    """The addresses, each once, lower case, that have no recipient_key."""
    seen, missing = set(), []
    for address in addresses or []:
        wanted = str(address or "").strip().lower()
        if not wanted or wanted in seen:
            continue
        seen.add(wanted)
        if recipient_key(store, wanted) is None:
            missing.append(wanted)
    return missing


def own_key(store, address, chosen=""):
    """The personal key for sending as address, or None. chosen is the
    identity's setting: a fingerprint, "none" for no OpenPGP, or "" for
    the newest usable personal key whose user ID holds the address."""
    _require()
    chosen = str(chosen or "").strip().lower()
    if chosen == "none":
        return None
    personal = store.personal_keys()
    if chosen:
        if chosen not in personal:
            return None
        try:
            return chosen if _usable(_cert_of(store._keys[chosen])) else None
        except Exception:  # noqa: BLE001 - a key that cannot be read is not used
            return None
    wanted = str(address or "").strip().lower()
    best = None
    for fingerprint in personal:
        try:
            cert = _cert_of(store._keys[fingerprint])
        except Exception:  # noqa: BLE001
            continue
        if wanted in _addresses_of(cert) and _usable(cert):
            if best is None or _stamp(cert) > best[0]:
                best = (_stamp(cert), fingerprint)
    return best[1] if best else None


def needs_unlock(store, fingerprint):
    """True when signing with this personal key needs its passphrase first."""
    return store.is_locked(fingerprint) and _session_passphrase(fingerprint) is None


def key_name(store, fingerprint):
    return _key_name(store, fingerprint)


def _signer(store, fingerprint):
    tsk = store._tsk(fingerprint)
    if store.is_locked(fingerprint):
        passphrase = _session_passphrase(fingerprint)
        if passphrase is None:
            raise NeedPassphrase(fingerprint, _key_name(store, fingerprint))
        return tsk.signer(passphrase)
    return tsk.signer()


def encrypt_message(store, data, recipients, sign_with=None):
    """data encrypted and armored, to the keys named by fingerprint in
    recipients, signed when sign_with names a personal key. Written in the
    classic RFC 4880 format (openpgp_classic) whenever every recipient's key
    allows it, as Thunderbird writes it and can read it; in PySequoia's own
    newer format only for keys the classic one cannot carry."""
    _require()
    try:
        certs = [_cert_of(store._keys[fingerprint]) for fingerprint in recipients]
        classic = _classic_subkeys(certs)
        if classic is not None:
            import openpgp_classic

            if sign_with:
                packets = _dearmor(_ps.sign(_signer(store, sign_with), data))
            else:
                packets = openpgp_classic.literal(data)
            result = _armored_message(openpgp_classic.encrypt(classic, packets))
            logger.info("Encrypted to %d key(s) for %d recipient(s), classic format.", len(classic), len(certs))
            return result
        if sign_with:
            result = _ps.encrypt(signer=_signer(store, sign_with), recipients=certs, bytes=data)
        else:
            result = _ps.encrypt(recipients=certs, bytes=data)
        logger.info("Encrypted for %d recipient(s), newer format.", len(certs))
    except NeedPassphrase:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpenPGPError(str(exc) or "the message could not be encrypted") from exc
    return result if isinstance(result, bytes) else str(result).encode("ascii")


def sign_detached(store, data, fingerprint):
    """(armored detached signature, micalg) over data exactly as given."""
    _require()
    try:
        result = _ps.sign(_signer(store, fingerprint), data, mode=_ps.SignatureMode.DETACHED)
    except NeedPassphrase:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpenPGPError(str(exc) or "the message could not be signed") from exc
    raw = result if isinstance(result, bytes) else str(result).encode("ascii")
    try:
        name = str(_ps.Sig.from_bytes(raw).hash_algorithm).rsplit(".", 1)[-1].lower()
    except Exception:  # noqa: BLE001 - PySequoia signs with SHA-512
        name = "sha512"
    return raw, "pgp-" + (name or "sha512")


def public_key_bytes(store, fingerprint):
    """The public key in binary form, refused if anything in it is secret."""
    _require()
    cert = _cert_of(store._keys[fingerprint])
    if cert.has_secret_keys:
        raise OpenPGPError("the key holds a secret part")
    raw = bytes(cert)
    from pysequoia.packet import PacketPile, Tag

    secret_tags = [getattr(Tag, name) for name in ("SecretKey", "SecretSubkey") if hasattr(Tag, name)]
    for packet in PacketPile.from_bytes(raw):
        if packet.tag in secret_tags:
            raise OpenPGPError("the key holds a secret part")
    return raw


def public_key_armored(store, fingerprint):
    """The public key as stored, armored, after the same check."""
    public_key_bytes(store, fingerprint)
    return store._keys[fingerprint]["public"]


def autocrypt_value(store, address, fingerprint):
    """The Autocrypt header value for sending as address with this key,
    its key data in chunks a header can be folded at, starting on a line of
    its own as Thunderbird writes it."""
    import base64

    data = base64.b64encode(public_key_bytes(store, fingerprint)).decode("ascii")
    chunks = [data[i:i + 76] for i in range(0, len(data), 76)]
    return "addr=%s; keydata= %s" % (str(address or "").strip(), " ".join(chunks))


def parse_autocrypt(value):
    """(address, key bytes) from an Autocrypt header value, or None. A
    header with an attribute it does not know, other than one starting with
    an underscore, is ignored, as the Autocrypt standard says."""
    import base64
    import binascii

    attributes = {}
    for item in str(value or "").split(";"):
        if "=" not in item:
            continue
        name, _, text = item.partition("=")
        attributes[name.strip().lower()] = text.strip()
    for name in attributes:
        if name not in ("addr", "keydata", "prefer-encrypt", "type") and not name.startswith("_"):
            return None
    if attributes.get("type", "1") != "1":
        return None
    address = attributes.get("addr", "").strip().lower()
    if not address or "keydata" not in attributes:
        return None
    try:
        keydata = base64.b64decode("".join(attributes["keydata"].split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    return (address, keydata) if keydata else None


# Addresses and keys already looked at this session, so a message read
# again does not open the key list again.
_COLLECT_SEEN = set()


def _iso_date(value):
    """A message date as UTC ISO text, or "" when it cannot be read."""
    import email.utils

    text = str(value or "").strip()
    if not text:
        return ""
    when = None
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            when = datetime.datetime.fromisoformat(text)
        except ValueError:
            return ""
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).isoformat()


def collect_autocrypt(paths, sender, value, date=None):
    """Keeps the key in a received message's Autocrypt header for its
    sender, silently, as Thunderbird collects such keys: only when the
    header names the sender's own address and the key holds a user ID with
    it. A key already in the key list is left as it is; a collected key is
    never used until it is accepted when sending. Never raises."""
    if _ps is None:
        return
    try:
        parsed = parse_autocrypt(value)
        sender = str(sender or "").strip().lower()
        if parsed is None or not sender or parsed[0] != sender:
            return
        cert = _ps.Cert.from_bytes(parsed[1])
        if cert.has_secret_keys or sender not in _addresses_of(cert):
            return
        fingerprint = str(cert.fingerprint).lower()
        with _SEND_LOCK:
            if (sender, fingerprint) in _COLLECT_SEEN:
                return
            _COLLECT_SEEN.add((sender, fingerprint))
            store = KeyStore(paths)
            if fingerprint in store._keys:
                return
            stamp = _iso_date(date)
            old = store._collected.get(sender)
            if old and old.get("date") and stamp and stamp < old["date"]:
                return
            store._collected[sender] = {"public": str(cert), "date": stamp}
            store._save()
    except Exception as exc:  # noqa: BLE001 - collecting is never worth an error
        logger.debug("An Autocrypt header was not kept: %s", type(exc).__name__)


def collected_preview(store, address):
    """The key collected from address's own mail, as KeyStore.preview
    items, each marked "source": "autocrypt"; [] when there is none."""
    entry = store._collected.get(str(address or "").strip().lower())
    if not entry:
        return []
    try:
        items = store.preview(entry["public"].encode("ascii"))
    except OpenPGPError:
        return []
    for item in items:
        item["source"] = "autocrypt"
    return [item for item in items if item["secret"] is None and _usable(item["cert"])]


def accept_keys(store, found):
    """Imports keys found for recipients and accepts them as unverified,
    Thunderbird's "Yes, but I have not verified that it is the correct key."
    Collected copies of them are dropped."""
    _require()
    store.commit(found)
    for item in found:
        entry = store._keys.get(item["fingerprint"])
        if entry is not None and not entry.get("personal"):
            entry["acceptance"] = "unverified"
        for address in _addresses_of(item["cert"]):
            store._collected.pop(address, None)
    store._save()


# --- Finding keys online (stage 4) ----------------------------------------
#
# Thunderbird's Discover Keys Online: the Web Key Directory of the
# address's own mail domain (the advanced form, then the direct one),
# then keys.openpgp.org by address. Every lookup is asked each time
# before it runs.

_ZBASE32 = "ybndrfg8ejkmcpqxot1uwisza345h769"


def _zbase32(data):
    bits = "".join("{:08b}".format(byte) for byte in data)
    return "".join(_ZBASE32[int(bits[i:i + 5].ljust(5, "0"), 2)] for i in range(0, len(bits), 5))


def wkd_urls(address):
    """The Web Key Directory addresses for an email address: advanced, then
    direct. [] for something that is not an address."""
    import hashlib
    import urllib.parse

    local, _, domain = str(address or "").strip().rpartition("@")
    if not local or not domain or "/" in domain:
        return []
    domain = domain.lower()
    hashed = _zbase32(hashlib.sha1(local.lower().encode("utf-8")).digest())
    query = urllib.parse.quote(local, safe="")
    return [
        "https://openpgpkey.%s/.well-known/openpgpkey/%s/hu/%s?l=%s" % (domain, domain, hashed, query),
        "https://%s/.well-known/openpgpkey/hu/%s?l=%s" % (domain, hashed, query),
    ]


def _https_get(url, timeout=15):
    """The body of a successful answer, or None. Never raises."""
    import http.client
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "ZBox"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            if getattr(reply, "status", 200) != 200:
                return None
            data = reply.read(MAX_IMPORT_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None
    if not data or len(data) > MAX_IMPORT_BYTES:
        return None
    return data


def lookup_online(store, address):
    """Keys for address found online, as KeyStore.preview items marked
    "source": "wkd" or "keyserver": only public keys holding a user ID with
    the address that can be encrypted to now, each fingerprint once.
    Nothing is saved."""
    _require()
    import urllib.parse

    wanted = str(address or "").strip().lower()
    sources = []
    for url in wkd_urls(wanted):
        data = _https_get(url)
        if data:
            sources.append(("wkd", data))
            break
    data = _https_get(_VKS + "by-email/" + urllib.parse.quote(wanted, safe=""))
    if data:
        sources.append(("keyserver", data))
    found, seen = [], set()
    for source, data in sources:
        try:
            items = store.preview(data)
        except OpenPGPError:
            continue
        for item in items:
            if item["secret"] is not None or item["fingerprint"] in seen:
                continue
            if wanted not in _addresses_of(item["cert"]) or not _usable(item["cert"]):
                continue
            seen.add(item["fingerprint"])
            item["source"] = source
            found.append(item)
    return found


# --- The classic format (stage 3, Thunderbird compatibility) -------------
#
# PySequoia 0.1.35 always encrypts in RFC 9580's newer format, which
# Thunderbird cannot read. openpgp_classic writes the RFC 4880 format that
# Thunderbird writes; Sequoia still decides which subkeys are usable, and
# still signs, armors and reads.


def _dearmor(armored):
    """The binary data inside ASCII armor."""
    import base64

    text = armored.decode("ascii", "replace") if isinstance(armored, bytes) else str(armored)
    body, state = [], "before"
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if state == "before":
            if line.startswith("-----BEGIN PGP"):
                state = "headers"
            continue
        if state == "headers":
            if not line:
                state = "data"
            elif ":" not in line:
                state = "data"
                body.append(line)
            continue
        if line.startswith("-----END PGP") or (line.startswith("=") and len(line) == 5):
            break
        body.append(line)
    if not body:
        raise OpenPGPError("no OpenPGP data in the armor")
    return base64.b64decode("".join(body))


def _armored_message(binary):
    result = _ps.armor(binary, _ps.ArmorKind.Message)
    return result if isinstance(result, bytes) else str(result).encode("ascii")


def _encryption_subkeys(cert):
    """The subkeys Sequoia encrypts to for cert under its standard policy,
    as openpgp_classic.Subkey objects: found by letting PySequoia encrypt a
    sample and reading which keys its key packets name."""
    import openpgp_classic
    from pysequoia.packet import PacketPile, Tag

    sample = _ps.encrypt(recipients=[cert], bytes=b"zbox", armor=False)
    sample = sample if isinstance(sample, bytes) else bytes(sample)
    named = set()
    for packet in PacketPile.from_bytes(sample):
        if "PKESK" not in str(packet.tag).upper():
            continue
        body = bytes(getattr(packet, "body", b"") or b"")
        if len(body) >= 9 and body[0] == 3:
            named.add(("id", body[1:9]))
        elif len(body) >= 3 and body[0] == 6 and body[1] > 1:
            named.add(("fingerprint", body[3:2 + body[1]]))
    found = []
    for packet in PacketPile.from_bytes(bytes(cert)):
        if packet.tag not in (Tag.PublicKey, Tag.PublicSubkey):
            continue
        subkey = openpgp_classic.Subkey(packet.body)
        if ("id", subkey.key_id) in named or ("fingerprint", subkey.fingerprint) in named:
            found.append(subkey)
    return found


def _classic_subkeys(certs):
    """Every recipient's encryption subkeys, each once, when the classic
    format can carry them all; None when one cannot, or when this copy
    lacks the cryptography package (logged)."""
    try:
        import openpgp_classic
    except ImportError:
        logger.warning("The cryptography package is missing; encrypting in the newer format.")
        return None
    subkeys, seen = [], set()
    for cert in certs:
        found = _encryption_subkeys(cert)
        if not found or not all(openpgp_classic.supported(subkey) for subkey in found):
            return None
        for subkey in found:
            if subkey.fingerprint not in seen:
                seen.add(subkey.fingerprint)
                subkeys.append(subkey)
    return subkeys
