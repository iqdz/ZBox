"""
Reading S/MIME mail: signed (multipart/signed with an
application/pkcs7-signature part, or opaque signed-data) and encrypted
(application/pkcs7-mime enveloped-data), and both together.

It works like openpgp_read and gives its answer the same way: a new message
dict built from the readable message, carrying an openpgp_read.Status with
kind "smime" under openpgp_read.STATUS_KEY, so the message tab, the reader
and the spoken summary say what was found without knowing which standard
it was. openpgp_read.read hands S/MIME mail here.

States: "verified" for a valid signature from a certificate an authority
Windows trusts issued for the sender's address; "unverified" when valid
but not from such an authority; "uid_mismatch" when the certificate is for
another address; "technical" when the signature does not match the
message. A valid signature's certificate is kept for its address
(smime.CertStore.collect). Nothing decrypted is written or logged.
"""

import email
import email.policy
import email.utils
import logging

import openpgp_read

logger = logging.getLogger("zbox.smime")


def is_smime(message):
    """True for a parsed message, as it arrived, that is S/MIME."""
    if not isinstance(message, dict):
        return False
    content_type = openpgp_read._content_type(message)
    if "pkcs7" in content_type:
        return True
    return any("pkcs7" in leaf for leaf in openpgp_read._leaf_types(message))


def _parse(data):
    return email.message_from_bytes(data, policy=email.policy.compat32)


def _address(value):
    return str(email.utils.parseaddr(str(value or ""))[1] or "").strip().lower()


def read(paths, message, fetch_bytes, sender=""):
    """The message to show for S/MIME mail, with its Status. Never raises;
    a failure is a status, and message itself is never changed."""
    status = openpgp_read.Status(sender)
    status.kind = "smime"
    try:
        import smime

        if not smime.available():
            raise RuntimeError("unavailable")
        raw = fetch_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("An S/MIME message could not be read: %s", type(exc).__name__)
        if openpgp_read.was_encrypted(message) or "enveloped" in openpgp_read._content_type(message):
            status.encrypted = False
            status.reason = "fetch"
            result = openpgp_read._explained(message, status)
        else:
            result = dict(message)
        result[openpgp_read.STATUS_KEY] = status
        return result
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogateescape")
    raw = bytes(raw or b"")
    try:
        store = smime.CertStore(paths)
    except Exception:  # noqa: BLE001 - nothing decrypts, nothing is kept
        store = None
    plain = None
    try:
        plain = _unwrap(store, raw, status, depth=0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reading an S/MIME message failed: %s", type(exc).__name__)
        if status.signature == "none" and not status.encrypted:
            status.signature = "technical"
    result = None
    if plain:
        try:
            import imap_body_fetch

            result = imap_body_fetch.build_message_dict(plain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("A readable S/MIME message could not be parsed: %s", type(exc).__name__)
            result = None
    if result is None:
        plain = None
        if status.encrypted is False:
            if status.reason is None:
                status.reason = "no_key"
            result = openpgp_read._explained(message, status)
        else:
            result = dict(message)
    status.plain = plain
    result[openpgp_read.STATUS_KEY] = status
    return result


def _unwrap(store, raw, status, depth):
    """The readable message bytes, after decrypting and checking every
    layer, outer headers kept where the inner part has none."""
    import smime

    outer = _parse(raw)
    content_type = outer.get_content_type()
    if content_type == "multipart/signed":
        boundary = outer.get_boundary()
        wire = openpgp_read._boundary_parts(openpgp_read._body_bytes(raw), boundary) if boundary else []
        if len(wire) < 2:
            status.signature = "technical"
            return raw
        entity, signature_part = wire[0], wire[1]
        der = _parse(signature_part).get_payload(decode=True) or b""
        _check(store, status, der, entity, _parse(entity))
        return openpgp_read._combine(outer, _inner(store, entity, status, depth), status)
    if content_type in ("application/pkcs7-mime", "application/x-pkcs7-mime"):
        der = outer.get_payload(decode=True) or b""
        kind = smime.content_type(der)
        if kind == "enveloped":
            status.encrypted = False
            inner = smime.decrypt(store, der) if store is not None else None
            if inner is None:
                status.reason = "no_key"
                return None
            status.encrypted = True
            return openpgp_read._combine(outer, _inner(store, inner, status, depth), status)
        if kind == "signed":
            content = _check(store, status, der, None, None)
            if content is None:
                return raw
            return openpgp_read._combine(outer, _inner(store, content, status, depth), status)
    return raw


def _inner(store, entity, status, depth):
    """A part that is itself signed or encrypted (signed, then encrypted)
    is read once more; anything else is the readable part."""
    if depth >= 2:
        return entity
    inner = _parse(entity)
    if "pkcs7" in (inner.get("Content-Type") or "").lower():
        return _unwrap(store, entity, status, depth + 1) or entity
    return entity


def _check(store, status, der, content, inner):
    """Checks one signature and sets the status; the signed content back."""
    import smime

    try:
        valid, cert, certificates, signed, signed_on = smime.verify(der, content)
    except Exception as exc:  # noqa: BLE001 - a signature that cannot be read
        logger.info("An S/MIME signature could not be read: %s", type(exc).__name__)
        status.signature = "technical"
        return content
    # The signing time of an S/MIME signature is not shown: its date
    # wording belongs to OpenPGP's own timestamps.
    status.signed_on = None
    del signed_on
    if not valid or cert is None:
        status.signature = "technical"
        return signed
    if inner is None and signed:
        inner = _parse(signed)
    sender = _address(inner.get("From")) if inner is not None else ""
    sender = sender or str(status.sender or "").lower()
    names = smime.addresses(cert)
    status.signer = smime.common_name(cert.subject)
    status.issuer = smime.common_name(cert.issuer)
    status.signer_addresses = names
    if sender and sender not in names:
        status.signature = "uid_mismatch"
        return signed
    try:
        trusted = smime.is_trusted(cert, extra=certificates)
    except Exception:  # noqa: BLE001
        trusted = False
    status.signature = "verified" if trusted else "unverified"
    if store is not None:
        store.collect(cert)
    return signed
