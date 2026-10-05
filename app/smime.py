"""
S/MIME: certificates and the checks behind reading signed and encrypted
mail, with the cryptography package only.

The certificate list is data/userdata/smime.json, encrypted at rest like
the address book (secure_json). It holds the user's own certificates with
their private keys, imported from .p12 or .pfx files, and correspondents'
certificates, imported by hand or kept from mail they signed.

cryptography decrypts S/MIME but has no call that checks a signature, so
the signature is read here with a small DER reader: the signed attributes'
message digest against the content, then the signature over those
attributes (or over the content when there are none) with the signer
certificate's key: RSA (PKCS #1 v1.5 or PSS), ECDSA or Ed25519, with SHA-1
to SHA-512. Trust is a chain from the signer to an authority Windows
trusts, each link checked with cryptography and within its dates. Nothing
is fetched from the network.
"""

import datetime
import hashlib
import logging
import os
import threading

import secure_json

logger = logging.getLogger("zbox.smime")

FILE_NAME = "smime.json"
MAX_IMPORT_BYTES = 5 * 1024 * 1024

_OID_DATA = bytes.fromhex("2a864886f70d010701")
_OID_SIGNED = bytes.fromhex("2a864886f70d010702")
_OID_ENVELOPED = bytes.fromhex("2a864886f70d010703")
_OID_MESSAGE_DIGEST = bytes.fromhex("2a864886f70d010904")
_OID_SIGNING_TIME = bytes.fromhex("2a864886f70d010905")
_OID_RSA_PSS = bytes.fromhex("2a864886f70d01010a")
_DIGESTS = {
    bytes.fromhex("2b0e03021a"): "sha1",
    bytes.fromhex("608648016503040204"): "sha224",
    bytes.fromhex("608648016503040201"): "sha256",
    bytes.fromhex("608648016503040202"): "sha384",
    bytes.fromhex("608648016503040203"): "sha512",
}
# Signature algorithm identifiers that name their digest themselves.
_SIGNATURE_DIGESTS = {
    bytes.fromhex("2a864886f70d010105"): "sha1",
    bytes.fromhex("2a864886f70d01010e"): "sha224",
    bytes.fromhex("2a864886f70d01010b"): "sha256",
    bytes.fromhex("2a864886f70d01010c"): "sha384",
    bytes.fromhex("2a864886f70d01010d"): "sha512",
    bytes.fromhex("2a8648ce3d040101"): "sha1",
    bytes.fromhex("2a8648ce3d040301"): "sha224",
    bytes.fromhex("2a8648ce3d040302"): "sha256",
    bytes.fromhex("2a8648ce3d040303"): "sha384",
    bytes.fromhex("2a8648ce3d040304"): "sha512",
}


class SmimeError(RuntimeError):
    """A certificate or a signature that cannot be used, with why."""


def available():
    try:
        import cryptography  # noqa: F401
        from cryptography.hazmat.primitives.serialization import pkcs7  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --- DER ------------------------------------------------------------------

def _one(data, pos, end):
    """(tag, start, content start, content end, item end) of the item at
    pos. BER's indefinite length, which some mail programs send, is read
    too: its content runs to the end-of-contents mark."""
    if pos + 2 > end:
        raise SmimeError("broken structure")
    tag = data[pos]
    length = data[pos + 1]
    if length == 0x80:
        inner = pos + 2
        while True:
            if inner + 2 > end:
                raise SmimeError("broken structure")
            if data[inner] == 0 and data[inner + 1] == 0:
                return (tag, pos, pos + 2, inner, inner + 2)
            inner = _one(data, inner, end)[4]
    head = 2
    if length & 0x80:
        count = length & 0x7F
        if count > 4 or pos + 2 + count > end:
            raise SmimeError("broken length")
        length = int.from_bytes(data[pos + 2:pos + 2 + count], "big")
        head = 2 + count
    stop = pos + head + length
    if stop > end:
        raise SmimeError("broken structure")
    return (tag, pos, pos + head, stop, stop)


def _items(data, start=0, end=None):
    """(tag, start, content start, content end, item end) for each item."""
    end = len(data) if end is None else end
    items = []
    pos = start
    while pos < end:
        if data[pos] == 0 and pos + 1 < end and data[pos + 1] == 0:
            break  # end-of-contents of an indefinite length
        item = _one(data, pos, end)
        items.append(item)
        pos = item[4]
    return items


def _inside(data, item):
    return _items(data, item[2], item[3])


def _value(data, item):
    return data[item[2]:item[3]]


def _whole(data, item):
    return data[item[1]:item[4]]


def content_type(der):
    """"signed", "enveloped" or "" for a ContentInfo."""
    try:
        top = _items(der)[0]
        oid = _value(der, _inside(der, top)[0])
    except (SmimeError, IndexError):
        return ""
    return {_OID_SIGNED: "signed", _OID_ENVELOPED: "enveloped"}.get(oid, "")


def _signed_data(der):
    top = _items(der)[0]
    fields = _inside(der, top)
    if _value(der, fields[0]) != _OID_SIGNED:
        raise SmimeError("not signed data")
    explicit = fields[1]
    return _inside(der, explicit)[0]


def _ber_octets(der, item):
    """The bytes of an OCTET STRING, joined when it was sent in pieces."""
    if item[0] == 0x04:
        return _value(der, item)
    if item[0] in (0x24, 0xA0):
        return b"".join(_ber_octets(der, part) for part in _inside(der, item))
    raise SmimeError("not an octet string")


def parse_signed(der):
    """What a SignedData holds: the signed content when it is inside
    (opaque signing, else None), the certificates sent with it, and the
    first signer's fields."""
    from cryptography import x509

    signed = _signed_data(der)
    fields = _inside(der, signed)
    encap = fields[2]
    encap_fields = _inside(der, encap)
    content = None
    if len(encap_fields) > 1:
        explicit = encap_fields[1]
        content = _ber_octets(der, _inside(der, explicit)[0])
    certificates = []
    signer_set = None
    for item in fields[3:]:
        if item[0] == 0xA0:
            for cert_item in _inside(der, item):
                if cert_item[0] == 0x30:
                    try:
                        certificates.append(x509.load_der_x509_certificate(_whole(der, cert_item)))
                    except Exception:  # noqa: BLE001 - an odd certificate is skipped
                        pass
        elif item[0] == 0x31:
            signer_set = item
    if signer_set is None:
        raise SmimeError("no signer")
    signers = _inside(der, signer_set)
    if not signers:
        raise SmimeError("no signer")
    info = _inside(der, signers[0])
    signer = {"sid": info[1], "attrs": None, "signature": None}
    rest = info[2:]
    signer["digest"] = _value(der, _inside(der, rest[0])[0])
    position = 1
    if position < len(rest) and rest[position][0] == 0xA0:
        signer["attrs"] = rest[position]
        position += 1
    signer["sig_alg"] = _value(der, _inside(der, rest[position])[0])
    signer["signature"] = _value(der, rest[position + 1])
    return content, certificates, signer


def _signer_certificate(der, sid, certificates):
    from cryptography import x509

    if sid[0] == 0x30:
        parts = _inside(der, sid)
        serial = int.from_bytes(_value(der, parts[1]), "big", signed=True)
        issuer = _whole(der, parts[0])
        for cert in certificates:
            if cert.serial_number == serial and cert.issuer.public_bytes() == issuer:
                return cert
        for cert in certificates:
            if cert.serial_number == serial:
                return cert
    elif sid[0] == 0x80:
        wanted = _value(der, sid)
        for cert in certificates:
            try:
                ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
            except Exception:  # noqa: BLE001
                continue
            if ski == wanted:
                return cert
    return None


def _hash(name):
    from cryptography.hazmat.primitives import hashes

    return {"sha1": hashes.SHA1(), "sha224": hashes.SHA224(), "sha256": hashes.SHA256(),
            "sha384": hashes.SHA384(), "sha512": hashes.SHA512()}[name]


def _check_signature(cert, signature, data, digest_name, sig_alg):
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

    key = cert.public_key()
    if isinstance(key, rsa.RSAPublicKey):
        if sig_alg == _OID_RSA_PSS:
            key.verify(signature, data, padding.PSS(
                mgf=padding.MGF1(_hash(digest_name)), salt_length=padding.PSS.AUTO,
            ), _hash(digest_name))
        else:
            key.verify(signature, data, padding.PKCS1v15(), _hash(digest_name))
    elif isinstance(key, ec.EllipticCurvePublicKey):
        key.verify(signature, data, ec.ECDSA(_hash(digest_name)))
    elif isinstance(key, ed25519.Ed25519PublicKey):
        key.verify(signature, data)
    else:
        raise SmimeError("unsupported key type")


def _utc_time(der, item):
    text = _value(der, item).decode("ascii", "replace").rstrip("Z")
    try:
        if item[0] == 0x17:
            moment = datetime.datetime.strptime(text[:12], "%y%m%d%H%M%S")
        else:
            moment = datetime.datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc)


def verify(der, content=None):
    """Checks a SignedData against content (the signed part exactly as sent
    for a detached signature; None takes the content from inside). Returns
    (valid, signer certificate or None, all certificates sent, the signed
    content, signing time or None). Raises SmimeError on a structure it
    cannot read."""
    inner, certificates, signer = parse_signed(der)
    if content is None:
        content = inner
    if content is None:
        raise SmimeError("no content")
    digest_name = _DIGESTS.get(signer["digest"]) or _SIGNATURE_DIGESTS.get(signer["sig_alg"])
    if digest_name is None:
        raise SmimeError("unsupported digest")
    cert = _signer_certificate(der, signer["sid"], certificates)
    if cert is None:
        return False, None, certificates, content, None
    signed_on = None
    candidates = [content]
    if b"\r\n" not in content and b"\n" in content:
        candidates.append(content.replace(b"\n", b"\r\n"))
    for candidate in candidates:
        if signer["attrs"] is not None:
            wanted = None
            for attr in _inside(der, signer["attrs"]):
                attr_fields = _inside(der, attr)
                oid = _value(der, attr_fields[0])
                values = _inside(der, attr_fields[1])
                if oid == _OID_MESSAGE_DIGEST and values:
                    wanted = _value(der, values[0])
                elif oid == _OID_SIGNING_TIME and values:
                    signed_on = _utc_time(der, values[0])
            if wanted is None or hashlib.new(digest_name, candidate).digest() != wanted:
                continue
            data = b"\x31" + der[signer["attrs"][1] + 1:signer["attrs"][3]]
        else:
            data = candidate
        try:
            _check_signature(cert, signer["signature"], data, digest_name, signer["sig_alg"])
        except Exception:  # noqa: BLE001 - a wrong signature
            continue
        return True, cert, certificates, candidate, signed_on
    return False, cert, certificates, content, signed_on


# --- certificates -----------------------------------------------------------

def addresses(cert):
    """The email addresses a certificate is for, lowercase."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    found = []
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        found += [str(value).strip().lower() for value in names.get_values_for_type(x509.RFC822Name)]
    except Exception:  # noqa: BLE001
        pass
    for attr in cert.subject.get_attributes_for_oid(NameOID.EMAIL_ADDRESS):
        found.append(str(attr.value).strip().lower())
    return list(dict.fromkeys(address for address in found if address))


def common_name(name):
    from cryptography.x509.oid import NameOID

    for attr in name.get_attributes_for_oid(NameOID.COMMON_NAME):
        return str(attr.value)
    for attr in name.get_attributes_for_oid(NameOID.ORGANIZATION_NAME):
        return str(attr.value)
    return name.rfc4514_string()


def _dates(cert):
    try:
        return cert.not_valid_before_utc, cert.not_valid_after_utc
    except AttributeError:  # older cryptography
        return (cert.not_valid_before.replace(tzinfo=datetime.timezone.utc),
                cert.not_valid_after.replace(tzinfo=datetime.timezone.utc))


def in_date(cert, moment=None):
    moment = moment or datetime.datetime.now(datetime.timezone.utc)
    start, end = _dates(cert)
    return start <= moment <= end


def fingerprint(cert):
    from cryptography.hazmat.primitives import hashes

    return cert.fingerprint(hashes.SHA256()).hex()


def windows_roots():
    """The certificates Windows trusts as roots, and its intermediate
    authorities, from the Windows certificate stores."""
    import ssl

    from cryptography import x509

    roots, intermediates = [], []
    for store, target in (("ROOT", roots), ("CA", intermediates)):
        try:
            entries = ssl.enum_certificates(store)
        except Exception:  # noqa: BLE001 - not Windows, or the store is closed
            continue
        for data, encoding, _trust in entries:
            if encoding != "x509_asn":
                continue
            try:
                target.append(x509.load_der_x509_certificate(data))
            except Exception:  # noqa: BLE001
                pass
    return roots, intermediates


def is_trusted(cert, extra=(), roots=None, intermediates=()):
    """True when a chain runs from cert to a trusted root, each link signed
    by the next and every certificate within its dates."""
    if roots is None:
        roots, found = windows_roots()
        intermediates = list(intermediates) + found
    pool = list(extra) + list(intermediates)
    root_keys = {fingerprint(root): root for root in roots}
    current = cert
    for _depth in range(8):
        if not in_date(current):
            return False
        if fingerprint(current) in root_keys:
            return True
        issuer = None
        for candidate in list(roots) + pool:
            if candidate.subject != current.issuer:
                continue
            try:
                current.verify_directly_issued_by(candidate)
            except Exception:  # noqa: BLE001
                continue
            issuer = candidate
            break
        if issuer is None:
            return False
        if fingerprint(issuer) in root_keys:
            return in_date(issuer)
        current = issuer
    return False


# --- the list ---------------------------------------------------------------

class CertStore:
    """The user's own certificates with their keys, and correspondents'
    certificates, in one encrypted file."""

    _lock = threading.Lock()

    def __init__(self, paths):
        self._file = os.path.join(paths.userdata, FILE_NAME)
        self._config = getattr(paths, "config", None)
        self._certs = []
        try:
            data = secure_json.load(self._config, self._file)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read %s: %s", FILE_NAME, type(exc).__name__)
            data = {}
        for entry in (data.get("certs") if isinstance(data, dict) else None) or []:
            if isinstance(entry, dict) and entry.get("pem"):
                self._certs.append(entry)

    def _save(self):
        secure_json.save(self._config, self._file, {"certs": self._certs})

    @staticmethod
    def _entry(cert, key_pem=None):
        from cryptography.hazmat.primitives import serialization

        start, end = _dates(cert)
        return {
            "fingerprint": fingerprint(cert),
            "pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "key_pem": key_pem,
            "own": bool(key_pem),
            "name": common_name(cert.subject),
            "issuer": common_name(cert.issuer),
            "emails": addresses(cert),
            "not_before": start.isoformat(),
            "not_after": end.isoformat(),
        }

    def entries(self):
        with self._lock:
            return [dict(entry) for entry in self._certs]

    def _put(self, entry):
        with self._lock:
            kept = [e for e in self._certs if e["fingerprint"] != entry["fingerprint"]]
            old = [e for e in self._certs if e["fingerprint"] == entry["fingerprint"]]
            if old and old[0].get("key_pem") and not entry.get("key_pem"):
                entry = dict(entry, key_pem=old[0]["key_pem"], own=True)
            kept.append(entry)
            self._certs = kept
            self._save()
        return entry

    def import_bytes(self, raw, password=None):
        """Adds a .p12/.pfx (your certificate with its key) or a .cer, .crt,
        .pem or .der certificate. Returns the entries added. Raises
        SmimeError with why."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import pkcs12

        if len(raw) > MAX_IMPORT_BYTES:
            raise SmimeError("larger than 5 MB")
        if b"-----BEGIN" in raw[:4096]:
            try:
                certs = x509.load_pem_x509_certificates(raw)
            except Exception as exc:  # noqa: BLE001
                raise SmimeError("not a certificate file") from exc
            return [self._put(self._entry(cert)) for cert in certs]
        try:
            cert = x509.load_der_x509_certificate(raw)
        except Exception:  # noqa: BLE001 - not a single certificate: .p12 or .pfx
            cert = None
        if cert is not None:
            return [self._put(self._entry(cert))]
        try:
            key, cert, _extra = pkcs12.load_key_and_certificates(
                raw, password.encode("utf-8") if password else None,
            )
        except ValueError as exc:
            raise SmimeError("wrong password, or not a certificate file") from exc
        if cert is None:
            raise SmimeError("no certificate in the file")
        key_pem = None
        if key is not None:
            key_pem = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii")
        return [self._put(self._entry(cert, key_pem))]

    @staticmethod
    def is_pkcs12(raw):
        """True for a .p12 or .pfx file, which needs its password."""
        from cryptography import x509

        if b"-----BEGIN" in raw[:4096]:
            return False
        try:
            x509.load_der_x509_certificate(raw)
        except Exception:  # noqa: BLE001
            return True
        return False

    def collect(self, cert):
        """Keeps a correspondent's certificate from a valid signature,
        silently. Your own certificates are never replaced."""
        try:
            entry = self._entry(cert)
            with self._lock:
                if any(e["fingerprint"] == entry["fingerprint"] for e in self._certs):
                    return False
            self._put(entry)
            return True
        except Exception as exc:  # noqa: BLE001 - collecting is never worth an error
            logger.debug("Certificate not kept: %s", type(exc).__name__)
            return False

    def own_pairs(self):
        """(certificate, private key) for each of your own certificates."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        pairs = []
        for entry in self.entries():
            if not entry.get("key_pem"):
                continue
            try:
                cert = x509.load_pem_x509_certificate(entry["pem"].encode("ascii"))
                key = serialization.load_pem_private_key(entry["key_pem"].encode("ascii"), None)
            except Exception:  # noqa: BLE001
                continue
            pairs.append((cert, key))
        return pairs

    def public_pem(self, fp):
        for entry in self.entries():
            if entry["fingerprint"] == fp:
                return entry["pem"].encode("ascii")
        return None

    def delete(self, fp):
        with self._lock:
            self._certs = [e for e in self._certs if e["fingerprint"] != fp]
            self._save()


def decrypt(store, der):
    """The content of an EnvelopedData opened with one of your own
    certificates, or None when none of them opens it."""
    from cryptography.hazmat.primitives.serialization import pkcs7

    for cert, key in store.own_pairs():
        try:
            return pkcs7.pkcs7_decrypt_der(der, cert, key, [])
        except Exception:  # noqa: BLE001 - not for this certificate
            continue
    return None
