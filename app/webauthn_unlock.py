"""
Windows' own WebAuthn API (webauthn.dll), reached with ctypes.

Why this exists: ZBox ships no third-party crypto or FIDO library --
the rule for this project is wxPython plus the standard library, with
pywin32 for the Windows APIs. Windows has had a first-class WebAuthn
platform API since 1903, and it is the same one Edge and Chrome call.
It drives the whole authenticator experience itself: the Windows
Hello prompt, the "touch your security key" dialog, PIN entry,
retries, cancellation. So this file is a binding, not an
implementation -- there is no FIDO protocol logic here at all.

Same lazy-binding shape as win_crypto: nothing is resolved at import,
so importing this module on a machine without webauthn.dll (an older
Windows, or a Linux box running the tests) does not raise.
is_available() reports the truth and the callers fall back to the
master password.

Scope, stated plainly because it is easy to assume otherwise: an
assertion from here proves that the authenticator was present and the
user verified themselves to it. It does not hand back a key, and
nothing in ZBox is encrypted with it. It is an access gate in front
of the app, and the master password remains the only thing that can
actually open the secret store on a computer that has never seen this
folder before. See unlock_methods for how that gate is used, and
dpapi_secret_store for what actually protects the secrets.
"""

import ctypes
import json
import os
from base64 import urlsafe_b64encode
from ctypes import wintypes
import lang

# How long the Windows prompt stays up waiting for a fingerprint, a
# PIN or a touch on a key. A minute is what browsers use.
TIMEOUT_MS = 60000

CREDENTIAL_TYPE = "public-key"
_HASH_ALGORITHM = "SHA-256"

# COSE algorithm identifiers. Both are offered at registration
# because security keys in circulation support one or the other.
_COSE_ES256 = -7
_COSE_RS256 = -257

ATTACHMENT_ANY = 0
ATTACHMENT_PLATFORM = 1
ATTACHMENT_CROSS_PLATFORM = 2

_UV_REQUIRED = 1
_ATTESTATION_NONE = 1

_S_OK = 0

# HRESULTs worth telling apart. Everything else is reported with the
# name Windows itself gives it.
_CANCELLED = (0x800704C7, 0x80090036)
_TIMEOUT = (0x800705B4,)
_NOT_FOUND = (0x80090011, 0x80070490)

_library = None
_load_error = None


class WebAuthnUnavailable(RuntimeError):
    """webauthn.dll is missing or too old, so passkeys and security
    keys cannot be used on this computer."""


class WebAuthnCancelled(RuntimeError):
    """The user dismissed the Windows prompt, or it timed out."""


class WebAuthnFailed(RuntimeError):
    """The authenticator refused, or Windows reported an error."""

    def __init__(self, message, hresult=0):
        super().__init__(message)
        self.hresult = hresult


# --- structures -------------------------------------------------------
#
# Only the fields ZBox uses are commented. Every options struct is
# filled at its VERSION_1 shape and declared with dwVersion = 1:
# Windows reads exactly as far as the version says, so a struct
# written this way works unchanged on every build that has the API at
# all, and none of the later versions add anything a local unlock
# gate needs.

class _RpEntity(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("pwszId", wintypes.LPCWSTR),
        ("pwszName", wintypes.LPCWSTR),
        ("pwszIcon", wintypes.LPCWSTR),
    ]


class _UserEntity(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("cbId", wintypes.DWORD),
        ("pbId", ctypes.POINTER(ctypes.c_ubyte)),
        ("pwszName", wintypes.LPCWSTR),
        ("pwszIcon", wintypes.LPCWSTR),
        ("pwszDisplayName", wintypes.LPCWSTR),
    ]


class _ClientData(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("cbClientDataJSON", wintypes.DWORD),
        ("pbClientDataJSON", ctypes.POINTER(ctypes.c_ubyte)),
        ("pwszHashAlgId", wintypes.LPCWSTR),
    ]


class _CoseParameter(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("pwszCredentialType", wintypes.LPCWSTR),
        ("lAlg", wintypes.LONG),
    ]


class _CoseParameters(ctypes.Structure):
    _fields_ = [
        ("cCredentialParameters", wintypes.DWORD),
        ("pCredentialParameters", ctypes.POINTER(_CoseParameter)),
    ]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("cbId", wintypes.DWORD),
        ("pbId", ctypes.POINTER(ctypes.c_ubyte)),
        ("pwszCredentialType", wintypes.LPCWSTR),
    ]


class _Credentials(ctypes.Structure):
    _fields_ = [
        ("cCredentials", wintypes.DWORD),
        ("pCredentials", ctypes.POINTER(_Credential)),
    ]


class _Extensions(ctypes.Structure):
    _fields_ = [
        ("cExtensions", wintypes.DWORD),
        ("pExtensions", ctypes.c_void_p),
    ]


class _MakeCredentialOptions(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("dwTimeoutMilliseconds", wintypes.DWORD),
        # At registration this list is the EXCLUDE list, not an allow
        # list. Left empty: registering the same authenticator twice
        # is harmless here, and an exclude list turns that into an
        # opaque Windows error instead.
        ("CredentialList", _Credentials),
        ("Extensions", _Extensions),
        ("dwAuthenticatorAttachment", wintypes.DWORD),
        ("bRequireResidentKey", wintypes.BOOL),
        ("dwUserVerificationRequirement", wintypes.DWORD),
        ("dwAttestationConveyancePreference", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
    ]


class _GetAssertionOptions(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("dwTimeoutMilliseconds", wintypes.DWORD),
        # Here the same list IS the allow list: the credentials ZBox
        # has on file, so an authenticator that was never registered
        # cannot answer.
        ("CredentialList", _Credentials),
        ("Extensions", _Extensions),
        ("dwAuthenticatorAttachment", wintypes.DWORD),
        ("dwUserVerificationRequirement", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
    ]


class _CredentialAttestation(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("pwszFormatType", wintypes.LPCWSTR),
        ("cbAuthenticatorData", wintypes.DWORD),
        ("pbAuthenticatorData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAttestation", wintypes.DWORD),
        ("pbAttestation", ctypes.POINTER(ctypes.c_ubyte)),
        ("dwAttestationDecodeType", wintypes.DWORD),
        ("pbAttestationDecode", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAttestationObject", wintypes.DWORD),
        ("pbAttestationObject", ctypes.POINTER(ctypes.c_ubyte)),
        # The two fields ZBox actually keeps.
        ("cbCredentialId", wintypes.DWORD),
        ("pbCredentialId", ctypes.POINTER(ctypes.c_ubyte)),
        ("Extensions", _Extensions),
        ("dwUsedTransport", wintypes.DWORD),
    ]


class _Assertion(ctypes.Structure):
    _fields_ = [
        ("dwVersion", wintypes.DWORD),
        ("cbAuthenticatorData", wintypes.DWORD),
        ("pbAuthenticatorData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbSignature", wintypes.DWORD),
        ("pbSignature", ctypes.POINTER(ctypes.c_ubyte)),
        # Which registered credential answered.
        ("Credential", _Credential),
        ("cbUserId", wintypes.DWORD),
        ("pbUserId", ctypes.POINTER(ctypes.c_ubyte)),
    ]


# --- binding ----------------------------------------------------------

def _bind():
    global _library, _load_error
    if _library is not None:
        return _library
    if _load_error is not None:
        raise WebAuthnUnavailable(_load_error)
    try:
        lib = ctypes.WinDLL("webauthn.dll")
    except (OSError, AttributeError) as exc:
        _load_error = str(exc)
        raise WebAuthnUnavailable(_load_error)

    try:
        lib.WebAuthNGetApiVersionNumber.restype = wintypes.DWORD
        lib.WebAuthNGetApiVersionNumber.argtypes = []

        lib.WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable.restype = (
            wintypes.LONG
        )
        lib.WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable.argtypes = [
            ctypes.POINTER(wintypes.BOOL)
        ]

        lib.WebAuthNAuthenticatorMakeCredential.restype = wintypes.LONG
        lib.WebAuthNAuthenticatorMakeCredential.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_RpEntity),
            ctypes.POINTER(_UserEntity),
            ctypes.POINTER(_CoseParameters),
            ctypes.POINTER(_ClientData),
            ctypes.POINTER(_MakeCredentialOptions),
            ctypes.POINTER(ctypes.POINTER(_CredentialAttestation)),
        ]

        lib.WebAuthNFreeCredentialAttestation.restype = None
        lib.WebAuthNFreeCredentialAttestation.argtypes = [
            ctypes.POINTER(_CredentialAttestation)
        ]

        lib.WebAuthNAuthenticatorGetAssertion.restype = wintypes.LONG
        lib.WebAuthNAuthenticatorGetAssertion.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            ctypes.POINTER(_ClientData),
            ctypes.POINTER(_GetAssertionOptions),
            ctypes.POINTER(ctypes.POINTER(_Assertion)),
        ]

        lib.WebAuthNFreeAssertion.restype = None
        lib.WebAuthNFreeAssertion.argtypes = [ctypes.POINTER(_Assertion)]

        lib.WebAuthNGetErrorName.restype = wintypes.LPCWSTR
        lib.WebAuthNGetErrorName.argtypes = [wintypes.LONG]
    except AttributeError as exc:
        # The DLL exists but is missing an export: a Windows too old
        # to have the API in a usable shape.
        _load_error = "webauthn.dll is too old: %s" % exc
        raise WebAuthnUnavailable(_load_error)

    _library = lib
    return lib


def is_available():
    """Whether this computer can do passkeys or security keys at
    all."""
    try:
        return _bind().WebAuthNGetApiVersionNumber() >= 1
    except (WebAuthnUnavailable, OSError):
        return False


def platform_authenticator_available():
    """Whether Windows Hello (fingerprint, face, or the PIN backing
    them) is set up on this computer. False does not stop a security
    key from working."""
    try:
        lib = _bind()
    except WebAuthnUnavailable:
        return False
    present = wintypes.BOOL(False)
    try:
        status = lib.WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable(
            ctypes.byref(present)
        )
    except OSError:
        return False
    return status == _S_OK and bool(present.value)


def _raise_for(lib, status, what):
    if status in _CANCELLED:
        raise WebAuthnCancelled("%s was cancelled." % what)
    if status in _TIMEOUT:
        raise WebAuthnCancelled("%s timed out." % what)
    try:
        name = lib.WebAuthNGetErrorName(status) or ""
    except Exception:  # noqa: BLE001
        name = ""
    if status in _NOT_FOUND:
        raise WebAuthnFailed(
            "That authenticator is not one of the ones registered with "
            "ZBox.", status,
        )
    raise WebAuthnFailed(
        "Windows reported %s (0x%08X)." % (name or "an error", status & 0xFFFFFFFF),
        status,
    )


def _bytes_buffer(raw):
    """A ctypes buffer over bytes. Held by the caller: the structs
    only point at it, so letting it fall out of scope while Windows
    is still reading is a use-after-free."""
    return (ctypes.c_ubyte * max(len(raw), 1)).from_buffer_copy(
        raw or b"\x00"
    )


def _pointer(buffer):
    return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))


def _client_data(kind, rp_id):
    """The client data blob, in the shape the API expects.

    A browser's copy of this is what a remote server checks the
    origin and challenge against. ZBox is both sides of this
    exchange and verifies nothing remotely, so the challenge is here
    to keep the structure well formed and to stop two runs producing
    byte-identical input, not as a security property. Said plainly
    rather than dressed up.
    """
    payload = json.dumps(
        {
            "type": kind,
            "challenge": urlsafe_b64encode(os.urandom(32))
            .decode("ascii").rstrip("="),
            "origin": "https://" + rp_id,
            "crossOrigin": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    buffer = _bytes_buffer(payload)
    data = _ClientData()
    data.dwVersion = 1
    data.cbClientDataJSON = len(payload)
    data.pbClientDataJSON = _pointer(buffer)
    data.pwszHashAlgId = _HASH_ALGORITHM
    return data, buffer


def _credential_list(credential_ids):
    """An allow list, plus the buffers it points into."""
    held = []
    if not credential_ids:
        return _Credentials(0, None), held

    array = (_Credential * len(credential_ids))()
    for index, raw in enumerate(credential_ids):
        buffer = _bytes_buffer(raw)
        held.append(buffer)
        array[index].dwVersion = 1
        array[index].cbId = len(raw)
        array[index].pbId = _pointer(buffer)
        array[index].pwszCredentialType = CREDENTIAL_TYPE
    held.append(array)

    credentials = _Credentials()
    credentials.cCredentials = len(credential_ids)
    credentials.pCredentials = ctypes.cast(
        array, ctypes.POINTER(_Credential)
    )
    return credentials, held


def register(hwnd, rp_id, rp_name, user_id, user_name, user_display_name,
             attachment=ATTACHMENT_PLATFORM, resident_key=True,
             timeout_ms=TIMEOUT_MS):
    """Runs the Windows registration prompt and returns the new
    credential's id as bytes.

    User verification is required, not merely preferred: an unlock
    method that a bare touch satisfies would be weaker than the
    master password it sits beside.
    """
    lib = _bind()

    rp = _RpEntity(1, rp_id, rp_name, None)

    user_buffer = _bytes_buffer(user_id)
    user = _UserEntity()
    user.dwVersion = 1
    user.cbId = len(user_id)
    user.pbId = _pointer(user_buffer)
    user.pwszName = user_name
    user.pwszIcon = None
    user.pwszDisplayName = user_display_name

    algorithms = (_CoseParameter * 2)()
    for index, algorithm in enumerate((_COSE_ES256, _COSE_RS256)):
        algorithms[index].dwVersion = 1
        algorithms[index].pwszCredentialType = CREDENTIAL_TYPE
        algorithms[index].lAlg = algorithm
    parameters = _CoseParameters(
        2, ctypes.cast(algorithms, ctypes.POINTER(_CoseParameter))
    )

    client_data, client_buffer = _client_data("webauthn.create", rp_id)

    options = _MakeCredentialOptions()
    options.dwVersion = 1
    options.dwTimeoutMilliseconds = timeout_ms
    options.CredentialList = _Credentials(0, None)
    options.Extensions = _Extensions(0, None)
    options.dwAuthenticatorAttachment = attachment
    options.bRequireResidentKey = bool(resident_key)
    options.dwUserVerificationRequirement = _UV_REQUIRED
    options.dwAttestationConveyancePreference = _ATTESTATION_NONE
    options.dwFlags = 0

    result = ctypes.POINTER(_CredentialAttestation)()
    status = lib.WebAuthNAuthenticatorMakeCredential(
        wintypes.HWND(hwnd),
        ctypes.byref(rp),
        ctypes.byref(user),
        ctypes.byref(parameters),
        ctypes.byref(client_data),
        ctypes.byref(options),
        ctypes.byref(result),
    )
    # Named so they visibly outlive the call above.
    del user_buffer, client_buffer, algorithms

    if status != _S_OK or not result:
        _raise_for(lib, status, lang.t('dialogs', 'unlock_registering', default="Registering this unlock method"))

    try:
        attestation = result.contents
        credential_id = bytes(bytearray(
            attestation.pbCredentialId[:attestation.cbCredentialId]
        ))
        transport = int(attestation.dwUsedTransport)
    finally:
        lib.WebAuthNFreeCredentialAttestation(result)

    if not credential_id:
        raise WebAuthnFailed("Windows returned no credential to save.")
    return credential_id, transport


def assert_identity(hwnd, rp_id, credential_ids,
                    attachment=ATTACHMENT_ANY, timeout_ms=TIMEOUT_MS):
    """Runs the Windows unlock prompt against the credentials on
    file, and returns the id of whichever one answered.

    The signature that comes back is deliberately not checked. There
    is no server here and no key to check it against -- ZBox never
    stored the public key, because a gate-only unlock has nothing to
    do with one. What is being trusted is the operating system's
    answer to "did the registered authenticator verify this user",
    which is the same thing that is being trusted when Windows Hello
    unlocks the desktop.
    """
    lib = _bind()
    if not credential_ids:
        raise WebAuthnFailed("No unlock methods are registered.")

    client_data, client_buffer = _client_data("webauthn.get", rp_id)
    credentials, held = _credential_list(credential_ids)

    options = _GetAssertionOptions()
    options.dwVersion = 1
    options.dwTimeoutMilliseconds = timeout_ms
    options.CredentialList = credentials
    options.Extensions = _Extensions(0, None)
    options.dwAuthenticatorAttachment = attachment
    options.dwUserVerificationRequirement = _UV_REQUIRED
    options.dwFlags = 0

    result = ctypes.POINTER(_Assertion)()
    status = lib.WebAuthNAuthenticatorGetAssertion(
        wintypes.HWND(hwnd),
        rp_id,
        ctypes.byref(client_data),
        ctypes.byref(options),
        ctypes.byref(result),
    )
    del client_buffer, held

    if status != _S_OK or not result:
        _raise_for(lib, status, "Unlocking")

    try:
        assertion = result.contents
        used = bytes(bytearray(
            assertion.Credential.pbId[:assertion.Credential.cbId]
        ))
    finally:
        lib.WebAuthNFreeAssertion(result)

    if used not in credential_ids:
        # Should not happen -- the allow list is what was offered --
        # so treat it as a refusal rather than quietly accepting an
        # answer from something ZBox never registered.
        raise WebAuthnFailed(
            "The authenticator that answered is not one ZBox has "
            "registered."
        )
    return used
