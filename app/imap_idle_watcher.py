"""
Background IMAP IDLE watchers: one per account, each holding its own
persistent IMAP connection open and blocking in IDLE (RFC 2177) so
new mail is noticed the moment the server pushes it, instead of
waiting for the next 20-second poll to even discover it exists.

Himalaya's own CLI has no IDLE support at all -- confirmed directly
against the pinned himalaya/himalaya.exe (v2.1.0): no `idle` subcommand
anywhere in its command tree (checked every subcommand group's
--help output), and not achievable by hand either -- `imap raw`'s
own description says it "reads until every command's tagged
completion has arrived", but IDLE never sends one until the client
sends DONE, so a raw-command invocation would just hang forever with
no way to send that DONE from a separate process invocation later.
By design every Himalaya invocation is a fresh, short-lived process
that exits once its one command completes; IDLE needs the opposite --
one connection held open indefinitely. So this bypasses Himalaya
entirely for this one job and speaks IMAP directly via imapclient
(already a declared project dependency in requirements.txt and
checked for by build.bat, just not used by any code yet -- this is
the "future IMAP IDLE watcher" himalaya_worker.py's own docstring was
already written in anticipation of). Every other mail operation still
goes through Himalaya as always; this module's only responsibility is
noticing that something changed and saying so -- it never parses
envelopes or message bodies itself, and never touches wx state
directly (see ImapIdleWatcher's on_activity contract below).

One watcher instance per account, each on its own daemon thread, so
one account's connection trouble (a bad password, a provider that
turns out not to support IDLE, a network blip) never affects any
other account -- the per-service isolation asked for. IDLE itself is
a plain, provider-agnostic IMAP feature: Gmail and a generic
Dovecot-style server like disroot's both speak the exact same wire
protocol here, so there's one real engine (ImapIdleWatcher) rather
than one per provider -- duplicating IDLE's handshake/timeout/
reconnect logic per provider would just be the same code copied
several times for no protocol reason. What legitimately varies by
provider -- whether IDLE is supported at all, which folder to watch,
and (longer term) whether a provider should use IDLE at all versus
some completely different push mechanism, e.g. Gmail's own Pub/Sub
push instead of IMAP IDLE -- is a handful of fields on a small
WatcherProfile, decided per account by build_watcher_profile() below.
Adding a provider-specific wrinkle later means adding a case there,
not touching the engine.
"""

import logging
import ssl
import threading

import wx

try:
    import imapclient
except ImportError:  # pragma: no cover - imapclient is a declared
    # dependency (requirements.txt / build.bat) but a broken or
    # missing install here should mean "no instant sync this run",
    # never "ZBox won't start". Every watcher just no-ops in that
    # case and the app falls back to its existing 20-second poll.
    imapclient = None

import dpapi_secret_store
from account_lanes import is_network_error
from envelope_format import _folder_display_to_himalaya

logger = logging.getLogger("zbox.idle")

# Servers commonly tear an IDLE connection down around the 29-minute
# mark, but home routers and NAT often drop a quiet connection far
# sooner, and a dropped one looks exactly like "no new mail" until
# the next renew. Renewing every 5 minutes caps how long a silently
# dead connection can leave an account deaf.
IDLE_REFRESH_SECONDS = 5 * 60

# Backoff for reconnect attempts after a failure (bad network, a
# server hiccup): starts short, doubles up to a ceiling, resets after
# any session that ran cleanly. Never retrying instantly avoids
# turning a flaky connection into the same kind of rapid-reconnect
# churn already identified as a likely cause of server-side
# throttling elsewhere in this app.
_RECONNECT_INITIAL_SECONDS = 5
_RECONNECT_MAX_SECONDS = 60


class ImapIdleUnsupported(Exception):
    """Raised when the server doesn't advertise the IDLE capability
    at all -- not a transient failure, so the watcher stops instead
    of retrying forever against a server that will never support it."""


class WatcherProfile:
    """
    The small set of per-account facts an IDLE watcher needs beyond
    the account's own login fields: whether to run at all, and which
    folder to watch. Everything else about the engine is identical
    across providers -- see the module docstring.
    """

    def __init__(self, supported=True, folder="INBOX"):
        self.supported = supported
        self.folder = folder


def build_watcher_profile(account):
    """
    Decides, per account, whether an IDLE watcher should run for it
    and what it should watch -- the one seam this module is meant to
    grow through. Today every account ZBox talks to is plain IMAP and
    only Inbox needs watching (matching the 20-second poll's own
    current scope), so this is trivial; an account known not to
    support IDLE, one that should watch more than Inbox, or eventually
    a provider with its own non-IMAP push mechanism entirely all get
    decided here without ImapIdleWatcher needing to know why.
    """
    if not account.imap_host:
        return WatcherProfile(supported=False)
    return WatcherProfile(
        supported=True, folder=_folder_display_to_himalaya("Inbox", account)
    )


class ImapIdleWatcher:
    """
    Holds one account's IDLE connection open on its own daemon
    thread. on_activity(account, folder) is called whenever the
    server reports the watched folder changed (new mail, a deletion,
    a flag change from another client, etc.) -- always via
    wx.CallAfter, so it lands on the main thread exactly like every
    other worker callback in this app, never on this watcher's own
    thread. ZBox decides what to actually do about that; this class's
    only job is noticing and saying so (himalaya_worker.py's own
    docstring anticipated exactly this hand-off: "the watcher thread
    only posts a lightweight signal... the actual fetch is queued
    here like any other job").

    Never raises out of its thread: any connection or protocol
    failure is caught, logged, and followed by a backoff-and-retry --
    the same "never let a background failure take the app down"
    posture as HimalayaWorker._run.
    """

    def __init__(self, account, profile, paths, on_activity, reachability=None):
        self._account = account
        self._profile = profile
        self._paths = paths
        self._on_activity = on_activity
        # account_lanes.Reachability, shared with the frame: while the
        # account is in its backoff pause this watcher does not try to
        # connect, and a connect that fails because the server could
        # not be reached counts toward that pause.
        self._reachability = reachability
        self._stop_event = threading.Event()
        self._thread = None
        self._catch_up = False

    def start(self):
        if imapclient is None or not self._profile.supported:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"idle-{self._account.account_id}", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        backoff = _RECONNECT_INITIAL_SECONDS
        # Set after a failure, so the next session that connects asks
        # for one refresh: mail that arrived while it was down would
        # otherwise wait for a push that already happened.
        self._catch_up = False
        while not self._stop_event.is_set():
            account_id = self._account.account_id
            if self._reachability is not None and not self._reachability.allowed(account_id):
                # Backing off: the server could not be reached. Checked
                # again every few seconds; the pause itself belongs to
                # Reachability. Mail missed meanwhile is caught up on
                # the next session.
                self._catch_up = True
                if self._stop_event.wait(_RECONNECT_INITIAL_SECONDS):
                    return
                continue
            try:
                self._watch_once()
                backoff = _RECONNECT_INITIAL_SECONDS  # a clean session resets the backoff
            except ImapIdleUnsupported as exc:
                logger.info(
                    "IDLE not supported for %s (%s); this account will rely on the "
                    "regular poll instead of instant sync.",
                    account_id, exc,
                )
                return
            except Exception as exc:
                self._catch_up = True
                if self._reachability is not None and is_network_error(exc):
                    pause = self._reachability.failed(account_id)
                    logger.warning(
                        "IDLE watcher for %s could not reach its server (%s); "
                        "automatic checks for it pause for %ss.",
                        account_id, exc, pause,
                    )
                    continue
                logger.exception(
                    "IDLE watcher for %s lost its connection; retrying in %ss.",
                    account_id, backoff,
                )
                if self._stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)

    def _watch_once(self):
        """
        One connect-login-select-idle session. Returns normally only
        if stop() was called mid-idle; raises on any failure so
        _run's loop can back off and reconnect (or, for
        ImapIdleUnsupported, give up on this account for good).
        """
        # A Microsoft account logs in with an access token (XOAUTH2).
        # A failure raises, and _run's loop backs off as for any other.
        microsoft = getattr(self._account, "auth_method", "password") == "microsoft"
        if microsoft:
            import ms_oauth  # only needed for Microsoft accounts

            password = ms_oauth.get_access_token(self._paths.config, self._account.account_id)
        else:
            password = dpapi_secret_store.get_password(self._paths.config, self._account.account_id)
        if not password:
            raise RuntimeError("No stored password for this account; cannot start IDLE.")

        use_ssl = self._account.imap_encryption == "tls"
        ssl_context = ssl.create_default_context() if self._account.imap_encryption != "none" else None

        with imapclient.IMAPClient(
            self._account.imap_host, port=self._account.imap_port,
            ssl=use_ssl, ssl_context=ssl_context, timeout=30,
        ) as client:
            if self._account.imap_encryption == "start-tls":
                client.starttls(ssl_context)
            if microsoft:
                client.oauth2_login(self._account.login_email, password)
            else:
                client.login(self._account.login_email, password)
            password = None  # done with the plaintext the moment login succeeds

            if not client.has_capability("IDLE"):
                raise ImapIdleUnsupported(f"server for {self._account.imap_host} doesn't advertise IDLE")

            client.select_folder(self._profile.folder, readonly=True)

            if self._catch_up:
                self._catch_up = False
                wx.CallAfter(self._on_activity, self._account, self._profile.folder)

            while not self._stop_event.is_set():
                client.idle()
                try:
                    responses = client.idle_check(timeout=IDLE_REFRESH_SECONDS)
                finally:
                    client.idle_done()
                if responses:
                    wx.CallAfter(self._on_activity, self._account, self._profile.folder)
                # No response inside the timeout just means the
                # refresh interval elapsed with nothing happening;
                # loop straight back into a fresh idle() either way,
                # which is also what re-arms the 20-minute ceiling.


class IdleWatcherManager:
    """
    Owns the set of running per-account watchers and keeps it in sync
    with ZBox's account list -- the one thing main_frame needs to
    know about this module. on_activity is one callback shared by
    every account's watcher; main_frame's version dispatches by the
    (account, folder) it's given.
    """

    def __init__(self, paths, on_activity, reachability=None):
        self._paths = paths
        self._on_activity = on_activity
        self._reachability = reachability
        self._watchers = {}  # account_id -> ImapIdleWatcher

    def sync(self, accounts):
        """
        Starts a watcher for any account that doesn't have one yet
        and stops any watcher whose account is no longer configured.
        Safe to call repeatedly (e.g. every time the account list
        changes) -- accounts already running are left alone, so
        editing one account's display name doesn't bounce every
        other account's IDLE connection.
        """
        # A disabled account is not watched. Filtering here also
        # drops it out of current_ids below, so disabling one stops
        # its watcher on the next sync() without any extra call.
        accounts = [
            account for account in accounts
            if getattr(account, "enabled", True)
            and getattr(account, "auto_check", True)
        ]
        current_ids = {account.account_id for account in accounts}
        for account_id in list(self._watchers):
            if account_id not in current_ids:
                self._watchers.pop(account_id).stop()
        for account in accounts:
            if account.account_id in self._watchers:
                continue
            profile = build_watcher_profile(account)
            watcher = ImapIdleWatcher(
                account, profile, self._paths, self._on_activity,
                reachability=self._reachability,
            )
            watcher.start()
            self._watchers[account.account_id] = watcher

    def stop_one(self, account_id):
        """
        Stops and forgets a single account's watcher, if running, so
        the next sync() call sees that account as unwatched and
        starts a fresh one for it -- used when an account's login
        details change, since sync() alone only starts accounts it
        doesn't already have a watcher for and would otherwise leave
        a just-edited account's watcher running against its old
        password or host indefinitely.
        """
        watcher = self._watchers.pop(account_id, None)
        if watcher is not None:
            watcher.stop()

    def stop_all(self):
        for watcher in self._watchers.values():
            watcher.stop()
        self._watchers.clear()
