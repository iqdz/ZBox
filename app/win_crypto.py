"""
AES-256-GCM through Windows' own CNG (bcrypt.dll), reached with
ctypes.

Why this exists: ZBox ships no third-party crypto library. The rule
for this project is wxPython plus the standard library (pywin32 for
the Windows APIs), and the standard library has PBKDF2 but no AES.
The alternatives were adding `cryptography` as a hard dependency, or
composing a cipher by hand out of hmac/hashlib. Calling the AES the
operating system already uses -- the same provider behind DPAPI and
BitLocker -- is neither.

Used by dpapi_secret_store to wrap the data key that encrypts every
account secret. Authenticated encryption matters there specifically:
a wrong master password has to fail as a clean tag mismatch rather
than returning plausible-looking garbage.

Everything is bound lazily inside the functions, so importing this
module on a machine without bcrypt.dll (a Linux box running the
tests, say) does not raise -- is_available() reports the truth and
the callers fall back.
"""

import ctypes
from ctypes import wintypes

TAG_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32

_STATUS_SUCCESS = 0

_bcrypt = None
_load_error = None


class CryptoUnavailable(RuntimeError):
    """bcrypt.dll could not be loaded, so AES is not reachable."""


class DecryptionFailed(ValueError):
    """The tag did not verify: wrong key, or the data was altered."""


class _AuthenticatedCipherModeInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wintypes.ULONG),
    ]


def _library():
    global _bcrypt, _load_error
    if _bcrypt is not None:
        return _bcrypt
    if _load_error is not None:
        raise CryptoUnavailable(_load_error)
    try:
        _bcrypt = ctypes.WinDLL("bcrypt.dll")
    except (OSError, AttributeError) as exc:
        _load_error = str(exc)
        raise CryptoUnavailable(_load_error)
    return _bcrypt


def is_available():
    try:
        _library()
        return True
    except CryptoUnavailable:
        return False


def _buffer(data):
    """A mutable ctypes buffer over bytes, plus its length."""
    array = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return array, len(data)


def _check(status, call):
    if status != _STATUS_SUCCESS:
        raise CryptoUnavailable(
            "%s failed with NTSTATUS 0x%08X" % (call, status & 0xFFFFFFFF)
        )


class _Key:
    """One AES-GCM key handle, opened and closed around a call.

    Deliberately not cached. These functions run a handful of times
    per launch (unwrapping the data key, then one decrypt per account
    secret), so keeping a provider handle alive to save microseconds
    would only widen the window in which a key sits in process memory.
    """

    def __init__(self, key):
        if len(key) != KEY_SIZE:
            raise ValueError("AES-256 needs a 32-byte key")
        lib = _library()
        self._lib = lib
        self._alg = ctypes.c_void_p()
        self._key = ctypes.c_void_p()

        _check(lib.BCryptOpenAlgorithmProvider(
            ctypes.byref(self._alg), "AES", None, 0
        ), "BCryptOpenAlgorithmProvider")

        mode = "ChainingModeGCM".encode("utf-16-le") + b"\x00\x00"
        mode_buffer, mode_len = _buffer(mode)
        _check(lib.BCryptSetProperty(
            self._alg, "ChainingMode", mode_buffer, mode_len, 0
        ), "BCryptSetProperty")

        key_buffer, key_len = _buffer(key)
        _check(lib.BCryptGenerateSymmetricKey(
            self._alg, ctypes.byref(self._key), None, 0,
            key_buffer, key_len, 0
        ), "BCryptGenerateSymmetricKey")

    def close(self):
        if self._key:
            self._lib.BCryptDestroyKey(self._key)
            self._key = ctypes.c_void_p()
        if self._alg:
            self._lib.BCryptCloseAlgorithmProvider(self._alg, 0)
            self._alg = ctypes.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _mode_info(nonce, aad, tag_buffer):
    info = _AuthenticatedCipherModeInfo()
    ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
    info.cbSize = ctypes.sizeof(info)
    info.dwInfoVersion = 1

    nonce_buffer, nonce_len = _buffer(nonce)
    info.pbNonce = ctypes.cast(nonce_buffer, ctypes.POINTER(ctypes.c_ubyte))
    info.cbNonce = nonce_len

    if aad:
        aad_buffer, aad_len = _buffer(aad)
        info.pbAuthData = ctypes.cast(aad_buffer, ctypes.POINTER(ctypes.c_ubyte))
        info.cbAuthData = aad_len
    else:
        aad_buffer = None

    info.pbTag = ctypes.cast(tag_buffer, ctypes.POINTER(ctypes.c_ubyte))
    info.cbTag = TAG_SIZE

    # The buffers have to outlive the struct that points into them,
    # so they are handed back to the caller to hold.
    return info, (nonce_buffer, aad_buffer)


def encrypt(key, nonce, plaintext, aad=b""):
    """Returns ciphertext + 16-byte tag, concatenated."""
    with _Key(key) as handle:
        tag_buffer = (ctypes.c_ubyte * TAG_SIZE)()
        info, _held = _mode_info(nonce, aad, tag_buffer)

        input_buffer, input_len = _buffer(plaintext)
        output = (ctypes.c_ubyte * max(input_len, 1))()
        written = wintypes.ULONG(0)

        _check(handle._lib.BCryptEncrypt(
            handle._key,
            input_buffer, input_len,
            ctypes.byref(info),
            None, 0,
            output, input_len,
            ctypes.byref(written),
            0,
        ), "BCryptEncrypt")

        return bytes(bytearray(output)[:written.value]) + bytes(bytearray(tag_buffer))


def decrypt(key, nonce, ciphertext_and_tag, aad=b""):
    """Reverses encrypt(). Raises DecryptionFailed on a bad tag."""
    if len(ciphertext_and_tag) < TAG_SIZE:
        raise DecryptionFailed("ciphertext is too short to hold a tag")
    ciphertext = ciphertext_and_tag[:-TAG_SIZE]
    tag = ciphertext_and_tag[-TAG_SIZE:]

    with _Key(key) as handle:
        tag_buffer = (ctypes.c_ubyte * TAG_SIZE).from_buffer_copy(tag)
        info, _held = _mode_info(nonce, aad, tag_buffer)

        input_buffer, input_len = _buffer(ciphertext)
        output = (ctypes.c_ubyte * max(input_len, 1))()
        written = wintypes.ULONG(0)

        status = handle._lib.BCryptDecrypt(
            handle._key,
            input_buffer, input_len,
            ctypes.byref(info),
            None, 0,
            output, input_len,
            ctypes.byref(written),
            0,
        )
        if status != _STATUS_SUCCESS:
            # STATUS_AUTH_TAG_MISMATCH (0xC000A002) is the expected
            # failure for a wrong master password; anything else is a
            # real problem, but neither is recoverable here and both
            # mean the same thing to the caller.
            raise DecryptionFailed(
                "AES-GCM verification failed (NTSTATUS 0x%08X)"
                % (status & 0xFFFFFFFF)
            )
        return bytes(bytearray(output)[:written.value])
