"""
Persistent-connection message body fetching, bypassing the Himalaya
CLI for the one operation where a fresh subprocess costs the most.

Why this module exists
----------------------
Every himalaya.exe invocation is a whole process that performs a full
IMAP login handshake before it does anything else, then exits. For a
folder listing, once, that is fine. For reading a message body it is
not: a real ZBox session's own debug log measured 'message read'
calls at 3.2s, 3.4s, 6.0s, 7.3s, 21.9s and 22.1s -- the connection
setup, not the fetch, is the bulk of that. Arrow-navigating a message
list queues one of those per row, so the reader pane spends minutes
catching up with where the user already is. That is the "navigation
glitches" symptom, and no amount of caching fixes it while the cache
is cold.

An IMAP connection held open across reads turns the same fetch into
a single UID FETCH on an already-authenticated socket: typically tens
of milliseconds. So this module keeps one connection per account
(separate from imap_idle_watcher's, which is blocked in IDLE and must
stay that way) and speaks IMAP directly via imapclient, exactly as
that module already does for the same reason.

Everything else still goes through Himalaya. This module's only job
is "give me the parsed body of message N in folder F, fast". On any
failure at all -- no imapclient, no stored password, a dropped
connection, a message id the server won't fetch -- it raises
ImapBodyFetchUnavailable and himalaya_client falls straight back to
the subprocess path, which is still correct, just slow.

Output shape
------------
build_message_dict() returns the same structure himalaya's
'message read --json' returns, because message_body.py, the reader
pane, the message tab and the attachment list all already speak it
and there is no reason to make them learn a second shape:

    {"parts": [...], "text_body": [i], "html_body": [j],
     "attachments": [k, ...]}

parts is flat; text_body/html_body/attachments are lists of INDEXES
into it. Each part's "body" is a tagged union -- {"Text": "..."},
{"Html": "..."} or {"Multipart": [child indexes]}. Headers live on
parts[0] as [{"name": ..., "value": ...}], with recognised names in
snake_case and everything else as {"other": "X-Whatever"}; values are
tagged too ({"Text": ...}, {"TextList": [...]}, {"Address": {"List":
[{"name": ..., "address": ...}]}}). See message_body.py's own header
notes -- this module is the second producer of that shape and must
not drift from it.

Attachment parts deliberately carry no decoded payload. ZBox never
saves attachments from the message dict -- Save/Save All call
himalaya's 'attachment download' -- so holding a 20 MB payload in
memory per open tab would buy nothing. The decoded byte count is
recorded as the part's "size" field instead, which
message_view_panel._attachment_size_bytes reads for the Size column.
"""

import datetime
import email
import email.header
import email.policy
import email.utils
import logging
import ssl
import threading
import time

try:
    import imapclient
except ImportError:  # pragma: no cover - same posture as
    # imap_idle_watcher: a broken imapclient install means "no fast
    # path this run", never a crash. Every call just reports itself
    # unavailable and himalaya_client uses the subprocess.
    imapclient = None

import dpapi_secret_store

logger = logging.getLogger("zbox.imapfetch")

# A pooled connection idle longer than this is closed and rebuilt on
# next use. Servers drop unused IMAP sessions on their own schedule
# and a socket that looks open but isn't costs a full timeout to
# discover; reconnecting proactively is cheaper than finding out.
IDLE_CONNECTION_MAX_SECONDS = 5 * 60

# Socket timeout for the pooled connection. Deliberately well under
# himalaya_client's 90s read timeout: the whole point of this path is
# that it is fast, so a fetch that is not fast should give up and let
# the subprocess fallback do its slow, patient thing rather than
# holding the worker thread for a minute and a half.
FETCH_TIMEOUT_SECONDS = 20

# Connect timeout for background use (prefetch, quiet listings). An
# unreachable server used to hold a background connect -- and the
# connection's lock -- for the full 20 seconds; nobody is waiting on a
# background fetch, so it gives up sooner. The read timeout stays
# FETCH_TIMEOUT_SECONDS, and your own actions keep the full wait.
BACKGROUND_CONNECT_TIMEOUT_SECONDS = 8

_ADDRESS_HEADERS = frozenset({"from", "to", "cc", "bcc", "reply_to", "sender"})
_LIST_HEADERS = frozenset({"references"})
# Header names himalaya reports in snake_case rather than as
# {"other": ...}. Anything outside this set is emitted as "other",
# which is exactly how message_body._header_name_matches expects to
# find it.
_KNOWN_HEADERS = frozenset({
    "date", "subject", "from", "to", "cc", "bcc", "reply_to", "sender",
    "message_id", "in_reply_to", "references", "content_type",
    "content_disposition", "content_transfer_encoding", "mime_version",
    "return_path", "received",
})


class ImapBodyFetchUnavailable(Exception):
    """
    This path cannot serve the request. Always a signal to fall back
    to the Himalaya subprocess, never an error to show the user --
    the fallback produces the same result.
    """


class ImapRefused(ImapBodyFetchUnavailable):
    """
    The server answered and said no (a NO or BAD response, a failed
    login, a missing folder or capability). Not a network problem, so
    retrying the same command will not help. A subclass of
    ImapBodyFetchUnavailable so every existing caller still falls back
    to Himalaya exactly as before; delete_queue tells the two apart.
    """


def _is_refusal(exc):
    """True for an error the server itself sent, as opposed to a
    dropped or unreachable connection."""
    exceptions = getattr(imapclient, "exceptions", None) if imapclient is not None else None
    if exceptions is None:
        return False
    abort = getattr(exceptions, "IMAPClientAbortError", None)
    error = getattr(exceptions, "IMAPClientError", None)
    if abort is not None and isinstance(exc, abort):
        return False
    return error is not None and isinstance(exc, error)


def canonical_message_id(value):
    """A Message-ID reduced to the form two copies of the same
    message always share: no angle brackets, no surrounding space,
    lower case. None when there is nothing left."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip().strip("<>").strip().lower()
    return text or None


def _int_uids(message_ids):
    """Every id as an int UID, or ImapBodyFetchUnavailable if one is
    not a UID (an offline-copy id, say)."""
    try:
        return [int(message_id) for message_id in message_ids]
    except (TypeError, ValueError) as exc:
        raise ImapBodyFetchUnavailable(f"unusable message id: {exc}") from exc


# --- Header and part conversion --------------------------------------
#
# Pure functions, no network: everything below is unit-testable
# against a raw RFC822 string, which is why the parsing lives here
# rather than inside the connection class.


def _header_key(name):
    """RFC header name -> the key himalaya would report it under."""
    key = (name or "").strip().lower().replace("-", "_")
    return key if key in _KNOWN_HEADERS else None


def _decoded(value):
    """
    A header value as a plain string, with RFC 2047 encoded words
    (=?utf-8?B?...?=) decoded. email's default policy hands these
    back already decoded for most headers, but a raw fallback is
    still needed for the ones it leaves alone or fails on.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = str(value)
    if "=?" not in text:
        return text.strip()
    try:
        parts = []
        for chunk, charset in email.header.decode_header(text):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return "".join(parts).strip()
    except Exception:  # noqa: BLE001 - a malformed encoded word must
        # not lose the whole header; the undecoded text is still more
        # useful than nothing.
        return text.strip()


def _address_value(raw):
    """An address header as himalaya's {"Address": {"List": [...]}}."""
    entries = []
    for display, address in email.utils.getaddresses([raw or ""]):
        address = (address or "").strip()
        if not address:
            continue
        entries.append({"name": _decoded(display), "address": address})
    return {"Address": {"List": entries}}


def _build_headers(message):
    """
    Every top-level header of the parsed message, in wire order, in
    the shape message_body.py's header_* helpers read.
    """
    headers = []
    for raw_name, raw_value in message.items():
        key = _header_key(raw_name)
        text = _decoded(raw_value)
        if key in _ADDRESS_HEADERS:
            value = _address_value(str(raw_value))
        elif key in _LIST_HEADERS:
            value = {"TextList": [token for token in text.split() if token]}
        else:
            value = {"Text": text}
        name = key if key is not None else {"other": str(raw_name)}
        headers.append({"name": name, "value": value})
    return headers


def _is_attachment(part):
    """
    True for a part the user would think of as a file, rather than
    the message text. Content-Disposition is authoritative when
    present; a filename parameter alone also counts, which covers the
    mailers that send attachments with no disposition at all.
    """
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    if part.get_filename():
        return True
    return False


def _payload_text(part):
    """A text part's decoded content, never raising on bad bytes."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - a part with a broken
        # transfer encoding should cost that one part, not the message.
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _payload_size(part):
    """Decoded byte count of an attachment part, or None."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - see _payload_text
        return None
    return len(payload) if payload is not None else None


def build_message_dict(raw_bytes):
    """
    Parses one raw RFC822 message into himalaya's 'message read'
    JSON shape. See the module docstring for the shape itself.

    Walks the MIME tree depth-first, appending every part to one flat
    list as it goes -- parents before children, so parts[0] is always
    the message itself and always carries the headers. Container
    parts get a {"Multipart": [child indexes]} body; leaves get Text,
    Html, or (for attachments) no body at all, only a filename and a
    size.
    """
    parsed = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    parts = []
    text_indexes = []
    html_indexes = []
    attachment_indexes = []

    def walk(part, is_root):
        index = len(parts)
        entry = {}
        parts.append(entry)
        if is_root:
            entry["headers"] = _build_headers(parsed)
        else:
            entry["headers"] = []

        if part.is_multipart():
            children = [walk(child, False) for child in part.iter_parts()]
            entry["body"] = {"Multipart": children}
            return index

        content_type = (part.get_content_type() or "").lower()
        entry["content_type"] = content_type

        if _is_attachment(part):
            filename = _decoded(part.get_filename())
            if filename:
                entry["filename"] = filename
            size = _payload_size(part)
            if size is not None:
                entry["size"] = size
            attachment_indexes.append(index)
            return index

        if content_type == "text/html":
            entry["body"] = {"Html": _payload_text(part)}
            html_indexes.append(index)
        elif content_type.startswith("text/"):
            entry["body"] = {"Text": _payload_text(part)}
            text_indexes.append(index)
        else:
            # An inline non-text part with no filename: nothing to
            # show and nothing to save, but it still occupies an
            # index so the tree stays faithful.
            size = _payload_size(part)
            if size is not None:
                entry["size"] = size
        return index

    walk(parsed, True)

    return {
        "parts": parts,
        "text_body": text_indexes,
        "html_body": html_indexes,
        "attachments": attachment_indexes,
    }


# --- Pooled connections ----------------------------------------------


class _PooledConnection:
    """
    One account's persistent IMAP connection, plus the lock that
    keeps two worker threads from interleaving commands on it. Not
    used directly -- see fetch_message.
    """

    def __init__(self, account, paths):
        self._account = account
        self._paths = paths
        self._lock = threading.Lock()
        self._client = None
        self._folder = None
        self._last_used = 0.0

    def _connect(self, background=False):
        if _CLOSING:
            # Quitting. A connect here can hold this connection's lock
            # for FETCH_TIMEOUT_SECONDS per attempt, and close_all
            # used to wait for it: 32 s frozen on quit, live 24
            # September 2026, while Disroot timed out twice.
            raise ImapBodyFetchUnavailable("ZBox is closing")
        if imapclient is None:
            raise ImapBodyFetchUnavailable("imapclient is not installed")
        account = self._account
        if not account.imap_host:
            raise ImapBodyFetchUnavailable("account has no IMAP host")
        # A Microsoft account logs in with an access token (XOAUTH2)
        # instead of a password. Any failure here only means this fast
        # path is unavailable; the Himalaya subprocess still runs.
        microsoft = getattr(account, "auth_method", "password") == "microsoft"
        if microsoft:
            import ms_oauth  # only needed for Microsoft accounts

            try:
                password = ms_oauth.get_access_token(self._paths.config, account.account_id)
            except Exception as exc:  # noqa: BLE001 - falls back to Himalaya
                raise ImapBodyFetchUnavailable("Microsoft sign-in: %s" % exc)
        else:
            password = dpapi_secret_store.get_password(self._paths.config, account.account_id)
        if not password:
            raise ImapBodyFetchUnavailable("no stored password for this account")

        use_ssl = account.imap_encryption == "tls"
        ssl_context = ssl.create_default_context() if account.imap_encryption != "none" else None
        timeout = FETCH_TIMEOUT_SECONDS
        socket_timeout = getattr(imapclient, "SocketTimeout", None)
        if background and socket_timeout is not None:
            timeout = socket_timeout(
                connect=BACKGROUND_CONNECT_TIMEOUT_SECONDS, read=FETCH_TIMEOUT_SECONDS,
            )
        client = imapclient.IMAPClient(
            account.imap_host, port=account.imap_port,
            ssl=use_ssl, ssl_context=ssl_context,
            timeout=timeout,
        )
        try:
            if account.imap_encryption == "start-tls":
                client.starttls(ssl_context)
            if microsoft:
                client.oauth2_login(account.login_email, password)
            else:
                client.login(account.login_email, password)
        except Exception:
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001 - already broken
                pass
            raise
        finally:
            password = None
        self._client = client
        self._folder = None
        # Stamped here, not only after a fetch: _ensure's staleness
        # check reads it, and leaving it at 0 made the very next call
        # after a connect look older than the idle ceiling and throw
        # away a connection one command old.
        self._last_used = time.monotonic()
        logger.debug(
            "Opened a pooled IMAP fetch connection for %s.", account.account_id
        )

    def _drop(self):
        client, self._client, self._folder = self._client, None, None
        if client is None:
            return
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - the socket is being
            # discarded either way; a failed logout changes nothing.
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def _ensure(self, folder, background=False):
        stale = (
            self._client is not None
            and time.monotonic() - self._last_used > IDLE_CONNECTION_MAX_SECONDS
        )
        if stale:
            logger.debug(
                "Recycling the idle IMAP fetch connection for %s.",
                self._account.account_id,
            )
            self._drop()
        if self._client is None:
            self._connect(background=background)
        if self._folder != folder:
            # Read-WRITE, not readonly. Flag changes (mark read,
            # star) go over this same connection, and a readonly
            # selection cannot STORE. Nothing is marked read by
            # accident as a result: every body fetch uses BODY.PEEK,
            # which explicitly does not set \Seen. The alternative --
            # a readonly select for reads and a read-write one for
            # flags -- would re-SELECT the folder on every switch
            # between reading a message and marking it, which is the
            # single most common pair of operations in the app.
            self._client.select_folder(folder, readonly=False)
            self._folder = folder

    def fetch(self, message_id, folder, background=False):
        """
        Returns the raw RFC822 bytes of one message by UID, opening
        or reselecting as needed. One retry on a dropped connection,
        because a server that closed the socket while we were idle
        is the expected case, not an error worth surfacing.

        background=True (the prefetch sweep) never waits for the
        connection: if an interactive read already holds it, the
        prefetch reports itself unavailable and that one message
        stays unwarmed until the next round. The user's own read is
        never queued behind speculative work -- the same priority
        rule himalaya_client's per-account gate applies to
        subprocess calls.
        """
        if not self._lock.acquire(blocking=not background):
            raise ImapBodyFetchUnavailable(
                "connection busy with interactive work"
            )
        try:
            for attempt in (1, 2):
                try:
                    self._ensure(folder, background=background)
                    response = self._client.fetch([int(message_id)], ["BODY.PEEK[]"])
                except ImapBodyFetchUnavailable:
                    raise
                except (ValueError, TypeError) as exc:
                    raise ImapBodyFetchUnavailable(f"unusable message id: {exc}") from exc
                except Exception as exc:  # noqa: BLE001 - any protocol
                    # or socket failure: drop the connection and try
                    # once more from scratch, then give up to the
                    # subprocess fallback.
                    self._drop()
                    if attempt == 2:
                        raise ImapBodyFetchUnavailable(str(exc)) from exc
                    continue

                self._last_used = time.monotonic()
                data = response.get(int(message_id)) or {}
                raw = data.get(b"BODY[]") or data.get(b"RFC822")
                if not raw:
                    raise ImapBodyFetchUnavailable(
                        f"server returned no body for uid {message_id}"
                    )
                return raw
        finally:
            self._lock.release()
        raise ImapBodyFetchUnavailable("unreachable")

    def list_envelopes(self, folder, page_size=200, page=1, background=False):
        """
        One page of a folder's envelopes, newest first. See the
        module-level list_envelopes for the paging contract.
        """
        if not self._lock.acquire(blocking=not background):
            raise ImapBodyFetchUnavailable("connection busy with interactive work")
        try:
            for attempt in (1, 2):
                try:
                    # Logged below: the Trash-read race of 16 September
                    # (Drafts rows under the Trash target) left no trace
                    # because a pooled listing logged nothing at all.
                    previous_folder = self._folder
                    # A fresh SELECT every listing, not only when the
                    # folder changed. The NOOP below is not enough on
                    # Gmail: folder_uids and has_message_id already
                    # force a new selection for exactly this reason,
                    # and the poll showed it live -- mail moved into
                    # [Gmail]/Spam from another client never appeared
                    # while ZBox already had Spam selected, though the
                    # same test passed against disroot. A new SELECT is
                    # a new snapshot of the folder.
                    self._folder = None
                    self._ensure(folder, background=background)
                    # A NOOP before the search, deliberately. RFC 3501
                    # forbids the server from sending untagged EXPUNGE
                    # inside a SEARCH or FETCH response, and _ensure
                    # skips the re-SELECT when the folder has not
                    # changed -- so without this the session never
                    # learns about messages moved or deleted over the
                    # Himalaya subprocess connection, and every refresh
                    # returns the same stale uid set forever. Measured
                    # live: 124 uids before and after a ten-message
                    # permanent delete, so the list kept showing rows
                    # whose bodies the server would no longer fetch.
                    self._client.noop()
                    uids = self._client.search(["ALL"])
                    if not uids:
                        return []
                    uids = sorted(uids)
                    # Page 1 is the newest page_size messages, so
                    # count back from the end.
                    end = len(uids) - (page - 1) * page_size
                    start = max(0, end - page_size)
                    if end <= 0:
                        return []
                    window = uids[start:end]
                    if not window:
                        return []
                    response = self._client.fetch(
                        window, _list_items(),
                    )
                except ImapBodyFetchUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - see fetch()
                    self._drop()
                    if attempt == 2:
                        raise ImapBodyFetchUnavailable(str(exc)) from exc
                    continue

                self._last_used = time.monotonic()
                envelopes = []
                for uid in reversed(window):  # newest first
                    data = response.get(uid)
                    if data:
                        envelopes.append(_envelope_dict(uid, data))
                logger.debug(
                    "Pooled listing of %s on %s (was %s): %d uid(s), page %d, %d envelope(s).",
                    folder, self._account.account_id,
                    previous_folder or "nothing",
                    len(uids), page, len(envelopes),
                )
                return envelopes
        finally:
            self._lock.release()
        raise ImapBodyFetchUnavailable("unreachable")

    def store_flags(self, message_id, folder, flags, add):
        """
        Adds or removes IMAP flags on one message (STORE), on the
        already-open connection. Same one-retry-then-give-up shape as
        fetch: a dropped socket is the expected failure, and anything
        else hands back to the subprocess path.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._ensure(folder)
                    uid = int(message_id)
                    if add:
                        self._client.add_flags([uid], flags)
                    else:
                        self._client.remove_flags([uid], flags)
                except ImapBodyFetchUnavailable:
                    raise
                except (ValueError, TypeError) as exc:
                    raise ImapBodyFetchUnavailable(f"unusable message id: {exc}") from exc
                except Exception as exc:  # noqa: BLE001 - see fetch()
                    self._drop()
                    if attempt == 2:
                        raise ImapBodyFetchUnavailable(str(exc)) from exc
                    continue
                self._last_used = time.monotonic()
                return
        raise ImapBodyFetchUnavailable("unreachable")

    def _run_classified(self, folder, action, fresh):
        """
        Runs action(client) with `folder` selected, under the lock.

        A dropped connection is rebuilt and the action tried once
        more, then ImapBodyFetchUnavailable. A NO or BAD from the
        server raises ImapRefused at once and keeps the connection,
        which is still healthy. fresh=True re-SELECTs even when the
        folder is already selected, for the Gmail snapshot reason
        folder_uids explains.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    if fresh:
                        self._folder = None
                    self._ensure(folder)
                    result = action(self._client)
                except ImapBodyFetchUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - classified below
                    if _is_refusal(exc):
                        self._last_used = time.monotonic()
                        raise ImapRefused(str(exc)) from exc
                    self._drop()
                    if attempt == 2:
                        raise ImapBodyFetchUnavailable(str(exc)) from exc
                    continue
                self._last_used = time.monotonic()
                return result
        raise ImapBodyFetchUnavailable("unreachable")

    def move_uids(self, message_ids, folder, to_folder):
        """
        Moves messages by UID from `folder` to `to_folder` on the
        already-open connection: MOVE, or COPY plus a UID EXPUNGE of
        exactly those messages on a server with UIDPLUS but no MOVE.

        The Himalaya path opens a new connection for every move -- a
        DNS lookup, TLS and a login. Live, 24 September 2026, that took
        12.3 s to fail on "No such host is known" while this
        connection kept fetching bodies in under 0.3 s. Raises
        ImapBodyFetchUnavailable when an id is not a UID or the
        connection fails twice, ImapRefused when the server says no.
        """
        uids = _int_uids(message_ids)
        if not uids:
            return

        def action(client):
            if client.has_capability("MOVE"):
                client.move(uids, to_folder)
            elif client.has_capability("UIDPLUS"):
                client.copy(uids, to_folder)
                client.add_flags(uids, [imapclient.DELETED], silent=True)
                client.expunge(uids)
            else:
                raise ImapRefused("server has neither MOVE nor UIDPLUS")

        self._run_classified(folder, action, fresh=False)

    def find_uids(self, folder, message_id_header):
        """
        The UIDs in `folder` whose Message-ID is this one, from a
        fresh SELECT. The server does the search, so a row from the
        offline copy is found without matching any local list.

        SEARCH HEADER is a substring match, so each hit's own
        Message-ID is fetched and compared exactly before it counts.
        """
        wanted = canonical_message_id(message_id_header)
        if not wanted:
            return []

        def action(client):
            hits = client.search(["HEADER", "Message-ID", wanted])
            if not hits:
                return []
            data = client.fetch(hits, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
            found = []
            for uid, item in data.items():
                raw = b""
                for key, value in item.items():
                    if isinstance(key, bytes) and key.upper().startswith(b"BODY[") and isinstance(value, bytes):
                        raw = value
                        break
                header = email.message_from_bytes(raw).get("Message-ID") if raw else None
                if header is None or canonical_message_id(header) == wanted:
                    found.append(int(uid))
            return sorted(found)

        return self._run_classified(folder, action, fresh=True)

    def delete_uids(self, folder, message_ids):
        """
        Permanently removes exactly these UIDs from `folder`: \\Deleted,
        then UID EXPUNGE of those UIDs only. Returns "expunged", or
        "flagged_only" on a server without UIDPLUS, where a plain
        EXPUNGE would also remove anything else marked deleted, so
        nothing is expunged.
        """
        uids = _int_uids(message_ids)
        if not uids:
            return "expunged"

        def action(client):
            client.add_flags(uids, [imapclient.DELETED], silent=True)
            if client.has_capability("UIDPLUS"):
                client.expunge(uids)
                return "expunged"
            return "flagged_only"

        return self._run_classified(folder, action, fresh=True)

    def mark_folder_seen(self, folder):
        """
        Marks every unread message in `folder` read: a fresh SELECT, a
        NOOP, SEARCH UNSEEN, then one STORE +FLAGS \\Seen on exactly
        those UIDs. Returns how many were marked. Fresh for the Gmail
        snapshot reason folder_uids explains.
        """

        def action(client):
            client.noop()
            uids = sorted(int(uid) for uid in client.search(["UNSEEN"]) or ())
            if uids:
                client.add_flags(uids, [imapclient.SEEN], silent=True)
            return len(uids)

        return self._run_classified(folder, action, fresh=True)

    def folder_uids(self, folder):
        """
        Every UID currently in `folder`, ascending, from a fresh SELECT
        of it followed by a NOOP. Blocking, like store_flags -- only
        interactive work asks for this.

        A fresh SELECT even when the session already has the folder
        selected. A NOOP alone was not enough on Gmail: live, 16
        September 2026, the prefetch sweep had [Gmail]/Spam selected,
        a 63-message Mark as Not Junk moved every one to Inbox over the
        Himalaya connection, and SEARCH on the still-selected session
        listed some of them as present, so the batch reported a failure
        that had not happened. A new selection is a new snapshot of the
        folder. It costs one SELECT per batched move, never per read.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._folder = None
                    self._ensure(folder)
                    self._client.noop()
                    uids = self._client.search(["ALL"])
                except ImapBodyFetchUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - see fetch()
                    self._drop()
                    if attempt == 2:
                        raise ImapBodyFetchUnavailable(str(exc)) from exc
                    continue
                self._last_used = time.monotonic()
                return sorted(int(uid) for uid in uids or ())
        raise ImapBodyFetchUnavailable("unreachable")

    def has_message_id(self, folder, message_id_header):
        """
        Whether `folder` holds a message with this Message-ID, from a
        fresh SELECT and a HEADER search. Fresh for the same Gmail
        reason as folder_uids: a session that already has the folder
        selected can miss what another connection just added.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._folder = None
                    self._ensure(folder)
                    uids = self._client.search(["HEADER", "Message-ID", message_id_header])
                except ImapBodyFetchUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - see fetch()
                    self._drop()
                    if attempt == 2:
                        raise ImapBodyFetchUnavailable(str(exc)) from exc
                    continue
                self._last_used = time.monotonic()
                return bool(uids)
        raise ImapBodyFetchUnavailable("unreachable")

    def attachment_uids(self, folder, message_ids):
        """
        Which of these UIDs in `folder` have an attachment, read from
        each message's BODYSTRUCTURE the way a listing with the
        attachment column marks rows (_has_attachment). IMAP SEARCH has
        no key for attachments, so the Search tab checks the results the
        server already matched this way.
        """
        uids = []
        for value in message_ids:
            try:
                uids.append(int(str(value)))
            except (TypeError, ValueError):
                continue
        if not uids:
            return []

        def action(client):
            data = client.fetch(uids, ["BODYSTRUCTURE"])
            return sorted(
                int(uid) for uid, item in data.items()
                if _has_attachment(item.get(b"BODYSTRUCTURE"))
            )

        return self._run_classified(folder, action, fresh=False)

    def close(self, wait=0.5):
        """
        Logs out, waiting at most `wait` seconds for the lock. A
        connection still busy (a connect timing out, say) is left to
        end with the app: its worker is a daemon thread. Returns
        whether it was closed.
        """
        if not self._lock.acquire(timeout=wait):
            logger.debug(
                "Pooled IMAP connection for %s still busy at quit; left to end with the app.",
                self._account.account_id,
            )
            return False
        try:
            self._drop()
        finally:
            self._lock.release()
        return True


_POOL = {}
_POOL_LOCK = threading.Lock()
# Set by close_all when ZBox quits; _connect then refuses to open a
# new connection.
_CLOSING = False


def _pooled(account, paths):
    connection = _POOL.get(account.account_id)
    if connection is None:
        with _POOL_LOCK:
            connection = _POOL.get(account.account_id)
            if connection is None:
                connection = _PooledConnection(account, paths)
                _POOL[account.account_id] = connection
    return connection


def fetch_message(paths, account, message_id, folder="INBOX", background=False):
    """
    The whole public surface: one message body, in himalaya's own
    'message read' shape, over a connection that stays open between
    calls. Raises ImapBodyFetchUnavailable for every failure mode,
    which himalaya_client treats as "use the subprocess instead".

    background=True marks speculative work (the prefetch sweep) that
    must yield the connection to a real read rather than making the
    user wait behind it.
    """
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    started = time.monotonic()
    raw = _pooled(account, paths).fetch(message_id, folder, background=background)
    try:
        message = build_message_dict(raw)
    except Exception as exc:  # noqa: BLE001 - a message this parser
        # chokes on is exactly what the subprocess fallback is for.
        raise ImapBodyFetchUnavailable(f"could not parse message: {exc}") from exc
    logger.debug(
        "Fetched message %s from %s over the pooled connection in %.2fs.",
        message_id, folder, time.monotonic() - started,
    )
    return message


def fetch_message_raw(paths, account, message_id, folder="INBOX", background=False):
    """
    The message's full raw RFC822 source as text, over the same
    pooled connection. Used by Save As and by the offline Maildir
    sync, which reads every message it copies -- a real session's log
    measured one of those at 22.1 seconds through the subprocess
    path, because that path pays a full IMAP login per message.
    Nothing is parsed here: this is the bytes the server sent.
    """
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    raw = _pooled(account, paths).fetch(message_id, folder, background=background)
    return raw.decode("utf-8", errors="replace")


_FLAG_IANA = {
    b"\\Seen": "seen",
    b"\\Answered": "answered",
    b"\\Flagged": "flagged",
    b"\\Deleted": "deleted",
    b"\\Draft": "draft",
    b"\\Recent": "recent",
}


def fetch_message_bytes(paths, account, message_id, folder="INBOX", background=False):
    """
    The message exactly as the server holds it, as bytes, over the same
    pooled connection: what an OpenPGP signature is checked against
    (openpgp_read). Not decoded, not parsed, not cached.
    """
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return bytes(_pooled(account, paths).fetch(message_id, folder, background=background))


def _flag_entries(raw_flags):
    """
    IMAP flags in the shape envelope consumers read: a list of
    {"raw": "\\Seen", "iana": "seen"}. A keyword the standard does
    not define (Junk, $Forwarded, a provider's own label) keeps its
    raw name with iana None, exactly as Himalaya reports it --
    _envelope_is_unread and _envelope_is_flagged both match on
    "iana", so a keyword must not accidentally claim one of those.
    """
    entries = []
    for flag in raw_flags or ():
        if isinstance(flag, bytes):
            text = flag.decode("utf-8", errors="replace")
            iana = _FLAG_IANA.get(flag)
        else:
            text = str(flag)
            iana = _FLAG_IANA.get(text.encode("utf-8"))
        entries.append({"raw": text, "iana": iana})
    return entries


def _address_entries(raw):
    """
    An IMAP ENVELOPE address list as [{"name": ..., "email": ...}].

    Note the key is "email", NOT "address": envelope addresses and
    parsed message-header addresses use different keys for the same
    thing, which message_body.py's own header notes call out as a
    trap. This is the envelope side.
    """
    entries = []
    for address in raw or ():
        mailbox = getattr(address, "mailbox", None)
        host = getattr(address, "host", None)
        if not mailbox or not host:
            continue
        email_text = "%s@%s" % (
            mailbox.decode("utf-8", errors="replace"),
            host.decode("utf-8", errors="replace"),
        )
        name = getattr(address, "name", None)
        entries.append({
            "name": _decoded(name.decode("utf-8", errors="replace")) if name else "",
            "email": email_text,
        })
    return entries


# What a listing asks for beyond the envelope, flags and arrival date
# (View > Sort By and View > Columns). The size always: one number per
# message, measured at no cost worth naming. The message structure, for
# attachments, and the priority headers only while a shown column or the
# chosen sort needs them (main_frame._update_list_extras), so a list
# nobody sorts by attachment stays as light as it was.
_PRIORITY_FIELDS = "BODY.PEEK[HEADER.FIELDS (X-PRIORITY IMPORTANCE PRIORITY X-MSMAIL-PRIORITY)]"
_LIST_EXTRAS = {"structure": False, "priority": False}


def set_list_extras(structure=False, priority=False):
    """Whether listings also ask for the message structure (attachments)
    and the priority headers."""
    _LIST_EXTRAS["structure"] = bool(structure)
    _LIST_EXTRAS["priority"] = bool(priority)


def list_extras():
    """(structure, priority): what listings ask for now."""
    return (_LIST_EXTRAS["structure"], _LIST_EXTRAS["priority"])


def _list_items():
    items = ["ENVELOPE", "FLAGS", "INTERNALDATE", "RFC822.SIZE"]
    if _LIST_EXTRAS["structure"]:
        items.append("BODYSTRUCTURE")
    if _LIST_EXTRAS["priority"]:
        items.append(_PRIORITY_FIELDS)
    return items


def _has_attachment(structure):
    """True when any part of a BODYSTRUCTURE is marked as an attachment."""
    if isinstance(structure, bytes):
        return structure.lower() == b"attachment"
    if isinstance(structure, (list, tuple)):
        return any(_has_attachment(item) for item in structure)
    return False


def _priority_from(data):
    """1 (highest) to 5 (lowest) from a row's priority headers, or None
    when it has none. X-Priority first, as Thunderbird reads it, then
    Importance and X-MSMail-Priority, then Priority."""
    for key, value in data.items():
        if not (isinstance(key, bytes) and key.upper().startswith(b"BODY[HEADER")):
            continue
        if not isinstance(value, bytes):
            continue
        headers = email.message_from_bytes(value)
        number = str(headers.get("X-Priority") or "").strip()
        if number[:1] in ("1", "2", "3", "4", "5"):
            return int(number[0])
        for name in ("Importance", "X-MSMail-Priority"):
            word = str(headers.get(name) or "").strip().lower()
            if word == "high":
                return 2
            if word == "low":
                return 4
            if word == "normal":
                return 3
        word = str(headers.get("Priority") or "").strip().lower()
        if word == "urgent":
            return 2
        if word == "non-urgent":
            return 4
    return None


def _envelope_dict(uid, data):
    """
    One FETCH response row as the envelope dict the rest of ZBox
    reads: id, from/to/cc address lists, subject, an ISO-8601 date,
    flags, message-id and in-reply-to. Same shape himalaya's
    'envelope list --json' produces, because envelope_format,
    thread_grouping and the whole message list already speak it.
    """
    envelope = data.get(b"ENVELOPE")
    flags = _flag_entries(data.get(b"FLAGS"))

    subject = b""
    date_value = None
    message_id = b""
    in_reply_to = b""
    if envelope is not None:
        subject = getattr(envelope, "subject", b"") or b""
        date_value = getattr(envelope, "date", None)
        message_id = getattr(envelope, "message_id", b"") or b""
        in_reply_to = getattr(envelope, "in_reply_to", b"") or b""

    if date_value is None:
        date_value = data.get(b"INTERNALDATE")
    if date_value is not None and date_value.tzinfo is None:
        # imapclient hands ENVELOPE and INTERNALDATE dates over already
        # converted to this PC's local time, with the offset dropped
        # (its parse_to_datetime normalises by default). Treating that
        # as UTC added the local offset a second time: every message
        # listed over this connection showed an hour ahead in UTC+1,
        # live 24 September 2026. astimezone() on a naive datetime
        # reads it as local time, which is what it is.
        try:
            date_value = date_value.astimezone()
        except (ValueError, OSError, OverflowError):
            date_value = date_value.replace(tzinfo=datetime.timezone.utc)

    def text(value):
        if isinstance(value, bytes):
            return _decoded(value.decode("utf-8", errors="replace"))
        return _decoded(value) if value else ""

    result = {
        "id": str(uid),
        "subject": text(subject),
        # isoformat(), matching what _parse_envelope_date expects --
        # a fixed-format string carrying each message's own offset.
        "date": date_value.isoformat() if date_value is not None else "",
        "flags": flags,
        "from": _address_entries(getattr(envelope, "from_", None) if envelope else None),
        "to": _address_entries(getattr(envelope, "to", None) if envelope else None),
        "cc": _address_entries(getattr(envelope, "cc", None) if envelope else None),
    }
    # The server's own arrival date (View > Sort By > Received), in the
    # same local-time form as "date" above.
    received = data.get(b"INTERNALDATE")
    if received is not None:
        if received.tzinfo is None:
            try:
                received = received.astimezone()
            except (ValueError, OSError, OverflowError):
                received = received.replace(tzinfo=datetime.timezone.utc)
        result["received"] = received.isoformat()
    # Himalaya's own names for these two, so one helper reads rows
    # from either path.
    size = data.get(b"RFC822.SIZE")
    if isinstance(size, int):
        result["size"] = size
    if b"BODYSTRUCTURE" in data:
        result["has-attachment"] = _has_attachment(data.get(b"BODYSTRUCTURE"))
    priority = _priority_from(data)
    if priority is not None:
        result["priority"] = priority
    message_id_text = text(message_id)
    if message_id_text:
        result["message-id"] = message_id_text
    in_reply_to_text = text(in_reply_to)
    # A list, which is the shape thread_grouping's envelope_parent_id
    # already handles and the shape himalaya reports.
    result["in-reply-to"] = [in_reply_to_text] if in_reply_to_text else []
    return result


def list_envelopes(paths, account, folder="INBOX", page_size=200, page=1, background=False):
    """
    A folder listing over the pooled connection, newest first.

    Through the subprocess this is a fresh IMAP login per folder
    switch: a real session's log measured 13.7s and 21.8s for a
    single 'envelope list'. It is the last cold-subprocess call on
    the path the user actually feels.

    Paging matches himalaya's own: page 1 is the most recent
    page_size messages. UIDs ascend with arrival, so the newest are
    the highest -- the slice is taken from the end and reversed.
    """
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return _pooled(account, paths).list_envelopes(
        folder, page_size=page_size, page=page, background=background,
    )


def set_flag(paths, account, message_id, folder, flag, on):
    """
    Sets or clears one IMAP flag over the pooled connection.

    'flag' is the bare IMAP name ("seen", "flagged"); it is sent as
    the backslash-prefixed system flag the protocol expects. Marking
    a message read runs immediately after every open, so through the
    subprocess path it was a full IMAP login to send one STORE.
    """
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    system_flag = "\\" + str(flag).strip().lstrip("\\").capitalize()
    _pooled(account, paths).store_flags(
        message_id, folder, [system_flag], add=bool(on),
    )


def folder_uids(paths, account, folder):
    """
    Every UID in `folder`, ascending, over the pooled connection --
    one NOOP and one SEARCH. The batched delete reads Trash's highest
    UID before moving anything, and each source folder's UIDs after,
    to know exactly which messages moved and which Trash copies are
    the ones it just created.
    """
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return _pooled(account, paths).folder_uids(folder)


def mark_folder_read(paths, account, folder):
    """Marks every unread message in `folder` read over the pooled
    connection (Folder tree > Mark Folder as Read). Returns the count
    marked. Raises ImapBodyFetchUnavailable on any failure; there is
    no subprocess fallback, the caller reports the error."""
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return _pooled(account, paths).mark_folder_seen(folder)


def move_uids(paths, account, message_ids, folder, to_folder):
    """Moves messages by UID over the pooled connection. Raises
    ImapBodyFetchUnavailable for every failure (ImapRefused when the
    server said no); himalaya_client then moves them through the
    subprocess instead."""
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    _pooled(account, paths).move_uids(message_ids, folder, to_folder)


def find_uids(paths, account, folder, message_id_header):
    """UIDs in `folder` carrying this Message-ID, searched by the
    server over the pooled connection."""
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return _pooled(account, paths).find_uids(folder, message_id_header)


def delete_uids(paths, account, folder, message_ids):
    """Permanently removes these UIDs from `folder` over the pooled
    connection. "expunged" or "flagged_only"; see the method."""
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return _pooled(account, paths).delete_uids(folder, message_ids)


def sent_copy_exists(paths, account, folder, message_id_header):
    """True when `folder` already holds the message with this
    Message-ID. Used after a send to decide whether ZBox must save
    the Sent copy itself."""
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return _pooled(account, paths).has_message_id(folder, message_id_header)


def attachment_uids(paths, account, folder, message_ids):
    """Of these UIDs in `folder`, the ones with an attachment, over the
    pooled connection (the Search tab's Has attachment)."""
    if imapclient is None:
        raise ImapBodyFetchUnavailable("imapclient is not installed")
    return _pooled(account, paths).attachment_uids(folder, message_ids)


def close_all():
    """
    Closes every pooled connection. Called from the frame's close
    handler alongside himalaya_client.shutdown(), so ZBox doesn't
    leave authenticated IMAP sessions open after its window is gone.
    """
    global _CLOSING
    _CLOSING = True
    with _POOL_LOCK:
        connections = list(_POOL.values())
        _POOL.clear()
    closed = 0
    for connection in connections:
        if connection.close():
            closed += 1
    return closed


# --- Personal Microsoft accounts through Microsoft Graph ---------------
#
# Each function named in graph_client.POOLED_ROUTES sends a Graph account
# to its Graph route, in the shape the pooled connection gives, and every
# other account to the function above, unchanged. That covers
# himalaya_client's fast paths, delete_queue, the Search tab and Mark
# Folder as Read.
import graph_client as _graph_client  # noqa: E402

_graph_client.route_module(globals(), _graph_client.POOLED_ROUTES)

_close_all_connections = close_all


def close_all():
    """Closes every pooled connection, and saves the Graph accounts'
    message numbers still waiting to be written."""
    closed = _close_all_connections()
    try:
        _graph_client.close_all()
    except Exception:  # noqa: BLE001 - quitting
        logger.debug("Could not save the Graph message numbers.", exc_info=True)
    return closed
