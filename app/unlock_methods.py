"""
The unlock methods ZBox will accept, and the file that remembers
them.

There are three, and any one of them is enough to unlock -- OR,
never AND:

  master password   already existed (dpapi_secret_store), and is
                    still the only one that is cryptographic. It is
                    what opens the secret store on a computer that
                    has never seen this folder before.
  passkey           Windows Hello on this computer: fingerprint,
                    face, or the PIN behind them.
  security key      a FIDO2 key on USB or NFC.

The master password is required before either of the other two can
be added at all. Not a policy decision dressed up as a technical
one: a passkey opens the app and nothing else, so a folder whose
only unlock method was a passkey would still be unreadable on any
other computer, and losing the machine that holds it would cost
every account with no way back. The master password is the floor
everything else stands on, so register() refuses without it and the
buttons that reach it are unavailable until it exists.

What the last two are, and what they are not. Both are gates in
front of the app: Windows verifies the user to the authenticator,
ZBox believes the answer and opens. Neither derives a key, and
nothing in the secret store is encrypted with either -- that is
still DPAPI plus, optionally, the master password. So a passkey
registered here does not make the folder portable, and cannot
substitute for the master password on a new computer: on a computer
that cannot unwrap the data key, there is nothing for a gate to let
you through to. Deriving a key from an authenticator is possible
(the FIDO2 hmac-secret extension) and is the natural next step when
the secrets themselves are re-keyed; it is deliberately not what
this file does today.

Registered methods live in config/unlock_methods.json, next to
secrets.dat. The credential ids in it are identifiers, not secrets:
they are useless without the authenticator that holds the private
key, which is the entire point of the design. Deleting the file
loses the passkeys (they can be registered again) and touches
nothing else.

Carrying the folder to another computer, which is the case this
whole project is built around: a security key travels with you and
keeps working anywhere it is plugged in, but a passkey does not. It
is Windows Hello on one specific machine, and on any other one the
credential simply does not exist -- so an entry left over from the
old computer is a button that can only ever fail. Every method
therefore records which machine registered it (the same fingerprint
dpapi_secret_store uses), is_usable_here() answers whether it can do
anything on this one, and reassign_passkey() replaces a stale entry
with a fresh passkey made here. Nothing is forgotten automatically:
a folder that travels back to the first computer would otherwise
arrive with its own passkey deleted by the trip.
"""

import json
import os
import uuid
from base64 import b64decode, b64encode
from datetime import datetime

import webauthn_unlock

# The relying-party id every ZBox credential is registered under.
# Windows wants a domain-shaped string; nothing resolves it and no
# network request is ever made with it. Changing it would orphan
# every credential already registered, so it does not change.
RP_ID = "zbox.local"
RP_NAME = "ZBox"

KIND_PASSKEY = "passkey"
KIND_SECURITY_KEY = "security_key"

KIND_LABELS = {
    KIND_PASSKEY: "Passkey (Windows Hello)",
    KIND_SECURITY_KEY: "Security key",
}

_FILE_NAME = "unlock_methods.json"
_FORMAT_VERSION = 1


class NoUnlockMethods(RuntimeError):
    """Nothing of this kind is registered, so there is nothing to
    ask for."""


class MasterPasswordRequired(RuntimeError):
    """A passkey or security key was asked for before a master
    password exists. See the module docstring for why that order is
    not negotiable."""


def _file(config_dir):
    return os.path.join(config_dir, _FILE_NAME)


def _load(config_dir):
    path = _file(config_dir)
    if not os.path.isfile(path):
        return {"version": _FORMAT_VERSION, "user_handle": "", "methods": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        # Same reasoning as the secret store: an unreadable file is
        # treated as empty rather than crashing the launch. The cost
        # is re-registering a passkey, not losing mail.
        return {"version": _FORMAT_VERSION, "user_handle": "", "methods": []}

    if not isinstance(data, dict):
        return {"version": _FORMAT_VERSION, "user_handle": "", "methods": []}
    methods = data.get("methods")
    return {
        "version": _FORMAT_VERSION,
        "user_handle": str(data.get("user_handle") or ""),
        "methods": [m for m in methods if isinstance(m, dict)]
        if isinstance(methods, list) else [],
    }


def _save(config_dir, store):
    path = _file(config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2)


def _user_handle(store):
    """The WebAuthn user id, made once and kept.

    It is random rather than derived from a Windows account name:
    a resident passkey stores this and shows it in the authenticator,
    and there is no reason for it to say anything about who the
    account belongs to.
    """
    handle = store.get("user_handle")
    if handle:
        return b64decode(handle)
    raw = os.urandom(32)
    store["user_handle"] = b64encode(raw).decode("ascii")
    return raw


def list_methods(config_dir):
    """Every registered method, newest last. Each is a plain dict
    with id, kind, name, machine and registered."""
    return list(_load(config_dir)["methods"])


def master_password_set(config_dir):
    """Whether the master password exists yet. Everything else in
    this file is gated on it."""
    try:
        import dpapi_secret_store

        return dpapi_secret_store.has_master_password(config_dir)
    except Exception:  # noqa: BLE001
        return False


def current_machine():
    """This computer's fingerprint, borrowed from the secret store so
    both files agree on what "this computer" means. Empty when it
    cannot be worked out, which is treated as "do not claim to
    know"."""
    try:
        import dpapi_secret_store

        return dpapi_secret_store.machine_id()
    except Exception:  # noqa: BLE001
        return ""


def is_usable_here(method):
    """Whether this method can actually unlock ZBox on this computer.

    A security key is a physical object: it works wherever it is
    plugged in, so it is always usable. A passkey is Windows Hello on
    the machine that made it and exists nowhere else.

    A method with no machine recorded is adopted rather than
    disabled, the same way dpapi_secret_store adopts a keyset written
    before machines were tracked: refusing to offer a method that
    might work is worse than offering one that turns out not to.
    """
    if method.get("kind") != KIND_PASSKEY:
        return True
    machine = method.get("machine")
    if not machine:
        return True
    return machine == current_machine()


def usable_methods(config_dir, kind=None):
    """The methods worth offering on this computer."""
    return [
        method for method in _load(config_dir)["methods"]
        if (kind is None or method.get("kind") == kind)
        and is_usable_here(method)
    ]


def foreign_passkeys(config_dir):
    """Passkeys belonging to a different computer -- the ones a
    carried folder arrives with."""
    return [
        method for method in _load(config_dir)["methods"]
        if method.get("kind") == KIND_PASSKEY and not is_usable_here(method)
    ]


def forget_foreign_passkeys(config_dir):
    """Drops every passkey that belongs to another computer and
    returns how many went.

    Only ever called because the user asked for it. Doing it
    automatically on arrival would mean a folder that travels to a
    second machine and back has quietly destroyed the first
    machine's own passkey on the way.
    """
    store = _load(config_dir)
    keep = [
        method for method in store["methods"]
        if method.get("kind") != KIND_PASSKEY or is_usable_here(method)
    ]
    removed = len(store["methods"]) - len(keep)
    if removed:
        store["methods"] = keep
        _save(config_dir, store)
    return removed


def has_methods(config_dir, kind=None):
    return any(
        kind is None or method.get("kind") == kind
        for method in _load(config_dir)["methods"]
    )


def available():
    """Whether this computer can register anything at all."""
    return webauthn_unlock.is_available()


def passkeys_supported():
    """Whether Windows Hello is set up here. A security key still
    works when this is False."""
    return webauthn_unlock.platform_authenticator_available()


def default_name(kind):
    if kind == KIND_PASSKEY:
        return "Windows Hello on %s" % (
            os.environ.get("COMPUTERNAME") or "this computer"
        )
    return "Security key"


def register(hwnd, config_dir, kind, name=None, account_label="ZBox"):
    """Runs the Windows registration prompt and records the result.

    A passkey is asked for as a resident (discoverable) platform
    credential, which is what makes it a passkey rather than a
    second factor. A security key is asked for cross-platform and
    non-resident, so keys without room for discoverable credentials
    -- most older ones -- still work.
    """
    if kind not in (KIND_PASSKEY, KIND_SECURITY_KEY):
        raise ValueError("Unknown unlock method: %r" % (kind,))
    if not master_password_set(config_dir):
        # Checked here and not only in the dialog: this is the rule
        # itself, and a rule enforced only by a greyed-out button is
        # enforced only until something else calls this function.
        raise MasterPasswordRequired(
            "Set a master password before adding a passkey or security "
            "key. Those unlock the app on this computer; the master "
            "password is what opens your saved account passwords "
            "anywhere else, and nothing can recover them without it."
        )

    store = _load(config_dir)
    user_id = _user_handle(store)

    if kind == KIND_PASSKEY:
        attachment = webauthn_unlock.ATTACHMENT_PLATFORM
        resident = True
    else:
        attachment = webauthn_unlock.ATTACHMENT_CROSS_PLATFORM
        resident = False

    credential_id, transport = webauthn_unlock.register(
        hwnd, RP_ID, RP_NAME, user_id,
        account_label, account_label,
        attachment=attachment, resident_key=resident,
    )

    method = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "name": (name or "").strip() or default_name(kind),
        "credential_id": b64encode(credential_id).decode("ascii"),
        "transport": transport,
        "machine": current_machine(),
        "machine_name": os.environ.get("COMPUTERNAME") or "",
        "registered": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    store["methods"].append(method)
    _save(config_dir, store)
    return method


def reassign_passkey(hwnd, config_dir, name=None, account_label="ZBox"):
    """Registers a passkey on this computer and forgets the ones
    that belong to other computers.

    This is the answer to carrying the folder somewhere new: the old
    machine's passkey cannot be used here and cannot be repaired, so
    it is replaced rather than left sitting in the list as a button
    that fails. Registration happens first and the old entries go
    only once it has succeeded -- a cancelled Windows prompt must
    leave the file exactly as it was, or a mistyped PIN would cost
    the passkey without providing a new one.

    Returns (new method, how many stale entries were dropped).
    """
    method = register(
        hwnd, config_dir, KIND_PASSKEY, name=name,
        account_label=account_label,
    )
    return method, forget_foreign_passkeys(config_dir)


def remove(config_dir, method_id):
    """Forgets one method. The credential is left on the
    authenticator itself -- Windows owns that, and there is no API
    for an application to delete someone's passkey -- but ZBox will
    no longer accept it, which is what removing it here means."""
    store = _load(config_dir)
    remaining = [m for m in store["methods"] if m.get("id") != method_id]
    if len(remaining) == len(store["methods"]):
        return False
    store["methods"] = remaining
    _save(config_dir, store)
    return True


def verify(hwnd, config_dir, kind=None):
    """Asks for one of the registered methods and returns whichever
    one answered. Raises NoUnlockMethods when there is nothing of
    that kind on file, and webauthn_unlock's own errors otherwise --
    WebAuthnCancelled for a dismissed prompt, WebAuthnFailed for a
    refusal."""
    methods = [
        method for method in _load(config_dir)["methods"]
        if (kind is None or method.get("kind") == kind)
        and is_usable_here(method)
    ]
    if not methods:
        raise NoUnlockMethods(
            "No unlock method of that kind is registered on this "
            "computer."
        )

    by_credential = {}
    for method in methods:
        try:
            by_credential[b64decode(method.get("credential_id", ""))] = method
        except (ValueError, TypeError):
            continue
    if not by_credential:
        raise NoUnlockMethods("The registered unlock methods are unreadable.")

    if kind == KIND_PASSKEY:
        attachment = webauthn_unlock.ATTACHMENT_PLATFORM
    elif kind == KIND_SECURITY_KEY:
        attachment = webauthn_unlock.ATTACHMENT_CROSS_PLATFORM
    else:
        attachment = webauthn_unlock.ATTACHMENT_ANY

    used = webauthn_unlock.assert_identity(
        hwnd, RP_ID, list(by_credential), attachment=attachment
    )
    return by_credential[used]
