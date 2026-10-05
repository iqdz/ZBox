"""
OpenPGP stage 3: the messages ZBox sends, as Thunderbird builds them.

- Encrypted: PGP/MIME (RFC 3156), multipart/encrypted, signed inside the
  encryption when Digitally Sign is on, always encrypted to your own key
  as well so the Sent copy stays readable. The real Subject and the other
  headers travel inside as protected headers (protected-headers="v1") and
  the outer Subject is "...".
- Signed only: PGP/MIME multipart/signed, with a detached signature over
  the signed part exactly as it is sent.
- Attach My Public Key: the public key as application/pgp-keys.
- Autocrypt: the header that carries your public key to anyone you write
  to.
- Drafts: encrypted to your own key only.

Nothing here shows anything, and nothing is written anywhere: these
functions take the message compose_panel built and return the text
Himalaya sends or appends. Every PySequoia call stays in openpgp.py.
"""

import email.policy
import secrets
from email.parser import BytesParser

import openpgp

# Everything leaves as 7-bit text with CRLF line endings, so a signature
# over the signed part survives any mail server on the way.
_SEVEN = email.policy.SMTP.clone(cte_type="7bit")
_CONTENT_HEADERS = (
    "content-type", "content-transfer-encoding", "content-disposition",
    "content-description", "content-id", "mime-version",
)
# The headers that also travel inside an encrypted or signed message,
# where they are protected (Thunderbird's protected headers). Bcc never
# does: everyone who can read the message would see it.
_PROTECTED = ("From", "To", "Cc", "Reply-To", "Subject", "Date", "Message-ID",
              "In-Reply-To", "References")
HIDDEN_SUBJECT = "..."
_CRLF = "\r\n"


class Plan:
    """What OpenPGP does to one outgoing message. Empty (active() False)
    for a message sent exactly as before."""

    def __init__(self, store=None, encrypt_to=(), sign_with=None, attach_key=None, autocrypt=None):
        self.store = store
        self.encrypt_to = list(encrypt_to or [])  # fingerprints, your own key included
        self.sign_with = sign_with  # your personal key's fingerprint
        self.attach_key = attach_key  # the key to attach, a fingerprint
        self.autocrypt = autocrypt  # the Autocrypt header value

    def active(self):
        return bool(self.encrypt_to or self.sign_with or self.attach_key or self.autocrypt)


def build(message, plan):
    """The message to send, as text, for himalaya_client.send_message_raw.
    message is the EmailMessage compose_panel built, Message-ID set."""
    store = plan.store
    if plan.autocrypt:
        message["Autocrypt"] = plan.autocrypt
    if plan.attach_key:
        attach_public_key(store, message, plan.attach_key)
    if not plan.encrypt_to and not plan.sign_with:
        return message.as_string()
    inner = _part_bytes(_inner_part(message))
    if plan.encrypt_to:
        armored = openpgp.encrypt_message(store, inner, plan.encrypt_to, sign_with=plan.sign_with)
        return _encrypted(message, armored)
    signature, micalg = openpgp.sign_detached(store, inner, plan.sign_with)
    return _signed(message, inner, signature, micalg)


def build_draft(store, message, fingerprint):
    """A draft encrypted to your own key only, protected headers inside and
    "..." outside, the Message-ID kept outside so the next save can find
    and replace it."""
    inner = _part_bytes(_inner_part(message))
    return _encrypted(message, openpgp.encrypt_message(store, inner, [fingerprint]))


def attach_public_key(store, message, fingerprint):
    """Attaches the public key as Thunderbird names it:
    OpenPGP_0x<key ID>.asc, application/pgp-keys."""
    armored = openpgp.public_key_armored(store, fingerprint).encode("ascii")
    message.add_attachment(
        armored, maintype="application", subtype="pgp-keys",
        filename="OpenPGP_0x%s.asc" % openpgp.key_id(fingerprint),
    )


def _inner_part(message):
    """The message's content as a multipart/mixed part carrying the
    protected headers, in 7-bit form."""
    part = BytesParser(policy=email.policy.default).parsebytes(message.as_bytes(policy=_SEVEN))
    for name in set(part.keys()):
        if name.lower() not in _CONTENT_HEADERS or name.lower() == "mime-version":
            del part[name]
    if part.get_content_type() != "multipart/mixed":
        part.make_mixed()
    part.set_param("protected-headers", "v1")
    content = [(name, value) for name, value in part.items()]
    for name, _value in content:
        del part[name]
    for name in _PROTECTED:
        value = message.get(name)
        if value is not None and str(value).strip():
            part[name] = str(value)
    for name, value in content:
        part[name] = value
    return part


def _part_bytes(part):
    data = part.as_bytes(policy=_SEVEN)
    if any(byte > 127 for byte in data):
        raise openpgp.OpenPGPError("the message could not be made 7-bit")
    return data


def _boundary(*inside):
    while True:
        boundary = "------------" + secrets.token_hex(12)
        token = ("--" + boundary).encode("ascii")
        if not any(token in data for data in inside):
            return boundary


def _outer_headers(message, hide_subject):
    """The outer header block, as text ending in a line break: every header
    of message except its content headers, the Subject replaced by "..."
    when it is hidden."""
    from email.message import EmailMessage

    outer = EmailMessage(policy=_SEVEN)
    for name, value in message.items():
        lower = name.lower()
        if lower in _CONTENT_HEADERS:
            continue
        if lower == "subject" and hide_subject:
            continue
        outer[name] = str(value)
    if hide_subject:
        outer["Subject"] = HIDDEN_SUBJECT
    text = outer.as_bytes(policy=_SEVEN).decode("ascii", "surrogateescape")
    return text.rstrip("\r\n") + _CRLF


def _lines(data):
    text = data.decode("ascii") if isinstance(data, bytes) else str(data)
    return _CRLF.join(text.strip("\r\n").splitlines())


def _encrypted(message, armored):
    boundary = _boundary(armored)
    head = _outer_headers(message, hide_subject=True) + _CRLF.join([
        "MIME-Version: 1.0",
        "Content-Type: multipart/encrypted;",
        ' protocol="application/pgp-encrypted";',
        ' boundary="%s"' % boundary,
        "",
        "",
    ])
    body = _CRLF.join([
        "This is an OpenPGP/MIME encrypted message (RFC 4880 and 3156)",
        "--" + boundary,
        "Content-Type: application/pgp-encrypted",
        "Content-Description: PGP/MIME version identification",
        "",
        "Version: 1",
        "",
        "--" + boundary,
        'Content-Type: application/octet-stream; name="encrypted.asc"',
        "Content-Description: OpenPGP encrypted message",
        'Content-Disposition: inline; filename="encrypted.asc"',
        "",
        _lines(armored),
        "",
        "--" + boundary + "--",
        "",
    ])
    return head + body


def _signed(message, inner, signature, micalg):
    """multipart/signed: the signed part exactly as signed, then the
    signature. The line break before each boundary belongs to the boundary,
    so the part a reader takes out is byte for byte what was signed."""
    boundary = _boundary(inner, signature)
    head = _outer_headers(message, hide_subject=False) + _CRLF.join([
        "MIME-Version: 1.0",
        "Content-Type: multipart/signed; micalg=%s;" % micalg,
        ' protocol="application/pgp-signature";',
        ' boundary="%s"' % boundary,
        "",
        "",
    ])
    signature_part = _CRLF.join([
        'Content-Type: application/pgp-signature; name="OpenPGP_signature.asc"',
        "Content-Description: OpenPGP digital signature",
        'Content-Disposition: attachment; filename="OpenPGP_signature.asc"',
        "",
        _lines(signature),
    ])
    return (
        head
        + "This is an OpenPGP/MIME signed message (RFC 4880 and 3156)" + _CRLF
        + "--" + boundary + _CRLF
        + inner.decode("ascii")
        + _CRLF + "--" + boundary + _CRLF
        + signature_part + _CRLF
        + "--" + boundary + "--" + _CRLF
    )
