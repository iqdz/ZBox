"""
Personal Microsoft accounts (Outlook.com, Hotmail, Live, MSN and their
regional forms) through Microsoft Graph instead of IMAP and SMTP.

Why
---
Microsoft's IMAP server answers a personal account's XOAUTH2 login with
"User is authenticated but not connected" for many people since
December 2024, and IMAP is off by default in Outlook.com's own
settings. Graph needs neither, and the permissions it needs
(Mail.ReadWrite, Mail.Send) are asked for at sign-in, with no change to
the app registration. Microsoft 365 work and school accounts stay on
IMAP and SMTP.

How it fits
-----------
Nothing above himalaya_client and imap_body_fetch changes. Their public
functions are routed here for a Graph account (see the routing blocks
at the end of each of those modules), and every answer comes back in
the shape the IMAP paths give: envelope dicts, the 'message read'
message dict (built from the MIME source by imap_body_fetch), folder
entries, raw text and bytes.

Message numbers
---------------
ZBox treats a message id as an IMAP UID: a number, unique in its
folder, never reused, and a moved message gets a new, higher one in its
new folder. Graph ids are long text. So every (folder, Graph id) pair
gets the next number of that folder the first time it is seen, and the
numbers are kept in graph_numbers.json in the account's cache folder,
next to the on-disk message cache. If that file is missing, the
account's message cache is cleared before any number is handed out, so
a cached message can never be shown under another message's number.
Graph is asked for immutable ids, which stay the same when a message
moves.

Requests
--------
Standard library only. Each request takes the same per-account gate as
a Himalaya call, so your own actions go first and background work
stands down. A 401 refreshes the token once; a 429 or 503 waits the
time Microsoft gives (at most ten seconds) once for your own actions,
while background work stands down instead. Small changes go in batch
requests of twenty.

Large messages
--------------
Graph takes a MIME message of about 4 MB per request. A message larger
than MIME_WHOLE_LIMIT is created as a draft without its attachments
(headers and text stay exactly as written), each attachment is added to
it (a large one through Graph's upload method, in pieces), and then it is
sent. A PGP/MIME signed or encrypted message cannot be split without
breaking it, so one over the limit is refused before sending.
"""

import base64
import datetime
import email
import email.policy
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import ms_oauth

logger = logging.getLogger("zbox.graph")

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT_SECONDS = 20
BACKGROUND_TIMEOUT_SECONDS = 8
UPLOAD_TIMEOUT_SECONDS = 120
MAX_RETRY_WAIT_SECONDS = 10
BATCH_SIZE = 20
PAGE_MAX = 1000
# A raw message up to this size is sent or saved whole: base64 makes
# it about 3.7 MB, under Graph's 4 MB request limit.
MIME_WHOLE_LIMIT = 2800000
# An attachment up to this size is added in one request; a larger one
# is uploaded in pieces.
SMALL_ATTACHMENT_LIMIT = 2500000
# Upload pieces must be a multiple of 320 KiB.
UPLOAD_CHUNK = 320 * 1024 * 10
NUMBERS_FILE_NAME = "graph_numbers.json"
_PREFER = 'IdType="ImmutableId"'

# ZBox's names for the special folders, and Graph's fixed names for
# them. The names are the ones ZBox already uses for this server
# (provider_presets has no override for it), so everything above sees
# the folders it expects.
SPECIAL_FOLDERS = (
    ("INBOX", "inbox", ""),
    ("Sent", "sentitems", "\\Sent"),
    ("Drafts", "drafts", "\\Drafts"),
    ("Trash", "deleteditems", "\\Trash"),
    ("Junk", "junkemail", "\\Junk"),
    ("Archive", "archive", "\\Archive"),
)

# Extended properties read with a listing: In-Reply-To, the size and
# the last action (102 reply, 103 reply all, 104 forward).
_PROP_IN_REPLY_TO = ("string", 0x1042)
_PROP_SIZE = ("integer", 0x0E08)
_PROP_LAST_VERB = ("integer", 0x1081)
_EXPAND = (
    "singleValueExtendedProperties($filter=id eq 'String 0x1042' "
    "or id eq 'Integer 0x0E08' or id eq 'Integer 0x1081')"
)
_SELECT = (
    "id,subject,from,toRecipients,ccRecipients,sentDateTime,receivedDateTime,"
    "isRead,isDraft,flag,hasAttachments,internetMessageId,importance"
)
# Cleared until ZBox closes if Graph refuses the expanded
# listing, so a listing never fails for want of the three extras.
_EXPAND_STATE = {"ok": True}

_LAST = threading.local()


class GraphFailure(Exception):
    """One Graph request that did not work. kind is one of: network,
    busy, closing, signin, refused, notfound, toolarge."""

    def __init__(self, kind, text=""):
        super().__init__(text or kind)
        self.kind = kind
        self.text = text or kind


# --- Which accounts ----------------------------------------------------

def uses_graph(account):
    """True for an account that signs in with a Microsoft account and
    whose address is a personal Microsoft address. Every routing point
    asks only this."""
    if account is None:
        return False
    try:
        if getattr(account, "auth_method", "password") != "microsoft":
            return False
        address = getattr(account, "login_email", "") or getattr(account, "identity_email", "")
    except Exception:  # noqa: BLE001 - an odd stand-in is not a Graph account
        return False
    return isinstance(address, str) and ms_oauth.personal_address(address)


# --- Per-account state -------------------------------------------------

class _State:
    def __init__(self, account_id):
        self.account_id = account_id
        self.lock = threading.RLock()
        self.folders = None  # list of folder entries, or None
        self.special = {}  # ZBox name casefolded -> folder id
        self.numbers = None  # {"folders": {fid: {"next": n, "ids": {gid: n}}}}
        self.reverse = {}  # fid -> {number: gid}
        self.dirty = False
        self.tenant_checked = False
        self.paths = None
        self.account = None


_STATES = {}
_STATES_LOCK = threading.Lock()


def _state(account):
    with _STATES_LOCK:
        state = _STATES.get(account.account_id)
        if state is None:
            state = _State(account.account_id)
            _STATES[account.account_id] = state
        return state


def forget_account(account_id):
    """Drops what is held for one account (after a new sign-in, say)."""
    with _STATES_LOCK:
        _STATES.pop(account_id, None)


# --- Errors for the two layers above -----------------------------------

def _remember(exc):
    _LAST.failure = exc


def take_last_failure():
    failure = getattr(_LAST, "failure", None)
    _LAST.failure = None
    return failure


def _signin_error(paths, account):
    import himalaya_client

    error = himalaya_client._signin_needed_error(paths, account, ms_oauth.SignInNeeded())
    if error is not None:
        return error
    return himalaya_client.MicrosoftSignInNeeded("Microsoft sign-in is needed again.")


def as_himalaya_error(paths, account, exc):
    """The exception a himalaya_client caller expects for a Graph failure."""
    import himalaya_client

    if exc.kind == "busy":
        return himalaya_client.HimalayaBackgroundSkipped(exc.text)
    if exc.kind == "closing":
        return himalaya_client.HimalayaShuttingDown(exc.text)
    if exc.kind == "signin":
        return _signin_error(paths, account)
    return himalaya_client.HimalayaError(exc.text)


def as_pooled_error(paths, account, exc):
    """The exception an imap_body_fetch caller expects: network trouble
    is ImapBodyFetchUnavailable (delete_queue retries it), a refusal is
    ImapRefused. Sign-in, stand-down and quitting pass as they would
    from Himalaya. The failure is kept for blocked_error, since
    himalaya_client falls back to its subprocess after either."""
    import imap_body_fetch

    if exc.kind in ("busy", "closing", "signin"):
        return as_himalaya_error(paths, account, exc)
    _remember(exc)
    if exc.kind == "network":
        return imap_body_fetch.ImapBodyFetchUnavailable(exc.text)
    return imap_body_fetch.ImapRefused(exc.text)


def blocked_error(paths, account, args):
    """Raised by himalaya_client._run instead of starting Himalaya with
    IMAP for a Graph account: the Graph failure that led there, or a
    plain error naming the call."""
    import himalaya_client

    failure = take_last_failure()
    if failure is not None:
        return as_himalaya_error(paths, account, failure)
    logger.warning(
        "Himalaya was asked to use IMAP for the Graph account %s (%s); refused.",
        getattr(account, "account_id", "?"), " ".join(str(a) for a in list(args)[:2]),
    )
    return himalaya_client.HimalayaError(
        "This action is not available for this Outlook.com account."
    )


# --- Requests ----------------------------------------------------------

def _token(paths, account, force=False):
    try:
        if force:
            return ms_oauth.get_access_token(paths.config, account.account_id, margin=10 ** 9)
        return ms_oauth.get_access_token(paths.config, account.account_id)
    except ms_oauth.SignInNeeded as exc:
        raise GraphFailure("signin", str(exc))
    except ms_oauth.OAuthError as exc:
        kind = "network" if exc.kind in ("network", "busy") else "refused"
        raise GraphFailure(kind, "Microsoft sign-in: %s" % (exc.detail or exc.kind))


def _check_tenant(paths, account):
    """Logs, once per run of ZBox, a sign-in whose tenant is not the one all
    personal Microsoft accounts share."""
    state = _state(account)
    if state.tenant_checked:
        return
    state.tenant_checked = True
    try:
        record = ms_oauth.load_record(paths.config, account.account_id) or {}
    except Exception:  # noqa: BLE001 - only a log line
        return
    tenant = str(record.get("tenant") or "")
    if tenant and tenant != ms_oauth.PERSONAL_TENANT:
        logger.warning(
            "Account %s is signed in to a work or school tenant but uses Graph by its address.",
            account.account_id,
        )


class _Gate:
    """The per-account gate himalaya_client's _run takes, held for one
    request."""

    def __init__(self, account):
        import himalaya_client

        if getattr(himalaya_client, "_SHUTTING_DOWN", False):
            raise GraphFailure("closing", "ZBox is closing")
        self.background = bool(getattr(himalaya_client._BACKGROUND_LOCAL, "active", False))
        self.gate = himalaya_client._account_gate(account.account_id)
        if self.background:
            if not self.gate.try_enter_background():
                raise GraphFailure("busy", "background Graph request stood down")
        else:
            self.gate.enter_interactive()

    def leave(self):
        self.gate.leave()


def _error_text(status, payload):
    try:
        data = json.loads(payload.decode("utf-8"))
        error = data.get("error") or {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    except Exception:  # noqa: BLE001 - no readable error body
        code, message = "", ""
    text = "Microsoft refused the request (%s)" % status
    if code:
        text += " %s" % code
    if message:
        text += ": %s" % message
    return text


def _retry_wait(headers):
    try:
        value = float((headers or {}).get("Retry-After") or 2)
    except (TypeError, ValueError):
        value = 2
    return max(1.0, min(value, MAX_RETRY_WAIT_SECONDS))


def _open(request, timeout):
    """One HTTP exchange: (status, headers, body bytes). Network
    trouble raises GraphFailure("network")."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except OSError:
            body = b""
        return exc.code, exc.headers, body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GraphFailure("network", "Could not reach Microsoft: %s" % getattr(exc, "reason", exc))


# Characters left as they are in a request address; everything else
# (spaces in $orderby and $filter, above all) is percent-encoded. "%"
# stays, so a part encoded already is not encoded twice.
_URL_SAFE = ":/?&=$,'()*+;%@!~-._#"


def _url(path):
    full = path if path.startswith("https://") else GRAPH_ROOT + path
    return urllib.parse.quote(full, safe=_URL_SAFE)


def request(paths, account, method, path, body=None, content_type=None, raw=False, timeout=None):
    """One Graph request with the token, the gate, one refresh on 401
    and one wait on 429 or 503. body is a dict (sent as JSON) or bytes
    (sent with content_type). Returns the parsed JSON answer, the bytes
    when raw is True, or None for an empty answer."""
    _check_tenant(paths, account)
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
        content_type = "application/json"
    else:
        data = body
    if data is None and method in ("POST", "PUT", "PATCH"):
        data = b""
    force = False
    waited = False
    while True:
        token = _token(paths, account, force=force)
        headers = {"Authorization": "Bearer " + token, "Prefer": _PREFER}
        if not raw:
            headers["Accept"] = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(_url(path), data=data, method=method, headers=headers)
        gate = _Gate(account)
        try:
            limit = timeout or (BACKGROUND_TIMEOUT_SECONDS if gate.background else REQUEST_TIMEOUT_SECONDS)
            status, answer_headers, payload = _open(req, limit)
        finally:
            gate.leave()
        if status == 401 and not force:
            force = True
            continue
        if status in (429, 503, 504):
            if gate.background:
                raise GraphFailure("busy", "Microsoft asked to wait (%s)" % status)
            if not waited:
                waited = True
                time.sleep(_retry_wait(answer_headers))
                continue
            raise GraphFailure("network", _error_text(status, payload))
        if status == 401:
            raise GraphFailure("signin", _error_text(status, payload))
        if status == 404:
            raise GraphFailure("notfound", _error_text(status, payload))
        if status >= 500:
            raise GraphFailure("network", _error_text(status, payload))
        if status >= 400:
            raise GraphFailure("refused", _error_text(status, payload))
        if raw:
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise GraphFailure("refused", "Microsoft sent an answer ZBox could not read.")


def _get_all(paths, account, path, limit):
    """Every item of a paged answer, following its next links, up to
    limit items."""
    items = []
    next_path = path
    while next_path and len(items) < limit:
        answer = request(paths, account, "GET", next_path) or {}
        items.extend(answer.get("value") or [])
        next_path = answer.get("@odata.nextLink")
    return items[:limit]


def batch(paths, account, calls):
    """Runs (method, path, body) calls in batch requests of twenty.
    Returns (status, body) per call, in order. A call Microsoft asked
    to wait on is tried once more after the wait it gave."""
    results = [None] * len(calls)
    pending = list(range(len(calls)))
    for attempt in (1, 2):
        retry = []
        for start in range(0, len(pending), BATCH_SIZE):
            chunk = pending[start:start + BATCH_SIZE]
            entries = []
            for index in chunk:
                method, path, body = calls[index]
                entry = {"id": str(index), "method": method,
                         "url": urllib.parse.quote(path, safe=_URL_SAFE),
                         "headers": {"Prefer": _PREFER}}
                if body is not None:
                    entry["body"] = body
                    entry["headers"]["Content-Type"] = "application/json"
                entries.append(entry)
            answer = request(paths, account, "POST", "/$batch", {"requests": entries}) or {}
            wait = 0
            for item in answer.get("responses") or []:
                try:
                    index = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                status = int(item.get("status") or 0)
                if status in (429, 503, 504) and attempt == 1:
                    retry.append(index)
                    wait = max(wait, _retry_wait(item.get("headers") or {}))
                    continue
                results[index] = (status, item.get("body"))
            if wait and attempt == 1:
                time.sleep(wait)
        pending = retry
        if not pending:
            break
    for index, result in enumerate(results):
        if result is None:
            results[index] = (0, None)
    return results


def _quote(graph_id):
    return urllib.parse.quote(str(graph_id), safe="")


# --- Folders -----------------------------------------------------------

def _special_ids(paths, account):
    """ZBox's special folder names (casefolded) to Graph folder ids,
    read once per run of ZBox in one batch."""
    state = _state(account)
    with state.lock:
        if state.special:
            return state.special
    calls = [("GET", "/me/mailFolders/%s?$select=id" % wellknown, None)
             for _name, wellknown, _attr in SPECIAL_FOLDERS]
    found = {}
    for (name, _wellknown, _attr), (status, body) in zip(SPECIAL_FOLDERS, batch(paths, account, calls)):
        if status == 200 and isinstance(body, dict) and body.get("id"):
            found[name.casefold()] = body["id"]
    if "inbox" not in found:
        raise GraphFailure("network", "Microsoft did not report the Inbox folder.")
    with state.lock:
        state.special = found
    return found


def _list_children(paths, account, parent_id, parent_name, special_by_id, out, depth):
    fields = "$top=250&$select=id,displayName,parentFolderId,childFolderCount,totalItemCount,unreadItemCount"
    if parent_id is None:
        path = "/me/mailFolders?" + fields
    else:
        path = "/me/mailFolders/%s/childFolders?%s" % (_quote(parent_id), fields)
    for folder in _get_all(paths, account, path, 5000):
        folder_id = folder.get("id")
        if not folder_id:
            continue
        special = special_by_id.get(folder_id)
        if special is not None:
            name, attribute = special
        else:
            display = str(folder.get("displayName") or "").strip() or "Folder"
            name = display if parent_name is None else "%s/%s" % (parent_name, display)
            attribute = ""
        out.append({
            "name": name, "delimiter": "/", "attributes": attribute,
            "total": folder.get("totalItemCount"), "unread": folder.get("unreadItemCount"),
            "id": folder_id,
        })
        if int(folder.get("childFolderCount") or 0) > 0 and depth < 10:
            _list_children(paths, account, folder_id, name, special_by_id, out, depth + 1)


def folder_entries(paths, account, refresh=True):
    """Every folder, as Himalaya's mailbox listing gives them: name,
    delimiter, attributes, total and unread (plus the Graph id)."""
    state = _state(account)
    if not refresh:
        with state.lock:
            if state.folders is not None:
                return list(state.folders)
    special = _special_ids(paths, account)
    special_by_id = {}
    for name, _wellknown, attribute in SPECIAL_FOLDERS:
        folder_id = special.get(name.casefold())
        if folder_id:
            special_by_id[folder_id] = (name, attribute)
    entries = []
    _list_children(paths, account, None, None, special_by_id, entries, 0)
    with state.lock:
        state.folders = entries
    return list(entries)


def _missing_folder(name):
    return GraphFailure("notfound", "NO Mailbox doesn't exist: %s" % name)


def folder_id(paths, account, name):
    """The Graph id of a ZBox folder name: the special ones by Graph's
    fixed names, the others from the folder list, read again once when
    the name is not in it."""
    key = str(name or "").strip().casefold()
    if not key:
        raise _missing_folder(name)
    special = _special_ids(paths, account)
    if key in special:
        return special[key]
    for refresh in (False, True):
        for entry in folder_entries(paths, account, refresh=refresh):
            if str(entry.get("name") or "").casefold() == key:
                return entry["id"]
    raise _missing_folder(name)


def _forget_folders(account):
    state = _state(account)
    with state.lock:
        state.folders = None


def create_folder(paths, account, name):
    parent, _, leaf = str(name).rpartition("/")
    if parent:
        path = "/me/mailFolders/%s/childFolders" % _quote(folder_id(paths, account, parent))
    else:
        path = "/me/mailFolders"
    request(paths, account, "POST", path, {"displayName": leaf})
    _forget_folders(account)


def delete_folder(paths, account, name):
    request(paths, account, "DELETE", "/me/mailFolders/%s" % _quote(folder_id(paths, account, name)))
    _forget_folders(account)


def rename_folder(paths, account, name, new_name):
    leaf = str(new_name).rpartition("/")[2]
    request(paths, account, "PATCH", "/me/mailFolders/%s" % _quote(folder_id(paths, account, name)),
            {"displayName": leaf})
    _forget_folders(account)


# --- Message numbers ---------------------------------------------------

def _numbers_path(paths, account):
    return os.path.join(paths.cache_dir(account.account_id), NUMBERS_FILE_NAME)


def _clear_message_cache(paths, account):
    """The account's on-disk and in-memory message cache, cleared when
    the number map is missing."""
    try:
        import himalaya_client

        for key in list(himalaya_client._read_cache):
            if key and key[0] == account.account_id:
                himalaya_client._read_cache.pop(key, None)
    except Exception:  # noqa: BLE001 - the disk cache below still goes
        pass
    folder = os.path.join(paths.cache_dir(account.account_id), "message_cache")
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
        logger.info("Cleared the message cache of %s: its message numbers were missing.",
                    account.account_id)


def _numbers(paths, account):
    """The account's number map, loaded once. Call with the lock held."""
    state = _state(account)
    if state.numbers is not None:
        return state.numbers
    data = None
    path = _numbers_path(paths, account)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("folders"), dict):
        _clear_message_cache(paths, account)
        data = {"version": 1, "folders": {}}
        state.dirty = True
    state.numbers = data
    state.reverse = {}
    for fid, entry in data["folders"].items():
        ids = entry.get("ids") if isinstance(entry, dict) else None
        if not isinstance(ids, dict):
            data["folders"][fid] = {"next": 1, "ids": {}}
            continue
        state.reverse[fid] = {int(number): gid for gid, number in ids.items()}
    return data


def _save_numbers(paths, account):
    """Writes the map when it changed: a new file, then a replace."""
    import private_mode

    if private_mode.ACTIVE:
        return  # kept in memory only in private mode
    state = _state(account)
    with state.lock:
        if not state.dirty or state.numbers is None:
            return
        text = json.dumps(state.numbers)
        state.dirty = False
    path = _numbers_path(paths, account)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except OSError:
        logger.warning("Could not save the message numbers of %s.", account.account_id, exc_info=True)
        with state.lock:
            state.dirty = True


def _folder_numbers(data, fid):
    entry = data["folders"].get(fid)
    if not isinstance(entry, dict):
        entry = {"next": 1, "ids": {}}
        data["folders"][fid] = entry
    return entry


def number_for(paths, account, fid, gid):
    """The number of (folder, Graph id), given now if it has none."""
    state = _state(account)
    with state.lock:
        data = _numbers(paths, account)
        entry = _folder_numbers(data, fid)
        number = entry["ids"].get(gid)
        if number is None:
            number = int(entry.get("next") or 1)
            entry["next"] = number + 1
            entry["ids"][gid] = number
            state.reverse.setdefault(fid, {})[number] = gid
            state.dirty = True
        return int(number)


def numbers_for(paths, account, fid, gids_oldest_first):
    return [number_for(paths, account, fid, gid) for gid in gids_oldest_first]


def graph_id(paths, account, fid, number):
    """The Graph id behind a number in a folder."""
    state = _state(account)
    try:
        key = int(str(number))
    except (TypeError, ValueError):
        raise GraphFailure("notfound", "unusable message id: %s" % number)
    with state.lock:
        _numbers(paths, account)
        gid = state.reverse.get(fid, {}).get(key)
    if gid is None:
        raise GraphFailure("notfound", "message %s is no longer in that folder" % number)
    return gid


def drop_number(paths, account, fid, gid):
    state = _state(account)
    with state.lock:
        data = _numbers(paths, account)
        entry = _folder_numbers(data, fid)
        number = entry["ids"].pop(gid, None)
        if number is not None:
            state.reverse.get(fid, {}).pop(int(number), None)
            state.dirty = True


def keep_only(paths, account, fid, gids):
    """After a full listing: forgets numbers of messages no longer in
    the folder. The folder's next number is kept, so none is reused."""
    keep = set(gids)
    state = _state(account)
    with state.lock:
        data = _numbers(paths, account)
        entry = _folder_numbers(data, fid)
        for gid in [gid for gid in entry["ids"] if gid not in keep]:
            number = entry["ids"].pop(gid)
            state.reverse.get(fid, {}).pop(int(number), None)
            state.dirty = True


# --- Envelopes ---------------------------------------------------------

def _local_iso(value):
    if not value:
        return ""
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    try:
        return moment.astimezone().isoformat()
    except (ValueError, OSError, OverflowError):
        return moment.isoformat()


def _address(entry):
    mailbox = (entry or {}).get("emailAddress") or {}
    address = str(mailbox.get("address") or "").strip()
    if not address:
        return None
    return {"name": str(mailbox.get("name") or ""), "email": address}


def _addresses(entries):
    result = []
    for entry in entries or []:
        one = _address(entry)
        if one is not None:
            result.append(one)
    return result


def _properties(message):
    """Extended properties by (type, tag number); Graph may write the
    tag as 0x0E08 or 0xe08."""
    found = {}
    for prop in message.get("singleValueExtendedProperties") or []:
        words = str(prop.get("id") or "").strip().lower().split()
        if len(words) != 2 or not words[1].startswith("0x"):
            continue
        try:
            found[(words[0], int(words[1], 16))] = prop.get("value")
        except ValueError:
            continue
    return found


def envelope_from(message, number):
    """One Graph message as the envelope dict imap_body_fetch builds."""
    props = _properties(message)
    flags = []
    if message.get("isRead"):
        flags.append({"raw": "\\Seen", "iana": "seen"})
    if ((message.get("flag") or {}).get("flagStatus") or "") == "flagged":
        flags.append({"raw": "\\Flagged", "iana": "flagged"})
    if message.get("isDraft"):
        flags.append({"raw": "\\Draft", "iana": "draft"})
    verb = str(props.get(_PROP_LAST_VERB) or "")
    if verb in ("102", "103"):
        flags.append({"raw": "\\Answered", "iana": "answered"})
    elif verb == "104":
        flags.append({"raw": "$Forwarded", "iana": None})
    sender = _address(message.get("from"))
    received = _local_iso(message.get("receivedDateTime"))
    result = {
        "id": str(number),
        "subject": str(message.get("subject") or ""),
        "date": _local_iso(message.get("sentDateTime")) or received,
        "flags": flags,
        "from": [sender] if sender else [],
        "to": _addresses(message.get("toRecipients")),
        "cc": _addresses(message.get("ccRecipients")),
    }
    if received:
        result["received"] = received
    try:
        size = int(props.get(_PROP_SIZE))
    except (TypeError, ValueError):
        size = None
    if size is not None:
        result["size"] = size
    if "hasAttachments" in message:
        result["has-attachment"] = bool(message.get("hasAttachments"))
    importance = str(message.get("importance") or "").lower()
    if importance in ("high", "normal", "low"):
        result["priority"] = {"high": 2, "normal": 3, "low": 4}[importance]
    message_id = str(message.get("internetMessageId") or "").strip()
    if message_id:
        result["message-id"] = message_id
    in_reply_to = str(props.get(_PROP_IN_REPLY_TO) or "").strip()
    result["in-reply-to"] = [in_reply_to] if in_reply_to else []
    return result


def _messages(paths, account, path_base, limit, extra=""):
    """Messages of a listing, with the extras when Graph takes them."""
    query = "$select=%s%s" % (_SELECT, extra)
    if _EXPAND_STATE["ok"]:
        try:
            return _get_all(paths, account, "%s%s&$expand=%s" % (path_base, query, _EXPAND), limit)
        except GraphFailure as exc:
            if exc.kind != "refused":
                raise
            _EXPAND_STATE["ok"] = False
            logger.info("Graph refused the listing extras; listing without them: %s", exc.text)
    return _get_all(paths, account, path_base + query, limit)


def _envelopes(paths, account, fid, messages):
    """Envelope dicts for messages listed newest first; numbers are
    given oldest first, as new UIDs would be."""
    numbered = {}
    for message in reversed(messages):
        gid = message.get("id")
        if gid and gid not in numbered:
            numbered[gid] = number_for(paths, account, fid, gid)
    _save_numbers(paths, account)
    return [envelope_from(message, numbered[message["id"]]) for message in messages if message.get("id")]


def list_envelopes(paths, account, folder="INBOX", page_size=200, page=1):
    """A folder's messages newest first, page by page as Himalaya pages."""
    fid = folder_id(paths, account, folder)
    size = max(1, int(page_size))
    skip = max(0, (int(page) - 1) * size)
    base = "/me/mailFolders/%s/messages?$top=%d&$skip=%d&$orderby=receivedDateTime desc&" % (
        _quote(fid), min(size, PAGE_MAX), skip,
    )
    return _envelopes(paths, account, fid, _messages(paths, account, base, size))


def folder_numbers(paths, account, folder):
    """Every number in a folder, ascending; forgets the numbers of
    messages no longer there."""
    fid = folder_id(paths, account, folder)
    path = "/me/mailFolders/%s/messages?$top=%d&$select=id&$orderby=receivedDateTime asc" % (
        _quote(fid), PAGE_MAX,
    )
    gids = [m["id"] for m in _get_all(paths, account, path, 10 ** 7) if m.get("id")]
    numbers = numbers_for(paths, account, fid, gids)
    keep_only(paths, account, fid, gids)
    _save_numbers(paths, account)
    return sorted(numbers)


def find_numbers(paths, account, folder, message_id_header):
    """Numbers in a folder of the messages with this Message-ID."""
    header = str(message_id_header or "").strip()
    if not header:
        return []
    if not header.startswith("<"):
        header = "<%s>" % header.strip("<>")
    fid = folder_id(paths, account, folder)
    path = "/me/mailFolders/%s/messages?$top=50&$select=id&$filter=%s" % (
        _quote(fid), urllib.parse.quote("internetMessageId eq '%s'" % header.replace("'", "''")),
    )
    gids = [m["id"] for m in _get_all(paths, account, path, 50) if m.get("id")]
    numbers = numbers_for(paths, account, fid, gids)
    _save_numbers(paths, account)
    return sorted(numbers)


def _kql_text(term):
    return " ".join(str(term or "").replace('"', " ").replace("\\", " ").split())


def search_envelopes(paths, account, term, folder, page_size=200, fields=("subject", "from", "body"),
                     unread=False, flagged=False, after=None, not_after=None):
    """The Search tab's search: Graph's search for words, over the
    chosen fields, with the received days; Unread only and Flagged only
    are then checked here, since Graph's search takes no filter. With no
    words, a filter does it all, newest first. Dates are days wider
    than asked, as the Search tab expects; it checks exact times."""
    fid = folder_id(paths, account, folder)
    size = max(1, int(page_size))
    words = _kql_text(term)
    field_names = {"subject": "subject", "from": "from", "to": "to", "body": "body"}
    chosen = [field_names[f] for f in (fields or ()) if f in field_names]
    if words and chosen:
        parts = ['%s:"%s"' % (name, words) for name in chosen]
        query = "(" + " OR ".join(parts) + ")"
        if after is not None:
            query += " AND received>=%s" % after.isoformat()
        if not_after is not None:
            query += " AND received<%s" % (not_after + datetime.timedelta(days=2)).isoformat()
        base = "/me/mailFolders/%s/messages?$top=%d&$search=%s&" % (
            _quote(fid), min(size, 250), urllib.parse.quote('"%s"' % query.replace('"', '\\"')),
        )
        messages = _messages(paths, account, base, size)
        if unread:
            messages = [m for m in messages if not m.get("isRead")]
        if flagged:
            messages = [m for m in messages if ((m.get("flag") or {}).get("flagStatus") == "flagged")]
        return _envelopes(paths, account, fid, messages)
    if not (unread or flagged or after is not None or not_after is not None):
        return []
    start = after.isoformat() if after is not None else "1900-01-01"
    clauses = ["receivedDateTime ge %sT00:00:00Z" % start]
    if not_after is not None:
        clauses.append("receivedDateTime lt %sT00:00:00Z"
                       % (not_after + datetime.timedelta(days=2)).isoformat())
    if unread:
        clauses.append("isRead eq false")
    if flagged:
        clauses.append("flag/flagStatus eq 'flagged'")
    base = "/me/mailFolders/%s/messages?$top=%d&$orderby=receivedDateTime desc&$filter=%s&" % (
        _quote(fid), min(size, PAGE_MAX), urllib.parse.quote(" and ".join(clauses)),
    )
    return _envelopes(paths, account, fid, _messages(paths, account, base, size))


# --- Message source ----------------------------------------------------

def message_bytes(paths, account, number, folder):
    """The message's full MIME source, as Microsoft holds it."""
    fid = folder_id(paths, account, folder)
    gid = graph_id(paths, account, fid, number)
    return request(paths, account, "GET", "/me/messages/%s/$value" % _quote(gid), raw=True,
                   timeout=60) or b""


def message_dict(paths, account, number, folder):
    import imap_body_fetch

    raw = message_bytes(paths, account, number, folder)
    try:
        return imap_body_fetch.build_message_dict(raw)
    except Exception as exc:  # noqa: BLE001 - reported like a failed read
        raise GraphFailure("refused", "could not parse message: %s" % exc)


# --- Changes -----------------------------------------------------------

def set_flag(paths, account, number, folder, flag, on):
    name = str(flag).strip().lstrip("\\").lower()
    if name == "seen":
        body = {"isRead": bool(on)}
    elif name == "flagged":
        body = {"flag": {"flagStatus": "flagged" if on else "notFlagged"}}
    else:
        raise GraphFailure("refused", "flag %s is not available on this account" % flag)
    fid = folder_id(paths, account, folder)
    gid = graph_id(paths, account, fid, number)
    request(paths, account, "PATCH", "/me/messages/%s" % _quote(gid), body)


def mark_folder_read(paths, account, folder):
    fid = folder_id(paths, account, folder)
    path = "/me/mailFolders/%s/messages?$top=%d&$select=id&$filter=%s" % (
        _quote(fid), PAGE_MAX, urllib.parse.quote("isRead eq false"),
    )
    gids = [m["id"] for m in _get_all(paths, account, path, 10 ** 6) if m.get("id")]
    results = batch(paths, account, [
        ("PATCH", "/me/messages/%s" % _quote(gid), {"isRead": True}) for gid in gids
    ])
    return sum(1 for status, _body in results if 200 <= status < 300)


def _gids(paths, account, fid, numbers):
    pairs = []
    for number in numbers:
        try:
            pairs.append((number, graph_id(paths, account, fid, number)))
        except GraphFailure:
            logger.debug("No Graph id for number %s; skipped.", number)
    return pairs


def transfer(paths, account, numbers, folder, to_folder, copy=False):
    """Moves (or copies) messages by number. Returns (done, failed)
    number lists. The moved ones get new numbers in to_folder."""
    fid = folder_id(paths, account, folder)
    to_fid = folder_id(paths, account, to_folder)
    pairs = _gids(paths, account, fid, numbers)
    action = "copy" if copy else "move"
    results = batch(paths, account, [
        ("POST", "/me/messages/%s/%s" % (_quote(gid), action), {"destinationId": to_fid})
        for _number, gid in pairs
    ])
    done, failed = [], []
    known = {str(number) for number, _gid in pairs}
    failed.extend(number for number in numbers if str(number) not in known)
    for (number, gid), (status, body) in zip(pairs, results):
        if 200 <= status < 300:
            new_gid = (body or {}).get("id") if isinstance(body, dict) else None
            if not copy:
                drop_number(paths, account, fid, gid)
            number_for(paths, account, to_fid, new_gid or gid)
            done.append(number)
        else:
            failed.append(number)
    _save_numbers(paths, account)
    return done, failed


def delete_numbers(paths, account, folder, numbers):
    """Removes messages from the mailbox for good, as ZBox sees it
    (Microsoft keeps them a while among its recoverable items, as it
    does after an IMAP expunge). Returns (done, failed)."""
    fid = folder_id(paths, account, folder)
    pairs = _gids(paths, account, fid, numbers)
    results = batch(paths, account, [("DELETE", "/me/messages/%s" % _quote(gid), None) for _n, gid in pairs])
    done, failed = [], []
    known = {str(number) for number, _gid in pairs}
    failed.extend(number for number in numbers if str(number) not in known)
    for (number, gid), (status, _body) in zip(pairs, results):
        if 200 <= status < 300 or status == 404:
            drop_number(paths, account, fid, gid)
            done.append(number)
        else:
            failed.append(number)
    _save_numbers(paths, account)
    return done, failed


def has_attachments(paths, account, folder, numbers):
    fid = folder_id(paths, account, folder)
    pairs = _gids(paths, account, fid, numbers)
    results = batch(paths, account, [
        ("GET", "/me/messages/%s?$select=hasAttachments" % _quote(gid), None) for _n, gid in pairs
    ])
    found = []
    for (number, _gid), (status, body) in zip(pairs, results):
        if status == 200 and isinstance(body, dict) and body.get("hasAttachments"):
            try:
                found.append(int(str(number)))
            except ValueError:
                continue
    return found


# --- Sending and saving --------------------------------------------------

def _as_bytes(raw_message):
    if isinstance(raw_message, bytes):
        return raw_message
    return str(raw_message or "").encode("utf-8", "surrogateescape")


def _pgp_too_large_text():
    import lang

    return lang.t(
        "errors", "graph_pgp_too_large",
        default=(
            "This encrypted or signed message is larger than Microsoft allows for "
            "such mail, about 3 MB. Send smaller attachments, or turn off Encrypt "
            "and Digitally Sign for this message."
        ),
    )


def split_attachments(message):
    """Takes the attachments out of a parsed message. Returns the list
    of (name, content type, bytes, inline, content id), oldest part
    first; the message keeps only its text and structure. A file part
    is any part with a file name or an attachment disposition, and any
    part that is not text."""
    found = []

    def strip(part):
        if not part.is_multipart():
            return
        kept = []
        for child in part.get_payload():
            if child.is_multipart():
                strip(child)
                if child.get_payload():
                    kept.append(child)
                continue
            disposition = (child.get_content_disposition() or "").lower()
            is_file = (disposition == "attachment" or bool(child.get_filename())
                       or child.get_content_maintype() not in ("text",))
            if is_file:
                content_id = str(child.get("Content-ID") or "").strip().strip("<>")
                found.append((
                    child.get_filename() or "attachment",
                    child.get_content_type(),
                    child.get_payload(decode=True) or b"",
                    disposition == "inline" or (bool(content_id) and disposition != "attachment"),
                    content_id,
                ))
            else:
                kept.append(child)
        part.set_payload(kept)

    strip(message)
    return found


def _attach(paths, account, gid, attachment):
    name, content_type, data, inline, content_id = attachment
    if len(data) <= SMALL_ATTACHMENT_LIMIT:
        body = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name, "contentType": content_type,
            "contentBytes": base64.b64encode(data).decode("ascii"),
            "isInline": bool(inline),
        }
        if content_id:
            body["contentId"] = content_id
        request(paths, account, "POST", "/me/messages/%s/attachments" % _quote(gid), body, timeout=60)
        return
    item = {"attachmentType": "file", "name": name, "size": len(data),
            "contentType": content_type, "isInline": bool(inline)}
    if content_id:
        item["contentId"] = content_id
    upload = request(
        paths, account, "POST",
        "/me/messages/%s/attachments/createUploadSession" % _quote(gid),
        {"AttachmentItem": item},
    ) or {}
    upload_url = upload.get("uploadUrl")
    if not upload_url:
        raise GraphFailure("refused", "Microsoft did not open an upload for %s." % name)
    total = len(data)
    start = 0
    while start < total:
        end = min(start + UPLOAD_CHUNK, total)
        # The upload address carries its own authorization; a token
        # must not be sent with it.
        req = urllib.request.Request(
            upload_url, data=data[start:end], method="PUT",
            headers={"Content-Range": "bytes %d-%d/%d" % (start, end - 1, total),
                     "Content-Type": "application/octet-stream"},
        )
        gate = _Gate(account)
        try:
            status, _headers, payload = _open(req, UPLOAD_TIMEOUT_SECONDS)
        finally:
            gate.leave()
        if status >= 400:
            kind = "network" if status >= 500 or status == 429 else "refused"
            raise GraphFailure(kind, _error_text(status, payload))
        start = end


def _create_mime(paths, account, fid, raw_bytes):
    answer = request(
        paths, account, "POST", "/me/mailFolders/%s/messages" % _quote(fid),
        base64.b64encode(raw_bytes), content_type="text/plain", timeout=60,
    ) or {}
    gid = answer.get("id")
    if not gid:
        raise GraphFailure("refused", "Microsoft did not return the new message.")
    return gid


def _create_large(paths, account, fid, raw_bytes):
    """A message over MIME_WHOLE_LIMIT: created without its attachments,
    which are then added one by one. Returns the Graph id."""
    message = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    if message.get_content_type() in ("multipart/signed", "multipart/encrypted"):
        raise GraphFailure("toolarge", _pgp_too_large_text())
    attachments = split_attachments(message)
    base = message.as_bytes()
    gid = _create_mime(paths, account, fid, base)
    try:
        for attachment in attachments:
            _attach(paths, account, gid, attachment)
    except GraphFailure:
        try:
            request(paths, account, "DELETE", "/me/messages/%s" % _quote(gid))
        except GraphFailure:
            logger.debug("Could not remove a half-built large message.", exc_info=True)
        raise
    return gid


def send_message(paths, account, raw_message):
    """Sends a raw message. Microsoft saves the Sent copy itself."""
    raw_bytes = _as_bytes(raw_message)
    if len(raw_bytes) <= MIME_WHOLE_LIMIT:
        request(paths, account, "POST", "/me/sendMail", base64.b64encode(raw_bytes),
                content_type="text/plain", timeout=60)
        return
    drafts = folder_id(paths, account, "Drafts")
    gid = _create_large(paths, account, drafts, raw_bytes)
    try:
        request(paths, account, "POST", "/me/messages/%s/send" % _quote(gid), timeout=60)
    except GraphFailure:
        try:
            request(paths, account, "DELETE", "/me/messages/%s" % _quote(gid))
        except GraphFailure:
            logger.debug("Could not remove an unsent large message.", exc_info=True)
        raise


def add_message(paths, account, folder, raw_message):
    """Saves a raw message in a folder (drafts); Graph keeps it as a
    draft. Returns its number, as text."""
    raw_bytes = _as_bytes(raw_message)
    fid = folder_id(paths, account, folder)
    if len(raw_bytes) <= MIME_WHOLE_LIMIT:
        gid = _create_mime(paths, account, fid, raw_bytes)
    else:
        gid = _create_large(paths, account, fid, raw_bytes)
    number = number_for(paths, account, fid, gid)
    _save_numbers(paths, account)
    return str(number)


def _safe_file_name(name):
    text = os.path.basename(str(name or "").replace("\\", "/")) or "attachment"
    text = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in text).strip(" .") or "attachment"
    return text


def download_attachments(paths, account, number, dest_dir, folder):
    """Writes each attachment of a message into dest_dir under its own
    name, as Himalaya's attachment download does."""
    import imap_body_fetch

    raw = message_bytes(paths, account, number, folder)
    message = email.message_from_bytes(raw, policy=email.policy.default)
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    for part in message.walk():
        if part.is_multipart() or not imap_body_fetch._is_attachment(part):
            continue
        name = _safe_file_name(part.get_filename())
        with open(os.path.join(dest_dir, name), "wb") as handle:
            handle.write(part.get_payload(decode=True) or b"")
        written.append(name)
    return written


def test_connection(paths, account):
    """The account wizard's test: one Inbox message listed through
    Graph, without giving it a number (the account may never be added)."""
    answer = request(paths, account, "GET", "/me/mailFolders/inbox/messages?$top=1&$select=id") or {}
    return [{"id": str(index + 1)} for index, _m in enumerate(answer.get("value") or [])]


def close_all():
    """Saves every number map still waiting to be written."""
    with _STATES_LOCK:
        states = list(_STATES.values())
    for state in states:
        if state.dirty and state.paths is not None:
            _save_numbers(state.paths, state.account)


# --- Routes ------------------------------------------------------------
#
# Called from the routing blocks at the end of himalaya_client and
# imap_body_fetch. Each takes the same arguments as the function it
# stands in for and raises what that function's callers expect.

def _hc():
    import himalaya_client

    return himalaya_client


def _note(paths, account):
    state = _state(account)
    state.paths = paths
    state.account = account


def _routed_himalaya(func):
    def run(paths, account, *args, **kwargs):
        _note(paths, account)
        take_last_failure()
        try:
            return func(paths, account, *args, **kwargs)
        except GraphFailure as exc:
            raise as_himalaya_error(paths, account, exc) from None
    run.__name__ = func.__name__
    run.__doc__ = func.__doc__
    return run


def _routed_pooled(func):
    def run(paths, account, *args, **kwargs):
        _note(paths, account)
        take_last_failure()
        try:
            return func(paths, account, *args, **kwargs)
        except GraphFailure as exc:
            raise as_pooled_error(paths, account, exc) from None
    run.__name__ = func.__name__
    run.__doc__ = func.__doc__
    return run


def _invalidate(paths, account, folder, numbers):
    hc = _hc()
    for number in numbers:
        hc._invalidate_cached_message(paths, account, folder, number)


@_routed_himalaya
def hc_list_folders(paths, account):
    return folder_entries(paths, account)


@_routed_himalaya
def hc_list_all_folders(paths, account, counts=False):
    return folder_entries(paths, account)


@_routed_himalaya
def hc_create_mailbox(paths, account, name):
    create_folder(paths, account, name)


@_routed_himalaya
def hc_delete_mailbox(paths, account, name):
    delete_folder(paths, account, name)


@_routed_himalaya
def hc_rename_mailbox(paths, account, name, new_name):
    rename_folder(paths, account, name, new_name)


@_routed_himalaya
def hc_subscribe_mailbox(paths, account, name):
    return None


@_routed_himalaya
def hc_unsubscribe_mailbox(paths, account, name):
    return None


@_routed_himalaya
def hc_search_envelopes(paths, account, term, folder, page_size=200, backend="imap",
                        fields=("subject", "from", "body"), unread=False, flagged=False,
                        after=None, not_after=None):
    return search_envelopes(paths, account, term, folder, page_size=page_size, fields=fields,
                            unread=unread, flagged=flagged, after=after, not_after=not_after)


@_routed_himalaya
def hc_move_message(paths, account, message_id, from_folder, to_folder):
    _invalidate(paths, account, from_folder, [message_id])
    done, _failed = transfer(paths, account, [message_id], from_folder, to_folder)
    if not done:
        raise GraphFailure("refused", "The message could not be moved to %s." % to_folder)


@_routed_himalaya
def hc_copy_message(paths, account, message_id, from_folder, to_folder):
    done, _failed = transfer(paths, account, [message_id], from_folder, to_folder, copy=True)
    if not done:
        raise GraphFailure("refused", "The message could not be copied to %s." % to_folder)


@_routed_himalaya
def hc_move_messages(paths, account, message_ids, from_folder, to_folder):
    ids = [message_id for message_id in message_ids if message_id is not None]
    if not ids:
        return
    _invalidate(paths, account, from_folder, ids)
    done, failed = transfer(paths, account, ids, from_folder, to_folder)
    if failed and not done:
        raise GraphFailure("refused", "No message could be moved to %s." % to_folder)


@_routed_himalaya
def hc_delete_message(paths, account, message_id, folder="INBOX"):
    _invalidate(paths, account, folder, [message_id])
    if folder_id(paths, account, folder) == folder_id(paths, account, "Trash"):
        done, _failed = delete_numbers(paths, account, folder, [message_id])
    else:
        done, _failed = transfer(paths, account, [message_id], folder, "Trash")
    if not done:
        raise GraphFailure("refused", "The message could not be deleted.")


@_routed_himalaya
def hc_purge_folder(paths, account, folder, trash_folder, envelopes):
    ids = [envelope.get("id") for envelope in envelopes if envelope.get("id") is not None]
    if not ids:
        return []
    _invalidate(paths, account, folder, ids)
    done, failed = delete_numbers(paths, account, folder, ids)
    if failed and not done:
        raise GraphFailure("refused", "Could not remove any of the %d message(s) in %s." % (len(failed), folder))
    return [(message_id, "purge_failed") for message_id in failed]


@_routed_himalaya
def hc_purge_moved_from_trash(paths, account, moved, trash_folder, pre_max):
    hc = _hc()
    unresolved = []
    wanted = {}
    for item in moved:
        header = hc._canonical_message_id(item[2])
        if not header:
            unresolved.append((item[0], item[1], "no_header"))
            continue
        wanted.setdefault(header, []).append(item)
    if not wanted:
        return unresolved
    needed = sum(len(group) for group in wanted.values())
    envelopes = list_envelopes(paths, account, folder=trash_folder, page_size=min(needed + 50, 2000))
    candidates = {}
    for envelope in envelopes:
        try:
            number = int(envelope.get("id"))
        except (TypeError, ValueError):
            continue
        header = hc._envelope_message_id(envelope)
        if number > int(pre_max or 0) and header in wanted:
            candidates.setdefault(header, set()).add(number)
    matched = []
    for header, group in wanted.items():
        numbers = sorted(candidates.get(header, ()))
        for index, item in enumerate(group):
            if index < len(numbers):
                matched.append((numbers[index], item))
            else:
                unresolved.append((item[0], item[1], "not_found"))
    if matched:
        done, failed = delete_numbers(paths, account, trash_folder, [number for number, _item in matched])
        failed_set = {str(number) for number in failed}
        for number, item in matched:
            hc._invalidate_cached_message(paths, account, trash_folder, number)
            if str(number) in failed_set:
                unresolved.append((item[0], item[1], "purge_failed"))
        hc.forget_offline_copies(paths, account, trash_folder,
                                 [item[2] for number, item in matched if str(number) not in failed_set])
    return unresolved


@_routed_himalaya
def hc_permanently_delete_messages(paths, account, items, trash_folder):
    hc = _hc()
    items = [item for item in items if item[0] is not None]
    items = hc.resolve_live_items(paths, account, items)
    unresolved = []
    groups = {}
    for item in items:
        groups.setdefault(item[1], []).append(item)
    for folder, group in groups.items():
        numbers = [item[0] for item in group]
        _invalidate(paths, account, folder, numbers)
        try:
            _done, failed = delete_numbers(paths, account, folder, numbers)
        except GraphFailure as exc:
            if exc.kind in ("signin", "busy", "closing"):
                raise
            logger.warning("Permanent delete in %s failed on %s: %s", folder, account.account_id, exc.text)
            failed = numbers
        failed_set = {str(number) for number in failed}
        unresolved.extend((item[0], item[1], "delete_failed") for item in group if str(item[0]) in failed_set)
        hc.forget_offline_copies(paths, account, folder,
                                 [item[2] for item in group if str(item[0]) not in failed_set])
    return unresolved


@_routed_himalaya
def hc_download_attachments(paths, account, message_id, dest_dir, folder="INBOX"):
    download_attachments(paths, account, message_id, dest_dir, folder)


@_routed_himalaya
def hc_send_message_raw(paths, account, raw_message_text, save_copy=True, section=None):
    send_message(paths, account, raw_message_text)


@_routed_himalaya
def hc_ensure_sent_copy(paths, account, raw_message_text, folder=None):
    return None


@_routed_himalaya
def hc_add_message_raw(paths, account, folder, raw_message_text, flags=("draft",)):
    return add_message(paths, account, folder, raw_message_text)


@_routed_pooled
def ib_fetch_message(paths, account, message_id, folder="INBOX", background=False):
    return message_dict(paths, account, message_id, folder)


@_routed_pooled
def ib_fetch_message_raw(paths, account, message_id, folder="INBOX", background=False):
    return message_bytes(paths, account, message_id, folder).decode("utf-8", errors="replace")


@_routed_pooled
def ib_fetch_message_bytes(paths, account, message_id, folder="INBOX", background=False):
    return bytes(message_bytes(paths, account, message_id, folder))


@_routed_pooled
def ib_list_envelopes(paths, account, folder="INBOX", page_size=200, page=1, background=False):
    return list_envelopes(paths, account, folder=folder, page_size=page_size, page=page)


@_routed_pooled
def ib_set_flag(paths, account, message_id, folder, flag, on):
    set_flag(paths, account, message_id, folder, flag, on)


@_routed_pooled
def ib_folder_uids(paths, account, folder):
    return folder_numbers(paths, account, folder)


@_routed_pooled
def ib_mark_folder_read(paths, account, folder):
    return mark_folder_read(paths, account, folder)


@_routed_pooled
def ib_move_uids(paths, account, message_ids, folder, to_folder):
    done, failed = transfer(paths, account, list(message_ids), folder, to_folder)
    if failed and not done:
        raise GraphFailure("refused", "No message could be moved to %s." % to_folder)


@_routed_pooled
def ib_find_uids(paths, account, folder, message_id_header):
    return find_numbers(paths, account, folder, message_id_header)


@_routed_pooled
def ib_delete_uids(paths, account, folder, message_ids):
    done, failed = delete_numbers(paths, account, folder, list(message_ids))
    if failed and not done:
        raise GraphFailure("refused", "No message could be removed from %s." % folder)
    return "expunged"


@_routed_pooled
def ib_sent_copy_exists(paths, account, folder, message_id_header):
    return bool(find_numbers(paths, account, folder, message_id_header))


@_routed_pooled
def ib_attachment_uids(paths, account, folder, message_ids):
    return has_attachments(paths, account, folder, list(message_ids))


# Names routed in each module, and the function here that serves them.
HIMALAYA_ROUTES = {
    "list_folders": hc_list_folders,
    "list_all_folders": hc_list_all_folders,
    "create_mailbox": hc_create_mailbox,
    "delete_mailbox": hc_delete_mailbox,
    "rename_mailbox": hc_rename_mailbox,
    "subscribe_mailbox": hc_subscribe_mailbox,
    "unsubscribe_mailbox": hc_unsubscribe_mailbox,
    "search_envelopes": hc_search_envelopes,
    "move_message": hc_move_message,
    "copy_message": hc_copy_message,
    "move_messages": hc_move_messages,
    "delete_message": hc_delete_message,
    "purge_folder": hc_purge_folder,
    "purge_moved_from_trash": hc_purge_moved_from_trash,
    "permanently_delete_messages": hc_permanently_delete_messages,
    "download_attachments": hc_download_attachments,
    "send_message_raw": hc_send_message_raw,
    "_ensure_sent_copy": hc_ensure_sent_copy,
    "add_message_raw": hc_add_message_raw,
}

POOLED_ROUTES = {
    "fetch_message": ib_fetch_message,
    "fetch_message_raw": ib_fetch_message_raw,
    "fetch_message_bytes": ib_fetch_message_bytes,
    "list_envelopes": ib_list_envelopes,
    "set_flag": ib_set_flag,
    "folder_uids": ib_folder_uids,
    "mark_folder_read": ib_mark_folder_read,
    "move_uids": ib_move_uids,
    "find_uids": ib_find_uids,
    "delete_uids": ib_delete_uids,
    "sent_copy_exists": ib_sent_copy_exists,
    "attachment_uids": ib_attachment_uids,
}


def route_module(namespace, routes, skip=None):
    """Wraps each named function of a module so a Graph account goes to
    its route here and every other account to the function as it was.
    skip(args, kwargs) may keep a call on the original (send_message_raw
    with an identity's own outgoing server)."""
    import functools

    for name, route in routes.items():
        original = namespace.get(name)
        if original is None:
            raise RuntimeError("graph_client: %s has no %s to route" % (namespace.get("__name__"), name))

        def make(original=original, route=route, name=name):
            @functools.wraps(original)
            def routed(paths, account, *args, **kwargs):
                if uses_graph(account) and not (skip is not None and skip(name, args, kwargs)):
                    return route(paths, account, *args, **kwargs)
                return original(paths, account, *args, **kwargs)
            routed.__graph_route__ = route
            return routed

        namespace[name] = make()
