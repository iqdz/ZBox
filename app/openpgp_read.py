"""
OpenPGP stage 2: reading encrypted and signed mail.

What it does, as Thunderbird does: decrypts PGP/MIME (multipart/encrypted)
and inline PGP, checks PGP/MIME (multipart/signed), inline and cleartext
signatures and the signature inside an encrypted message, takes the
protected headers (the real Subject inside a message whose outer Subject
is "...") and says what it found in Thunderbird's own words.

Where it sits. The caches, the offline copy and every listing keep the
message as it arrived: read_message caches the outer, encrypted form,
which holds no readable text. Only when a message is shown does
main_frame._readable hand it here, on the worker that read it, with a way
to fetch the exact bytes from the server; the readable form comes back
as a new message dict, in the 'message read' shape every reader already
understands, carrying its Status under STATUS_KEY. It lives in the tab or
the preview that shows it and goes with it. Nothing here writes anything
anywhere, and nothing decrypted is logged.

Every PySequoia call stays in openpgp.py; this module only imports it
when a protected message is actually read, so ZBox starts as before.
"""

import email
import email.header
import email.policy
import email.utils
import logging
import re

import lang
import message_body

logger = logging.getLogger("zbox.openpgp")

STATUS_KEY = "_zbox_pgp"
_ARMOR_MESSAGE = "-----BEGIN PGP MESSAGE-----"
_ARMOR_SIGNED = "-----BEGIN PGP SIGNED MESSAGE-----"
_ARMOR_RE = re.compile(
    r"-----BEGIN PGP (?:SIGNED )?MESSAGE-----.*?-----END PGP (?:MESSAGE|SIGNATURE)-----", re.S
)
# Headers that describe the outer wrapper rather than the message. The
# readable message takes these from the inner part only.
_MIME_HEADERS = (
    "content-type", "content-transfer-encoding", "content-disposition",
    "content-description", "content-id", "mime-version",
)
_GOOD = ("own", "verified", "unverified")
_UNCERTAIN = ("not_accepted", "no_key", "uid_mismatch")
_INVALID = ("rejected", "technical")


class Status:
    """What reading one protected message found. Memory only."""

    def __init__(self, sender=""):
        # None: the message was not encrypted. True: decrypted.
        # False: encrypted, and it could not be decrypted.
        self.encrypted = None
        # Why an encrypted message was not decrypted: "no_key",
        # "passphrase", "unavailable", "fetch" or "unknown". None otherwise.
        self.reason = None
        self.decryption_key = None  # the personal key's fingerprint
        # "none", or a state from openpgp.signature_state.
        self.signature = "none"
        self.signer = None  # fingerprint or key ID
        self.signed_on = None  # detached signatures only
        self.subject = None  # the protected (inner) Subject
        self.plain = None  # the readable message, bytes
        self.sender = sender
        # (fingerprint, name) of a locked personal key to open first;
        # raw and original are kept only while that is pending.
        self.needs_passphrase = None
        self.raw = None
        self.original = None
        # "openpgp", or "smime" for S/MIME mail (smime_read); signer is
        # then the certificate's name, and issuer who issued it.
        self.kind = "openpgp"
        self.issuer = None


# --- What a parsed message is ---------------------------------------------

def status_of(message):
    """The Status of a message read here, or None."""
    if isinstance(message, dict):
        status = message.get(STATUS_KEY)
        if isinstance(status, Status):
            return status
    return None


def _content_type(message):
    return message_body.header_text(message, "content_type").lower()


def _leaf_types(message):
    return [
        str(part.get("content_type") or "").lower()
        for part in (message.get("parts") or [])
        if isinstance(part, dict)
    ]


def _texts(message):
    parts = message.get("parts") or []
    found = []
    for index in message.get("text_body") or []:
        if isinstance(index, int) and 0 <= index < len(parts):
            body = parts[index].get("body") if isinstance(parts[index], dict) else None
            if isinstance(body, dict) and isinstance(body.get("Text"), str):
                found.append(body["Text"])
    return found


def looks_protected(message):
    """True when a parsed message, as it arrived, is PGP/MIME encrypted or
    signed, or holds inline OpenPGP armor in its text. False for a message
    already read here, and for anything that is not a message dict."""
    if not isinstance(message, dict) or STATUS_KEY in message:
        return False
    content_type = _content_type(message)
    if "multipart/encrypted" in content_type:
        return True
    if "multipart/signed" in content_type and "pgp" in content_type:
        return True
    leaves = _leaf_types(message)
    if "application/pgp-encrypted" in leaves or "application/pgp-signature" in leaves:
        return True
    if "pkcs7" in content_type or any("pkcs7" in leaf for leaf in leaves):
        return True  # S/MIME, read by smime_read
    return any(_ARMOR_MESSAGE in text or _ARMOR_SIGNED in text for text in _texts(message))


def was_encrypted(message):
    """True for a message that came encrypted, read here or not: its text
    is not quoted into an unencrypted reply or forward."""
    status = status_of(message)
    if status is not None:
        return status.encrypted is not None
    if not isinstance(message, dict):
        return False
    if "multipart/encrypted" in _content_type(message):
        return True
    if "application/pgp-encrypted" in _leaf_types(message):
        return True
    return any(_ARMOR_MESSAGE in text for text in _texts(message))


def display_subject(message):
    """The protected Subject of a message read here, or None. For an
    encrypted message that was not decrypted and whose outer Subject is the
    "..." of protected headers: Encrypted Message, so the tab, the headers
    and the list never read a bare "...". A real outer Subject stays."""
    status = status_of(message)
    if status is None:
        return None
    if status.subject:
        return status.subject
    if status.encrypted is False and message_body.header_text(message, "subject") == "...":
        return lang.t("dialogs", "pgp_rs_hidden_subject", default="Encrypted Message")
    return None


def key_attachment_positions(message):
    """Positions, among the message's attachments, of attached public keys
    (application/pgp-keys), for Import OpenPGP Key."""
    if not isinstance(message, dict):
        return []
    parts = message.get("parts") or []
    positions = []
    for position, index in enumerate(message.get("attachments") or []):
        if isinstance(index, int) and 0 <= index < len(parts) and isinstance(parts[index], dict):
            if str(parts[index].get("content_type") or "").lower() == "application/pgp-keys":
                positions.append(position)
    return positions


def envelope_address(envelope):
    """The sender's address from a listing row, lower case, or ""."""
    sender = envelope.get("from") if isinstance(envelope, dict) else None
    if isinstance(sender, list):
        sender = sender[0] if sender else None
    if isinstance(sender, dict):
        return str(sender.get("addr") or sender.get("email") or sender.get("address") or "").strip().lower()
    return ""


def remember_subject(envelope, message):
    """Shows the protected Subject in the message list for the rest of the
    session, in memory only (envelope_format)."""
    subject = display_subject(message)
    if subject and isinstance(envelope, dict):
        import envelope_format

        envelope_format.remember_session_subject(envelope.get("message-id"), subject)


# --- Reading ----------------------------------------------------------------

def read(paths, message, fetch_bytes, sender=""):
    """
    The message to show. For a protected one: a new dict built from the
    readable message, carrying its Status under STATUS_KEY. Anything else,
    or any copy of ZBox without PySequoia, gets message itself back. Never
    raises; a failure is a status, and message itself is never changed.
    """
    if not looks_protected(message):
        return message
    import smime_read

    if smime_read.is_smime(message):
        return smime_read.read(paths, message, fetch_bytes, sender)
    reason = "unavailable"
    try:
        import openpgp

        if not openpgp.available():
            return _not_read(message, reason)
        reason = "fetch"
        raw = fetch_bytes()
    except Exception as exc:  # noqa: BLE001 - shown as not decrypted, with why
        logger.warning("A protected message could not be read: %s", type(exc).__name__)
        return _not_read(message, reason)
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogateescape")
    return _process(paths, message, bytes(raw or b""), Status(sender))


def retry(paths, message):
    """Reads a message again from the bytes kept while a passphrase was
    asked for, once it has been given."""
    status = status_of(message)
    if status is None or status.raw is None or status.original is None:
        return message
    return _process(paths, status.original, status.raw, Status(status.sender))


def give_up(message):
    """No passphrase was given: the kept bytes go."""
    status = status_of(message)
    if status is not None:
        status.raw = None
        status.original = None


def passphrase_opens(paths, fingerprint, passphrase):
    import openpgp

    try:
        return openpgp.passphrase_opens(openpgp.KeyStore(paths), fingerprint, passphrase)
    except Exception:  # noqa: BLE001 - a key that cannot be read does not open
        return False


def remember_passphrase(fingerprint, passphrase):
    import openpgp

    openpgp.remember_passphrase(fingerprint, passphrase)


def _not_read(message, reason):
    """An encrypted message that could not be read here at all -- no
    OpenPGP in this copy of ZBox, or its bytes could not be fetched --
    shown as not decrypted, with why. Anything else, a signed-only message
    included, comes back as it arrived."""
    if not was_encrypted(message):
        return message
    status = Status()
    status.encrypted = False
    status.reason = reason
    result = _explained(message, status)
    result[STATUS_KEY] = status
    return result


def _explained(original, status):
    """The message shown for one that came encrypted and was not decrypted:
    its own headers, and in place of the body it has none of, the words
    for what happened (undecrypted_text). The encrypted part itself is not
    offered as an attachment."""
    result = dict(original)
    parts = list(result.get("parts") or [])
    parts.append({
        "headers": [],
        "content_type": "text/plain",
        "body": {"Text": undecrypted_text(status)},
    })
    result["parts"] = parts
    result["text_body"] = [len(parts) - 1]
    result["html_body"] = []
    result["attachments"] = []
    return result


def _process(paths, original, raw, status):
    import imap_body_fetch
    import openpgp

    try:
        store = openpgp.KeyStore(paths)
    except Exception:  # noqa: BLE001 - no key list: nothing decrypts, signers unknown
        store = None
    plain = None
    try:
        plain = _unwrap(store, raw, status)
    except openpgp.NeedPassphrase as exc:
        status.encrypted = False
        status.needs_passphrase = (exc.fingerprint, exc.name)
        status.reason = "passphrase"
        status.raw = raw
        status.original = original
    except Exception as exc:  # noqa: BLE001 - shown as not decrypted or not checked
        logger.warning("Reading a protected message failed: %s", type(exc).__name__)
        if status.encrypted is None and _is_encrypted_raw(raw):
            status.encrypted = False
        if status.encrypted is False and status.reason is None:
            status.reason = "unknown"
    result = None
    if plain:
        try:
            result = imap_body_fetch.build_message_dict(plain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("A readable message could not be parsed: %s", type(exc).__name__)
            result = None
    if result is None:
        plain = None
        if status.encrypted is False:
            # Encrypted, not decrypted: never the empty page of the
            # message as it arrived.
            if status.reason is None:
                status.reason = "no_key"
            result = _explained(original, status)
        else:
            result = dict(original)
    status.plain = plain
    result[STATUS_KEY] = status
    return result


def _is_encrypted_raw(raw):
    head = raw[:4096].lower()
    return b"multipart/encrypted" in head or _ARMOR_MESSAGE.encode("ascii") in raw


def _parse(data):
    return email.message_from_bytes(data, policy=email.policy.compat32)


def _unwrap(store, raw, status):
    """The readable message as bytes, or None when there is nothing to
    show beyond the message as it arrived."""
    outer = _parse(raw)
    content_type = outer.get_content_type()
    protocol = str(outer.get_param("protocol") or "").lower()
    if content_type == "multipart/encrypted" and protocol == "application/pgp-encrypted":
        return _pgp_mime_encrypted(store, raw, outer, status)
    if content_type == "multipart/signed" and protocol == "application/pgp-signature":
        entity = _pgp_mime_signed(store, raw, outer, status)
        return _combine(outer, entity, status) if entity else None
    return _inline(store, outer, status)


def _pgp_mime_encrypted(store, raw, outer, status):
    import openpgp

    status.encrypted = False
    parts = outer.get_payload()
    if not isinstance(parts, list) or len(parts) < 2:
        status.reason = "unknown"
        return None
    if store is None:
        return None
    data = parts[1].get_payload(decode=True) or b""
    try:
        plain, fingerprint = openpgp.decrypt(store, data)
    except openpgp.NeedPassphrase:
        raise
    except openpgp.OpenPGPError:
        return None
    status.encrypted = True
    status.decryption_key = fingerprint
    check = openpgp.check_encrypted_signature(store, data, fingerprint)
    inner = _parse(plain)
    inner_protocol = str(inner.get_param("protocol") or "").lower()
    if (not check.signed and inner.get_content_type() == "multipart/signed"
            and inner_protocol == "application/pgp-signature"):
        # Signed first, then encrypted as a whole, as older senders do.
        entity = _pgp_mime_signed(store, plain, inner, status)
        if entity:
            return _combine(outer, entity, status)
    _set_signature(store, status, check, inner)
    return _combine(outer, plain, status)


def _body_bytes(raw):
    match = re.search(rb"\r?\n\r?\n", raw)
    return raw[match.end():] if match else b""


def _boundary_parts(body, boundary):
    """The parts of a multipart body exactly as on the wire, without the
    line break that belongs to each boundary line."""
    delim = b"--" + boundary.encode("ascii", "replace")
    pieces = re.split(rb"(?:\r?\n)?" + re.escape(delim) + rb"(?:--)?[ \t]*(?:\r?\n|$)", body)
    return pieces[1:-1] if len(pieces) > 2 else []


def _pgp_mime_signed(store, raw, message, status):
    """The signed part exactly as sent, after checking its signature; the
    status says what the check found."""
    import openpgp

    boundary = message.get_boundary()
    wire = _boundary_parts(_body_bytes(raw), boundary) if boundary else []
    if len(wire) < 2:
        return None
    entity, signature_part = wire[0], wire[1]
    signature = _parse(signature_part).get_payload(decode=True) or b""
    if store is None:
        status.signature = "no_key"
        return entity
    check = openpgp.verify_detached(store, entity, signature)
    status.signed_on = check.created
    _set_signature(store, status, check, _parse(entity))
    return entity


def _address(value):
    if not value:
        return ""
    return str(email.utils.parseaddr(str(value))[1] or "").strip().lower()


def _set_signature(store, status, check, inner):
    import openpgp

    sender = _address(inner.get("From")) if inner is not None else ""
    state, signer = openpgp.signature_state(store, check, sender or status.sender)
    status.signature = state
    status.signer = signer


def _decoded(value):
    try:
        return str(email.header.make_header(email.header.decode_header(str(value)))).strip()
    except Exception:  # noqa: BLE001 - shown as received
        return str(value).strip()


def _drop_legacy_parts(message):
    """Protected headers sent a second time as a visible part, for mail
    clients that cannot read them, are not shown as text."""
    if not message.is_multipart():
        return
    parts = message.get_payload()
    kept = [part for part in parts if part.get_content_type() != "text/rfc822-headers"]
    if len(kept) != len(parts):
        message.set_payload(kept)


def _combine(outer, inner_bytes, status):
    """The readable message: the inner part with its own headers, which
    win (protected headers), and the outer headers it lacks."""
    inner = _parse(inner_bytes)
    _drop_legacy_parts(inner)
    subject = inner.get("Subject")
    if subject is not None:
        status.subject = _decoded(subject) or None
    present = {name.lower() for name in inner.keys()}
    for name, value in outer.items():
        lower = name.lower()
        if lower in _MIME_HEADERS or lower in present:
            continue
        inner[name] = value
    return inner.as_bytes()


def _is_attachment(part):
    if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
        return True
    return bool(part.get_filename())


def _inline(store, message, status):
    """Inline OpenPGP in the text parts, replaced by the readable text."""
    changed = False
    for part in message.walk():
        if part.is_multipart() or part.get_content_type() != "text/plain" or _is_attachment(part):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if _ARMOR_MESSAGE not in text and _ARMOR_SIGNED not in text:
            continue
        new_text = _inline_text(store, text, status)
        if new_text is None:
            continue
        del part["Content-Transfer-Encoding"]
        part.set_payload(new_text, "utf-8")
        changed = True
    return message.as_bytes() if changed else None


def _strip_cleartext(block):
    """The text of a cleartext-signed block without its armor, for a
    signature that could not be checked."""
    lines = block.splitlines()
    out, started = [], False
    for line in lines[1:]:
        if not started:
            if not line.strip():
                started = True
            continue
        if line.startswith("-----BEGIN PGP SIGNATURE-----"):
            break
        out.append(line[2:] if line.startswith("- ") else line)
    return "\n".join(out)


def _inline_text(store, text, status):
    import openpgp

    found = False
    for block in _ARMOR_RE.findall(text):
        armored = block.encode("utf-8")
        if block.startswith(_ARMOR_SIGNED):
            plain, check = openpgp.verify_signed(store, armored) if store is not None else (None, None)
            readable = plain.decode("utf-8", "replace") if plain else _strip_cleartext(block)
            if check is not None:
                _set_signature(store, status, check, None)
            else:
                status.signature = "no_key"
        else:
            if status.encrypted is None:
                status.encrypted = False
            if store is None:
                continue
            try:
                plain, fingerprint = openpgp.decrypt(store, armored)
            except openpgp.NeedPassphrase:
                raise
            except openpgp.OpenPGPError:
                # Perhaps signed only, in the binary inline form.
                plain, check = openpgp.verify_signed(store, armored)
                if not plain:
                    continue
                status.encrypted = None
                _set_signature(store, status, check, None)
            else:
                status.encrypted = True
                status.decryption_key = fingerprint
                _set_signature(store, status, openpgp.check_encrypted_signature(store, armored, fingerprint), None)
            readable = plain.decode("utf-8", "replace")
        text = text.replace(block, readable, 1)
        found = True
    return text if found else None


# --- The words --------------------------------------------------------------

def _signature_label(status):
    import openpgp

    date = openpgp.format_date(status.signed_on)
    state = status.signature
    if state in _GOOD:
        if date:
            return lang.t("dialogs", "pgp_rs_good_date", default="Good Digital Signature - Signed on {date}", date=date)
        return lang.t("dialogs", "pgp_rs_good", default="Good Digital Signature")
    if state in _UNCERTAIN:
        if date:
            return lang.t("dialogs", "pgp_rs_uncertain_date", default="Uncertain Digital Signature - Signed on {date}", date=date)
        return lang.t("dialogs", "pgp_rs_uncertain", default="Uncertain Digital Signature")
    if state in _INVALID:
        if date:
            return lang.t("dialogs", "pgp_rs_invalid_date", default="Invalid Digital Signature - Signed on {date}", date=date)
        return lang.t("dialogs", "pgp_rs_invalid", default="Invalid Digital Signature")
    return lang.t("dialogs", "pgp_rs_no_sig", default="No Digital Signature")


def _signature_detail(status):
    state = status.signature
    if getattr(status, "kind", "openpgp") == "smime" and state != "none":
        return _smime_detail(status)
    if state == "own":
        return lang.t("dialogs", "pgp_rs_own_key", default="This message includes a valid digital signature from your personal key.")
    if state == "verified":
        return lang.t("dialogs", "pgp_rs_verified", default="This message includes a valid digital signature from a verified key.")
    if state == "unverified":
        return lang.t("dialogs", "pgp_rs_unverified", default="This message includes a valid digital signature from a key that you have already accepted. However, you have not yet verified that the key is really owned by the sender.")
    if state == "not_accepted":
        return lang.t("dialogs", "pgp_rs_not_accepted", default="This message contains a digital signature, but you haven’t yet decided if the signer’s key is acceptable to you.")
    if state == "no_key":
        return lang.t("dialogs", "pgp_rs_no_key", default="This message contains a digital signature, but it is uncertain if it is correct. To verify the signature, you need to obtain a copy of the sender’s public key.")
    if state == "uid_mismatch":
        return lang.t("dialogs", "pgp_rs_uid_mismatch", default="This message contains a digital signature, but a mismatch was detected. The message was sent from an email address that doesn’t match the signer’s public key.")
    if state == "rejected":
        return lang.t("dialogs", "pgp_rs_rejected", default="This message contains a digital signature, but you have previously decided to reject the signer key.")
    if state == "technical":
        return lang.t("dialogs", "pgp_rs_technical", default="This message contains a digital signature, but a technical error was detected. Either the message has been corrupted, or the message has been modified by someone else.")
    return lang.t("dialogs", "pgp_rs_no_sig_info", default="This message does not include the sender’s digital signature. The absence of a digital signature means that the message could have been sent by someone pretending to have this email address. It is also possible that the message has been altered while in transit over the network.")


def _smime_detail(status):
    """The S/MIME words for a signature's state (smime_read)."""
    state = status.signature
    if state == "verified":
        return lang.t("dialogs", "smime_rs_verified", default="This message includes a valid S/MIME signature from a certificate that an authority trusted by Windows issued for the sender's address.")
    if state == "unverified":
        return lang.t("dialogs", "smime_rs_unverified", default="This message includes a valid S/MIME signature, but its certificate was not issued by an authority that Windows trusts.")
    if state == "uid_mismatch":
        return lang.t("dialogs", "smime_rs_mismatch", default="This message includes an S/MIME signature, but the sender's address does not match the signer's certificate.")
    return lang.t("dialogs", "smime_rs_technical", default="This message includes an S/MIME signature, but it does not match the message. The message may have been changed after it was signed.")


def _encryption_label(status):
    if status.encrypted is True:
        return lang.t("dialogs", "pgp_rs_enc_valid", default="Message Is Encrypted")
    if status.encrypted is False:
        return lang.t("dialogs", "pgp_rs_enc_invalid", default="Message Cannot Be Decrypted")
    return lang.t("dialogs", "pgp_rs_enc_none", default="Message Is Not Encrypted")


def _encryption_detail(status):
    if status.encrypted is True:
        return lang.t("dialogs", "pgp_rs_enc_valid_info", default="This message was encrypted before it was sent to you. Encryption ensures the message can only be read by the recipients it was intended for.")
    if status.encrypted is False:
        return lang.t("dialogs", "pgp_rs_enc_invalid_info", default="This message was encrypted before it was sent to you, but it cannot be decrypted.")
    return lang.t("dialogs", "pgp_rs_enc_none_info", default="This message was not encrypted before it was sent. Information sent over the Internet without encryption can be seen by other people while in transit.")


def _reason_text(status):
    """Why an encrypted message was not decrypted, in words, or ""."""
    reason = getattr(status, "reason", None)
    if reason == "no_key":
        return lang.t("dialogs", "pgp_rs_why_no_key", default="The secret key that is required to decrypt this message is not available.")
    if reason == "passphrase":
        return lang.t("dialogs", "pgp_rs_why_passphrase", default="The passphrase of your key was not entered. Open the message again to enter it.")
    if reason == "unavailable":
        return lang.t("dialogs", "pgp_rs_why_unavailable", default="OpenPGP is not available in this copy of ZBox.")
    if reason == "fetch":
        return lang.t("dialogs", "pgp_rs_why_fetch", default="The message could not be fetched from the server to decrypt it. Open it again to try once more.")
    if reason == "unknown":
        return lang.t("dialogs", "pgp_rs_why_unknown", default="There are unknown problems with this encrypted message.")
    return ""


def undecrypted_text(status):
    """The text shown in place of the body of an encrypted message that was
    not decrypted: Thunderbird's words for it, why, and what to do."""
    lines = [_encryption_label(status), _encryption_detail(status)]
    why = _reason_text(status)
    if why:
        lines.append(why)
    if getattr(status, "reason", None) == "no_key" and getattr(status, "kind", "openpgp") == "smime":
        lines.append(lang.t("dialogs", "smime_rs_import_hint", default="To read it, import your S/MIME certificate with its private key, a .p12 or .pfx file, in Tools, S/MIME Certificate Manager."))
    elif getattr(status, "reason", None) == "no_key":
        lines.append(lang.t("dialogs", "pgp_rs_import_hint", default="To read it, import your secret key for this message into ZBox."))
    return "\n\n".join(lines)


def summary(status):
    """The words said when a message opens and shown first in the
    preview: the signature, then the encryption, in the order of
    Thunderbird's message security panel. "" without a status."""
    if status is None:
        return ""
    return "%s. %s." % (_signature_label(status), _encryption_label(status))


def details(status):
    """Everything about one message's security, one fact per line, for
    the Message security field above an open message."""
    import openpgp

    if status is None:
        return ""
    lines = [_signature_label(status), _signature_detail(status)]
    if status.signer and getattr(status, "kind", "openpgp") == "smime":
        lines.append(lang.t("dialogs", "smime_rs_signer", default="Signed by: {name}", name=status.signer))
        if getattr(status, "issuer", None):
            lines.append(lang.t("dialogs", "smime_rs_issuer", default="Certificate issued by: {issuer}", issuer=status.issuer))
    elif status.signer:
        lines.append(lang.t("dialogs", "pgp_rs_signer_id", default="Signer key ID: {keyid}", keyid="0x" + openpgp.key_id(status.signer)))
    lines.append(_encryption_label(status))
    lines.append(_encryption_detail(status))
    if status.encrypted is False:
        why = _reason_text(status)
        if why:
            lines.append(why)
    if status.decryption_key:
        lines.append(lang.t("dialogs", "pgp_rs_decrypt_id", default="Your decryption key ID: {keyid}", keyid="0x" + openpgp.key_id(status.decryption_key)))
    return "\n".join(lines)
