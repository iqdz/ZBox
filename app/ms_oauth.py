"""
Microsoft sign-in (OAuth 2.0) for Outlook.com, Hotmail, Live, MSN and
Microsoft 365 accounts. Standard library only, and no wx, so main.py's
--get-token flag can use it without starting the app.

Two ways to sign in, both ending in the same token record:

1. Browser: the default browser opens Microsoft's sign-in page with
   PKCE, and a catcher on the loopback address receives the answer on
   a port chosen at that moment. It listens on 127.0.0.1 and on the
   IPv6 loopback at the same port, because a browser may resolve
   localhost to either. The state value is checked, and anything else
   that reaches the port is answered and ignored.
2. Code: Microsoft hands out a short code, the person enters it at
   Microsoft's address on any device, and ZBox polls until the
   sign-in is finished.

The token record lives in the encrypted secret store under
"<account_id>.oauth", next to the account passwords, so it travels
with the ZBox folder exactly as they do: through the master password
on another computer. Nothing goes to the registry, the Windows
credential manager or a browser profile. Tokens are never logged.

Himalaya asks for a token on every connection through
"ZBox.exe --get-token <account id>" (see get_access_token). A token
with under five minutes left is refreshed first, and the new refresh
token Microsoft returns replaces the old one. One lock file in the
config folder keeps two processes from refreshing, or writing the
store, at the same time.

The client ID is not part of the source. It is read from
ms_client.dat, a local file that is never committed and that the
build carries into apps_files. It is stored scrambled so a plain text
search of the folder or of ZBox.exe does not find it. That is only
obfuscation: a desktop app cannot hide its client ID, which appears
in the browser's address bar on every sign-in. Without the file this
copy simply has no Microsoft sign-in.
"""

import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets as _random
import socket
import socketserver
import http.server
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import dpapi_secret_store

logger = logging.getLogger("zbox.ms_oauth")

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0/"
AUTHORIZE_URL = AUTHORITY + "authorize"
TOKEN_URL = AUTHORITY + "token"
DEVICE_CODE_URL = AUTHORITY + "devicecode"

# One resource only: a Microsoft token serves a single resource, and
# the IMAP and SMTP servers accept only tokens for outlook.office.com.
# openid and profile add the signed-in address and name to the answer.
SCOPES = (
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
    "offline_access",
    "openid",
    "profile",
)
SCOPE = " ".join(SCOPES)

SECRET_SUFFIX = ".oauth"
# Put in front of every "sign in again" error, so himalaya_client can
# tell it apart from any other failure in Himalaya's stderr.
SIGNIN_MARKER = "ZBOX-SIGN-IN-NEEDED"

REFRESH_MARGIN_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 30
BROWSER_TIMEOUT_SECONDS = 300

CLIENT_FILE_NAME = "ms_client.dat"
_SCRAMBLE_KEY = b"ZBox portable mail, Microsoft sign-in"
_GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
)

_LOCK_NAME = "oauth.lock"
_LOCK_WAIT_SECONDS = 20
_LOCK_STALE_SECONDS = 60


class OAuthError(Exception):
    """A sign-in or token step that did not work.

    kind is one of: not_configured, network, server, cancelled,
    timeout, denied, expired, busy, sign_in_needed. detail is
    Microsoft's own first line, or ZBox's, for showing to the person.
    """

    def __init__(self, kind, detail=""):
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


class SignInNeeded(OAuthError):
    """The stored sign-in can no longer be used: none is stored, it
    belongs to a different client ID, or Microsoft refused the refresh
    token (a password change, a withdrawn consent, long disuse)."""

    def __init__(self, detail=""):
        super().__init__("sign_in_needed", detail)

    def __str__(self):
        text = "%s Microsoft sign-in for this account is needed again." % SIGNIN_MARKER
        if self.detail:
            text += " " + self.detail
        return text


# --- The client ID ----------------------------------------------------

def scramble_client_id(text):
    """The form ms_client.dat holds. Obfuscation only, see the module
    note."""
    data = (text or "").strip().encode("ascii")
    key = _SCRAMBLE_KEY
    mixed = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))
    return base64.b64encode(mixed[::-1]).decode("ascii")


def unscramble_client_id(blob):
    """The client ID from ms_client.dat's text, or "" if it is not a
    valid one."""
    try:
        mixed = base64.b64decode((blob or "").strip().encode("ascii"), validate=True)[::-1]
        key = _SCRAMBLE_KEY
        text = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(mixed)).decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    return text if _GUID.match(text) else ""


def _client_file_candidates():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        return [
            os.path.join(base, "apps_files", CLIENT_FILE_NAME),
            os.path.join(base, CLIENT_FILE_NAME),
        ]
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(base, CLIENT_FILE_NAME)]


_client_id_cache = None


def client_id():
    """This copy's Microsoft client ID, or "" when it has none."""
    global _client_id_cache
    if _client_id_cache is None:
        found = ""
        for path in _client_file_candidates():
            try:
                with open(path, "r", encoding="ascii") as handle:
                    found = unscramble_client_id(handle.read())
            except (OSError, UnicodeError):
                continue
            if found:
                break
        _client_id_cache = found
    return _client_id_cache


def is_configured():
    return bool(client_id())


def _require_client():
    value = client_id()
    if not value:
        raise OAuthError(
            "not_configured", "Microsoft sign-in is not set up in this copy of ZBox.",
        )
    return value


# --- Talking to Microsoft ---------------------------------------------

def _describe(data):
    text = str(data.get("error_description") or data.get("error") or "").strip()
    first = text.splitlines()[0] if text else ""
    return first[:300]


def _post(url, fields):
    """One form POST, answered as a dict. Microsoft's error answers
    come with a 4xx status and a JSON body, so those are read too."""
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read() or b""
        except OSError:
            raw = b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError("network", str(getattr(exc, "reason", exc)))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise OAuthError("server", "Microsoft sent an answer ZBox could not read.")
    if not isinstance(data, dict):
        raise OAuthError("server", "Microsoft sent an answer ZBox could not read.")
    return data


def _raise_for(data):
    error = str(data.get("error") or "")
    detail = _describe(data)
    if error in ("invalid_grant", "interaction_required", "consent_required", "login_required"):
        raise SignInNeeded(detail)
    if error in ("authorization_declined", "access_denied"):
        raise OAuthError("denied", detail)
    if error in ("expired_token", "code_expired"):
        raise OAuthError("expired", detail)
    raise OAuthError("server", detail or "Microsoft refused the request.")


def _id_claims(id_token):
    """The claims inside an ID token, read without checking its
    signature. That is allowed here: the token came straight from
    Microsoft's token endpoint over TLS, and it is only used to show
    and fill in the signed-in address."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:  # noqa: BLE001 - no claims is a fine answer
        return {}
    return claims if isinstance(claims, dict) else {}


def _record_from(data, previous=None):
    access = data.get("access_token")
    if not access:
        raise OAuthError("server", _describe(data) or "Microsoft sent no access token.")
    previous = previous or {}
    claims = _id_claims(str(data.get("id_token") or ""))
    try:
        lifetime = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        lifetime = 3600
    return {
        "client_id": client_id(),
        "access_token": access,
        "expires_at": int(time.time()) + lifetime,
        "refresh_token": data.get("refresh_token") or previous.get("refresh_token", ""),
        "username": claims.get("preferred_username") or claims.get("email") or previous.get("username", ""),
        "name": claims.get("name") or previous.get("name", ""),
    }


def _pkce_pair():
    verifier = base64.urlsafe_b64encode(_random.token_bytes(48)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# --- Browser sign-in --------------------------------------------------

class _Catcher:
    """Receives the browser's return visit with the code."""

    def __init__(self, state, page_done, page_failed):
        self.state = state
        self.page_done = page_done
        self.page_failed = page_failed
        self.result = None
        self.done = threading.Event()

    def accept(self, query):
        """True when this visit carried a usable code."""
        if self.done.is_set():
            return self.result is not None and self.result[0] == "code"
        state = (query.get("state") or [""])[0]
        if state != self.state:
            # Not the answer to this sign-in: answer and keep waiting.
            return False
        if "error" in query:
            error = (query.get("error") or [""])[0]
            detail = (query.get("error_description") or [""])[0] or error
            self.result = ("error", error, detail.splitlines()[0][:300] if detail else "")
        else:
            self.result = ("code", (query.get("code") or [""])[0], "")
        self.done.set()
        return self.result[0] == "code"

    def handler_class(self):
        catcher = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 10

            def do_GET(self):  # noqa: N802 - the name http.server calls
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if "code" not in query and "error" not in query:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ok = catcher.accept(query)
                text = catcher.page_done if ok else catcher.page_failed
                body = (
                    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                    "<title>ZBox</title></head><body><h1>ZBox</h1><p>%s</p></body></html>"
                    % html.escape(text)
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # quiet: the query holds the code
                pass

        return Handler


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # TCPServer, not HTTPServer: HTTPServer looks up the host's full
    # name on bind, which can take seconds on Windows for nothing.
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        logger.debug("The sign-in catcher dropped one request.", exc_info=True)


class _Server6(_Server):
    address_family = socket.AF_INET6


def _ipv6_loopback_available():
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind(("::1", 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _open_servers(handler):
    """(port, servers): IPv4 and IPv6 loopback at one port, or IPv4
    alone where this computer has no IPv6 loopback."""
    for _attempt in range(10):
        first = _Server(("127.0.0.1", 0), handler)
        port = first.server_address[1]
        try:
            second = _Server6(("::1", port), handler)
        except OSError:
            if not _ipv6_loopback_available():
                return port, [first]
            first.server_close()
            continue
        return port, [first, second]
    raise OAuthError("server", "No free local port for the sign-in answer.")


def sign_in_with_browser(open_url, cancel_event=None, page_done="Signed in.",
                         page_failed="Sign-in did not finish.", login_hint="",
                         timeout=BROWSER_TIMEOUT_SECONDS):
    """Runs a browser sign-in and returns the token record.

    open_url(url) is called once with Microsoft's sign-in address; the
    caller opens it in the default browser. Blocks until the browser
    comes back, cancel_event is set, or timeout passes, so it belongs
    on a background thread. page_done and page_failed are the sentences
    the browser shows afterwards, in the person's language.
    """
    cid = _require_client()
    verifier, challenge = _pkce_pair()
    state = _random.token_urlsafe(24)
    catcher = _Catcher(state, page_done, page_failed)
    port, servers = _open_servers(catcher.handler_class())
    redirect = "http://localhost:%d" % port
    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    stop = threading.Event()

    def serve(server):
        server.timeout = 0.5
        while not stop.is_set():
            server.handle_request()

    threads = [threading.Thread(target=serve, args=(server,), daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        open_url(url)
        deadline = time.monotonic() + timeout
        while not catcher.done.wait(0.5):
            if cancel_event is not None and cancel_event.is_set():
                raise OAuthError("cancelled")
            if time.monotonic() > deadline:
                raise OAuthError("timeout")
    finally:
        stop.set()
        for thread in threads:
            thread.join(2)
        for server in servers:
            try:
                server.server_close()
            except OSError:
                pass

    kind, value, detail = catcher.result
    if kind != "code":
        if value == "access_denied":
            raise OAuthError("denied", detail)
        raise OAuthError("server", detail or value)
    data = _post(TOKEN_URL, {
        "client_id": cid,
        "grant_type": "authorization_code",
        "code": value,
        "redirect_uri": redirect,
        "code_verifier": verifier,
        "scope": SCOPE,
    })
    if data.get("error"):
        _raise_for(data)
    return _record_from(data)


# --- Code sign-in -----------------------------------------------------

def start_device_code():
    """Asks Microsoft for a sign-in code. Returns a dict with user_code
    and verification_uri to show, plus what complete_device_code needs."""
    cid = _require_client()
    data = _post(DEVICE_CODE_URL, {"client_id": cid, "scope": SCOPE})
    if data.get("error") or not data.get("device_code"):
        _raise_for(data)
    try:
        interval = int(data.get("interval") or 5)
        lifetime = int(data.get("expires_in") or 900)
    except (TypeError, ValueError):
        interval, lifetime = 5, 900
    return {
        "device_code": data["device_code"],
        "user_code": str(data.get("user_code") or ""),
        "verification_uri": str(data.get("verification_uri") or data.get("verification_url") or ""),
        "interval": max(1, interval),
        "expires_at": time.monotonic() + lifetime,
    }


def complete_device_code(info, cancel_event=None):
    """Waits for the code sign-in to finish and returns the token
    record. Blocks, so it belongs on a background thread."""
    cid = _require_client()
    interval = max(1, int(info.get("interval", 5)))
    network_failures = 0
    while True:
        if cancel_event is not None:
            if cancel_event.wait(interval):
                raise OAuthError("cancelled")
        else:
            time.sleep(interval)
        if time.monotonic() > info.get("expires_at", 0):
            raise OAuthError("expired")
        try:
            data = _post(TOKEN_URL, {
                "client_id": cid,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": info["device_code"],
            })
        except OAuthError as exc:
            if exc.kind != "network":
                raise
            network_failures += 1
            if network_failures >= 3:
                raise
            continue
        network_failures = 0
        error = data.get("error")
        if not error:
            return _record_from(data)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        _raise_for(data)


# --- Storage ----------------------------------------------------------

def secret_key(account_id):
    return "%s%s" % (account_id, SECRET_SUFFIX)


class _StoreLock:
    """One lock for every token write. Himalaya can start several
    --get-token processes at once, for one account or several, and the
    secret store is a single file: two writers at once could lose one
    of the new refresh tokens."""

    def __init__(self, config_dir):
        self.path = os.path.join(config_dir, _LOCK_NAME)
        self.held = False

    def __enter__(self):
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(handle, str(os.getpid()).encode("ascii"))
                os.close(handle)
                self.held = True
                return self
            except (FileExistsError, PermissionError):
                pass
            try:
                if time.time() - os.path.getmtime(self.path) > _LOCK_STALE_SECONDS:
                    os.remove(self.path)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise OAuthError("busy", "Another ZBox process kept the sign-in store busy.")
            time.sleep(0.2)

    def __exit__(self, *_exc):
        if self.held:
            self.held = False
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def load_record(config_dir, account_id):
    """The stored token record, or None. Raises
    dpapi_secret_store.SecretStoreUnavailable if the store itself
    cannot be read."""
    raw = dpapi_secret_store.get_password(config_dir, secret_key(account_id))
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _write_record(config_dir, account_id, record):
    dpapi_secret_store.set_password(
        config_dir, secret_key(account_id), json.dumps(record, separators=(",", ":")),
    )


def save_record(config_dir, account_id, record):
    with _StoreLock(config_dir):
        _write_record(config_dir, account_id, record)


def delete_record(config_dir, account_id):
    """Removes an account's tokens, if it has any. Never raises."""
    try:
        with _StoreLock(config_dir):
            if dpapi_secret_store.get_password(config_dir, secret_key(account_id)) is not None:
                dpapi_secret_store.delete_password(config_dir, secret_key(account_id))
    except Exception:  # noqa: BLE001 - cleanup must not stop anything
        logger.info("Could not remove the Microsoft sign-in of %s.", account_id, exc_info=True)


def signed_in_username(config_dir, account_id):
    """The address an account is signed in as, or "". Never raises."""
    try:
        record = load_record(config_dir, account_id)
    except Exception:  # noqa: BLE001
        return ""
    return str((record or {}).get("username") or "")


def needs_sign_in(config_dir, account_id):
    """True when this account has to be signed in again: no usable
    record, a record from another client ID, or one Microsoft already
    refused. Never raises; an unreadable store answers False, since
    that is the store's own problem and says so elsewhere."""
    try:
        record = load_record(config_dir, account_id)
    except Exception:  # noqa: BLE001
        return False
    if not record or not record.get("refresh_token"):
        return True
    if record.get("client_id") != client_id():
        return True
    return bool(record.get("needs_sign_in"))


def _usable_token(record, margin):
    if not record:
        raise SignInNeeded("No Microsoft sign-in is stored for this account.")
    if record.get("client_id") != client_id():
        raise SignInNeeded("The stored sign-in belongs to a different app registration.")
    if record.get("needs_sign_in"):
        # Microsoft already refused this record's refresh token: fail
        # at once rather than ask again on every connection.
        raise SignInNeeded("Microsoft refused the stored sign-in.")
    try:
        left = float(record.get("expires_at") or 0) - time.time()
    except (TypeError, ValueError):
        left = 0
    if record.get("access_token") and left > margin:
        return record["access_token"]
    return ""


def refresh(record):
    """A new token record from a stored one's refresh token."""
    cid = _require_client()
    token = (record or {}).get("refresh_token")
    if not token:
        raise SignInNeeded("No refresh token is stored.")
    data = _post(TOKEN_URL, {
        "client_id": cid,
        "grant_type": "refresh_token",
        "refresh_token": token,
        "scope": SCOPE,
    })
    if data.get("error"):
        _raise_for(data)
    return _record_from(data, previous=record)


def get_access_token(config_dir, account_id, margin=REFRESH_MARGIN_SECONDS):
    """A current access token for one account, refreshed first when
    under margin seconds remain. Raises SignInNeeded when the person
    has to sign in again, OAuthError for anything else."""
    _require_client()
    token = _usable_token(load_record(config_dir, account_id), margin)
    if token:
        return token
    with _StoreLock(config_dir):
        # Another process may have refreshed while this one waited.
        record = load_record(config_dir, account_id)
        token = _usable_token(record, margin)
        if token:
            return token
        try:
            fresh = refresh(record)
        except SignInNeeded:
            # Marked, so every later request fails without contacting
            # Microsoft until the person signs in again, which writes
            # a whole new record.
            if record:
                marked = dict(record)
                marked["needs_sign_in"] = True
                marked["access_token"] = ""
                _write_record(config_dir, account_id, marked)
            raise
        _write_record(config_dir, account_id, fresh)
        logger.info("Refreshed the Microsoft sign-in of %s.", account_id)
        return fresh["access_token"]
