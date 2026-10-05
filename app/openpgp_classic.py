"""
OpenPGP in the classic format of RFC 4880, the one every OpenPGP program
reads: Thunderbird, GnuPG, and every other mail client with OpenPGP.

Why it exists. PySequoia 0.1.35 always encrypts in the newer RFC 9580
format (version 6 key packets and SEIPD version 2), even to keys that
advertise nothing newer, and offers no way to choose. Thunderbird's
OpenPGP engine cannot read that format: such a message shows there as an
empty page whose subject is "...". So ZBox writes what Thunderbird itself
writes: a version 3 key packet (PKESK) for each recipient's encryption
subkey, then the message in a SEIPD version 1 packet, AES-256 in CFB mode
with its modification detection code.

What is here and what is not. Only the encryption container: the key
packets and the encrypted data packet, with the cryptography package for
AES, AES key wrap, X25519, NIST curve ECDH and RSA. Which subkeys to
encrypt to, signing, armor and every reading stay with PySequoia, in
openpgp.py, so Sequoia's policy decides what is a usable key. The tests
prove each message decrypts through Sequoia.

Key types: RSA, and ECDH on Curve25519 or NIST P-256, P-384 and P-521, in
version 4 keys. supported() says whether a subkey is one of them; for any
other the caller keeps PySequoia's own format.
"""

import hashlib
import os
import struct
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

_AES256 = 9
_RSA = (1, 2)
_ECDH = 18
_CURVE25519 = bytes.fromhex("2b060104019755010501")
_NIST = {
    bytes.fromhex("2a8648ce3d030107"): ec.SECP256R1,
    bytes.fromhex("2b81040022"): ec.SECP384R1,
    bytes.fromhex("2b81040023"): ec.SECP521R1,
}
_KDF_HASHES = {8: hashlib.sha256, 9: hashlib.sha384, 10: hashlib.sha512}
_KEK_SIZES = {7: 16, 8: 24, 9: 32}


class ClassicError(ValueError):
    """A key or message the classic format cannot be written for."""


class Subkey:
    """One encryption subkey, from its packet body (no header)."""

    def __init__(self, body):
        body = bytes(body)
        if len(body) < 6:
            raise ClassicError("a key packet is too short")
        self.body = body
        self.version = body[0]
        self.algorithm = body[5]
        self.fingerprint = hashlib.sha1(b"\x99" + struct.pack(">H", len(body)) + body).digest()
        self.key_id = self.fingerprint[-8:]


def supported(subkey):
    """True for a version 4 RSA or ECDH (Curve25519, NIST) subkey."""
    if subkey.version != 4:
        return False
    if subkey.algorithm in _RSA:
        return True
    if subkey.algorithm == _ECDH:
        try:
            oid, _point, kdf = _ecdh_fields(subkey.body)
        except ClassicError:
            return False
        return (oid == _CURVE25519 or oid in _NIST) and kdf[0] in _KDF_HASHES and kdf[1] in _KEK_SIZES
    return False


# --- Small encodings ------------------------------------------------------

def _mpi_read(data, offset):
    if offset + 2 > len(data):
        raise ClassicError("a key packet ends early")
    bits = struct.unpack(">H", data[offset:offset + 2])[0]
    end = offset + 2 + (bits + 7) // 8
    if end > len(data):
        raise ClassicError("a key packet ends early")
    return data[offset + 2:end], end


def _mpi(value):
    """An integer or big-endian bytes as an OpenPGP MPI."""
    if isinstance(value, int):
        value = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    value = value.lstrip(b"\x00") or b"\x00"
    bits = (len(value) - 1) * 8 + value[0].bit_length()
    return struct.pack(">H", bits) + value


def _header(tag, length):
    """A new-format packet header."""
    if length < 192:
        return bytes([0xC0 | tag, length])
    if length < 8384:
        length -= 192
        return bytes([0xC0 | tag, (length >> 8) + 192, length & 0xFF])
    return bytes([0xC0 | tag, 0xFF]) + struct.pack(">I", length)


def _packet(tag, body):
    return _header(tag, len(body)) + body


def _ecdh_fields(body):
    """(curve OID, public point, (KDF hash, key wrap algorithm))."""
    if len(body) < 7:
        raise ClassicError("a key packet is too short")
    oid_len = body[6]
    if oid_len in (0, 0xFF) or 7 + oid_len > len(body):
        raise ClassicError("a key packet has no curve")
    oid = body[7:7 + oid_len]
    point, offset = _mpi_read(body, 7 + oid_len)
    if offset + 4 > len(body) or body[offset] < 3 or body[offset + 1] != 1:
        raise ClassicError("a key packet has no key-derivation settings")
    return oid, point, (body[offset + 2], body[offset + 3])


# --- The key packets ----------------------------------------------------------

def _session_material(session_key):
    checksum = sum(session_key) & 0xFFFF
    return bytes([_AES256]) + session_key + struct.pack(">H", checksum)


def _pkesk_rsa(subkey, session_key):
    n, offset = _mpi_read(subkey.body, 6)
    e, _offset = _mpi_read(subkey.body, offset)
    public = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
    encrypted = public.encrypt(_session_material(session_key), padding.PKCS1v15())
    return bytes([3]) + subkey.key_id + bytes([subkey.algorithm]) + _mpi(encrypted)


def _pkesk_ecdh(subkey, session_key):
    """RFC 6637: an ephemeral key, the shared secret through the KDF into a
    key-encryption key, the session key wrapped with AES key wrap."""
    oid, point, (hash_id, wrap_id) = _ecdh_fields(subkey.body)
    if oid == _CURVE25519:
        if len(point) != 33 or point[0] != 0x40:
            raise ClassicError("a Curve25519 key has an unexpected point")
        ephemeral = X25519PrivateKey.generate()
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(point[1:]))
        ephemeral_point = b"\x40" + ephemeral.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
    elif oid in _NIST:
        curve = _NIST[oid]()
        recipient = ec.EllipticCurvePublicKey.from_encoded_point(curve, point)
        ephemeral = ec.generate_private_key(curve)
        shared = ephemeral.exchange(ec.ECDH(), recipient)
        ephemeral_point = ephemeral.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
    else:
        raise ClassicError("a key uses a curve the classic format is not written for")
    param = (bytes([len(oid)]) + oid + bytes([_ECDH, 3, 1, hash_id, wrap_id])
             + b"Anonymous Sender    " + subkey.fingerprint)
    kek = _KDF_HASHES[hash_id](b"\x00\x00\x00\x01" + shared + param).digest()[:_KEK_SIZES[wrap_id]]
    material = _session_material(session_key)
    pad = 8 - (len(material) % 8)
    wrapped = aes_key_wrap(kek, material + bytes([pad]) * pad)
    return (bytes([3]) + subkey.key_id + bytes([_ECDH]) + _mpi(ephemeral_point)
            + bytes([len(wrapped)]) + wrapped)


def _pkesk(subkey, session_key):
    if not supported(subkey):
        raise ClassicError("a key type the classic format is not written for")
    if subkey.algorithm in _RSA:
        return _packet(1, _pkesk_rsa(subkey, session_key))
    return _packet(1, _pkesk_ecdh(subkey, session_key))


# --- The message -------------------------------------------------------------

def literal(data, when=None):
    """A literal data packet holding data as binary, with no file name."""
    stamp = int(time.time() if when is None else when) & 0xFFFFFFFF
    return _packet(11, b"b\x00" + struct.pack(">I", stamp) + bytes(data))


def encrypt(subkeys, packets):
    """The binary OpenPGP message: one version 3 key packet per subkey, then
    packets (a literal data packet, or a signed message) inside a SEIPD
    version 1 packet, AES-256 in CFB mode with its modification detection
    code. subkeys are Subkey objects, each supported()."""
    if not subkeys:
        raise ClassicError("there is no key to encrypt to")
    session_key = os.urandom(32)
    head = b"".join(_pkesk(subkey, session_key) for subkey in subkeys)
    prefix = os.urandom(16)
    prefix += prefix[-2:]
    plain = prefix + bytes(packets) + b"\xd3\x14"
    plain += hashlib.sha1(plain).digest()
    encryptor = Cipher(algorithms.AES(session_key), modes.CFB(bytes(16))).encryptor()
    encrypted = encryptor.update(plain) + encryptor.finalize()
    return head + _packet(18, b"\x01" + encrypted)
