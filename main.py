"""
ZBox mail app entry point.
All paths are resolved relative to this file's own location, so the
whole ZBox folder stays portable and can run from any drive.
"""

import os
import time
import re
import sys
import logging

# Base directory is wherever this file actually lives, not the
# current working directory and not a fixed install path.
if getattr(sys, "frozen", False):
    # Running as a built executable.
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The portable layout, identical from source and frozen, so a path
# that works in development cannot break only once built:
#
#     <root>/ZBox.exe            (main.py from source)
#     <root>/apps_files/         PyInstaller runtime, bundled WebView2
#     <root>/himalaya/           himalaya.exe
#     <root>/data/               config, userdata, cache, logs, sounds
#
# Everything the user owns is under data/, so backing up or moving an
# installation is one folder, and nothing ZBox writes ever lands
# outside the app folder.
#
# userdata/ and cache/ are separate on purpose. userdata/ holds what
# no re-download brings back (contacts, blocklists, per-account
# thread state); cache/ holds only the offline Maildir copies and
# cached message bodies. Back up userdata/, never cache/, and cache/
# can be deleted at any time.
APP_DIR = os.path.join(BASE_DIR, "app")
APPS_FILES_DIR = os.path.join(BASE_DIR, "apps_files")
HIMALAYA_DIR = os.path.join(BASE_DIR, "himalaya")
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
USERDATA_DIR = os.path.join(DATA_DIR, "userdata")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
SOUNDS_DIR = os.path.join(DATA_DIR, "sounds")

# Makes app modules importable before anything else runs. app/ only
# exists when running from source; in a frozen build PyInstaller has
# already put the same modules on sys.path out of apps_files.
sys.path.insert(0, APP_DIR)
sys.path.insert(0, APPS_FILES_DIR)

# Only user-data folders are created. app/, apps_files/, himalaya/
# and data/sounds/ ship with the app: creating them on demand would
# leave an empty folder next to the executable whenever part of the
# install is missing, hiding the real error behind it.
for folder in (DATA_DIR, CONFIG_DIR, USERDATA_DIR, CACHE_DIR, LOGS_DIR):
    os.makedirs(folder, exist_ok=True)


def run_get_token(argv):
    """
    Handles "ZBox.exe --get-token <account id>": prints a current
    Microsoft access token for one account and exits, without
    importing wx or starting the app.

    This is what himalaya.toml's xoauth2 token.command points at for
    an account that uses Microsoft sign-in, the token counterpart of
    --get-secret below and kept as light for the same reason. A token
    close to expiry is refreshed first (app/ms_oauth.py). When the
    person has to sign in again, stderr starts with ms_oauth's
    sign-in-needed marker so himalaya_client can recognise it.
    """
    if len(argv) < 2 or not argv[1].strip():
        _write_stderr("Usage: --get-token <account id>\n")
        return 2

    account_id = argv[1].strip()
    try:
        import ms_oauth

        token = ms_oauth.get_access_token(CONFIG_DIR, account_id)
    except Exception as exc:
        _write_stderr("%s\n" % exc)
        return 1

    # No trailing newline: Himalaya reads stdout as the raw token.
    # Through fd 1, as for the password: a windowed build has no
    # sys.stdout.
    _write_stdout(token)
    return 0


def run_get_secret(argv):
    """
    Handles "ZBox.exe --get-secret <account id>": prints the stored
    password and exits, without importing wx or starting the app.

    This is what himalaya.toml's password.command points at. It used
    to point at [sys.executable, app\\get_secret.py, <id>], which is
    correct from source and broken once frozen: PyInstaller sets
    sys.executable to ZBox.exe, so Himalaya ran
    "ZBox.exe app\\get_secret.py <id>", launched a second copy of the
    GUI, and got no password back. Every account failed to
    authenticate in the built app while working perfectly from
    source.

    Must stay above every heavy import. Himalaya runs this once per
    connection and waits on it, so the cost of starting wx would be
    paid on every single fetch.
    """
    if len(argv) < 2 or not argv[1].strip():
        _write_stderr("Usage: --get-secret <account id>\n")
        return 2

    account_id = argv[1].strip()
    try:
        from get_secret import lookup_password

        password = lookup_password(CONFIG_DIR, account_id)
    except Exception as exc:
        _write_stderr("%s\n" % exc)
        return 1

    if not password:
        _write_stderr(
            "No stored password for account %s. If this ZBox folder was "
            "moved to another computer, start ZBox once and enter the "
            "master password, or sign the account in again.\n" % account_id
        )
        return 1

    _write_stdout(password)
    return 0


def _write_stdout(text):
    """Writes to file descriptor 1 rather than sys.stdout.

    A PyInstaller --windowed build has no console, and Python's
    sys.stdout is None there -- but the pipe Himalaya hands the
    process is still a perfectly good fd 1. Writing the descriptor
    directly is what makes the secret actually reach Himalaya in the
    built app. sys.stdout is the fallback for the run-from-source
    case where fd 1 might be something odd.
    """
    data = text.encode("utf-8")
    try:
        os.write(1, data)
        return
    except Exception:
        pass
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def _write_stderr(text):
    try:
        os.write(2, text.encode("utf-8"))
        return
    except Exception:
        pass
    try:
        sys.stderr.write(text)
    except Exception:
        pass


# Per-record character cap for the debug log. Tracebacks are not
# part of a record's message and are never touched by this, so an
# error stays complete; what gets cut is a 40 KB raw message body
# dumped as a single line.
LOG_MESSAGE_LIMIT = 800

# Ceiling for the log file. Reached only by something looping, which
# is exactly when the newest lines are the ones worth keeping.
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 1

# How many launches' logs to keep. Each launch writes its own
# timestamped file, so without a ceiling the folder grows forever.
LOG_KEEP = 5

# Third-party loggers that write protocol-level traffic at DEBUG.
# imapclient logs every IMAP line it sends and receives, envelopes
# and message bodies included. Held at WARNING always: there is no
# longer a setting to raise them, because a log a tester sends must
# never carry their mail.
PROTOCOL_LOGGERS = ("imapclient", "imaplib", "urllib3", "comtypes", "asyncio")


class _TruncateLongMessages(logging.Filter):
    """Caps one record's message, and says how much it cut.

    Deliberately not a lower log level: the fact that a message was
    read, and the first several hundred characters of the response,
    are what diagnoses a problem. The remaining 40 KB of somebody's
    mail never did, and it is what made the file unreadable.
    """

    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def filter(self, record):
        if self.limit <= 0:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if len(message) <= self.limit:
            return True
        record.msg = "%s... [%d more characters suppressed]" % (
            message[:self.limit], len(message) - self.limit,
        )
        record.args = ()
        return True


def _make_log_handler(log_path, header_lines):
    """A size-capped handler over one launch's own log file.

    The caller passes a name that is unique to this launch, so
    nothing has to be truncated to give one launch one log. The
    rollover stays as a backstop for a session that runs away.
    The header is rewritten at the top of each new file so the source
    fingerprint is still the first thing in whatever file is current;
    without that, a rollover would carry the one line that says which
    build the log describes off into the backup.
    """
    import logging.handlers

    class _RotatingLog(logging.handlers.RotatingFileHandler):
        def doRollover(self):
            super().doRollover()
            try:
                for line in header_lines():
                    self.stream.write(line + "\n")
                self.stream.flush()
            except Exception:  # noqa: BLE001
                pass

    return _RotatingLog(
        log_path, mode="a", maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT, encoding="utf-8", delay=False,
    )


def _prune_old_logs(logs_dir, keep):
    """Keeps the newest few launches and deletes the rest.

    Every launch writes its own timestamped file, so something has
    to take the old ones away. A rotation backup is grouped with the
    launch it belongs to, and a file that will not delete (open in an
    editor, held by a viewer) is skipped rather than allowed to stop
    startup.
    """
    try:
        names = [name for name in os.listdir(logs_dir)
                 if name.startswith("zbox_debug_") and ".log" in name]
    except OSError:
        return
    # The stem carries a zero-padded date and time, so sorting the
    # names is sorting by launch time.
    stems = sorted({name.split(".log")[0] for name in names}, reverse=True)
    for stem in stems[max(keep, 0):]:
        for name in names:
            if name.split(".log")[0] != stem:
                continue
            try:
                os.remove(os.path.join(logs_dir, name))
            except OSError:
                pass


def setup_logging(debug_logging):
    if not debug_logging:
        logging.basicConfig(handlers=[logging.NullHandler()], level=logging.CRITICAL)
        return
    # One file per launch, named for when the launch happened, so a
    # log is no longer destroyed by the next start. The prune runs
    # one short of LOG_KEEP because this launch's own file is about
    # to become the newest of them.
    _prune_old_logs(LOGS_DIR, LOG_KEEP - 1)
    log_path = os.path.join(
        LOGS_DIR, "zbox_debug_%s.log" % time.strftime("%Y-%m-%d_%H%M%S")
    )

    fingerprint = _source_fingerprint()

    def header_lines():
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        return [
            "%s [INFO] zbox.log: log continued after rotation" % stamp,
            "%s [INFO] zbox.log: ZBox base dir: %s" % (stamp, BASE_DIR),
            "%s [INFO] zbox.log: ZBox source fingerprint: %s"
            % (stamp, fingerprint),
        ]

    handler = _make_log_handler(log_path, header_lines)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    handler.addFilter(_TruncateLongMessages(LOG_MESSAGE_LIMIT))

    logging.basicConfig(handlers=[handler], level=logging.DEBUG)

    # The IMAP library logs every line it sends and receives, message
    # bodies included. A tester's log must never carry their mail, so
    # these stay at WARNING and there is no setting to raise them:
    # removed in session w along with Tools > Capture Next Message
    # Read, for the same reason.
    for name in PROTOCOL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.info("ZBox starting. Base dir: %s", BASE_DIR)
    logging.info("ZBox source fingerprint: %s", fingerprint)


def _source_fingerprint():
    """
    A short hash over every app/*.py, plus the newest modification
    time among them, logged once at startup.

    ZBox is developed in one folder and run from a copy of it, so
    "did that change actually reach the build I just tested?" is a
    real and recurring question. It has already cost two rounds of
    debugging a fix that was never running: the log showed the old
    behaviour and looked like the fix had failed, when the file
    simply had not been copied across. One line at the top of every
    log answers it -- if the fingerprint matches the source folder's,
    the log is describing the code you think it is.
    """
    import hashlib

    if getattr(sys, "frozen", False):
        # A frozen build has no app/ folder at all: PyInstaller puts
        # the modules inside the executable. Fingerprint the
        # executable itself, which answers the question a copied
        # build actually raises -- which build is this log from.
        exe_path = sys.executable
        try:
            exe_digest = hashlib.sha256()
            with open(exe_path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    exe_digest.update(chunk)
            built = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(os.path.getmtime(exe_path)),
            )
        except OSError as exc:
            return "unavailable (%s)" % exc
        return "%s (%s, built: %s)" % (
            exe_digest.hexdigest()[:12], os.path.basename(exe_path), built,
        )

    app_dir = os.path.join(BASE_DIR, "app")
    digest = hashlib.sha256()
    newest = 0.0
    try:
        for name in sorted(os.listdir(app_dir)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(app_dir, name)
            try:
                with open(path, "rb") as handle:
                    digest.update(name.encode("utf-8"))
                    digest.update(handle.read())
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                # A file that cannot be read still has to change the
                # fingerprint, or a half-copied build would look
                # identical to a complete one.
                digest.update(b"<unreadable>" + name.encode("utf-8"))
    except OSError as exc:
        return "unavailable (%s)" % exc
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest)) if newest else "unknown"
    return "%s (newest app file: %s)" % (digest.hexdigest()[:12], stamp)


def install_exception_logging():
    """
    Routes uncaught exceptions from inside wx's MainLoop into the
    debug log instead of a bare traceback on stderr.

    wx's own default handling of an exception raised inside a Python
    event handler (an EVT_MENU/EVT_BUTTON/etc callback, including
    accelerator-table handlers) during MainLoop() calls
    sys.excepthook and then keeps the app running -- this only
    replaces that hook's destination, not wx's decision to survive
    the exception. Without a custom hook, the default one just
    prints to stderr, which is invisible in a --windowed PyInstaller
    build (no console attached) and was the reason a real bug
    (see message_view_panel.py's accelerator-table handlers) could
    look to the user like a keystroke silently "doing nothing"
    instead of an obvious, diagnosable crash -- exactly the same
    class of silent failure already found once in main_frame.py's
    search-tab handling (commit b5ba31d) and by the same missing
    piece: no sys.excepthook anywhere in this app before now. Must be
    installed after setup_logging() so a handler actually exists to
    write to.
    """
    logger = logging.getLogger("zbox.main")

    def _log_unhandled(exc_type, exc_value, exc_tb):
        logger.error(
            "Unhandled exception from inside wx.MainLoop (a screen "
            "or keystroke action likely appeared to do nothing):",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _log_unhandled


def _runtime_version_key(name):
    """Sorts expanded runtime folder names by their real version
    numbers, so 140.0.3485.44 ranks above 99.0.1150.55 instead of
    losing a plain string comparison to it."""
    parts = re.split(r"(\d+)", name)
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.lower())
            for part in parts]


def resolve_webview2_fixed_runtime(apps_files_dir, logger=None):
    r"""
    Finds a WebView2 Fixed Version runtime bundled under
    apps_files\webview2, if one is there.

    Two layouts are accepted, because Microsoft's Fixed Version
    package expands into a versioned folder of its own:

        apps_files\webview2\msedgewebview2.exe
        apps_files\webview2\<version folder>\msedgewebview2.exe

    When several version folders are present the highest version
    wins, so dropping in a newer expansion and deleting the old one
    is the whole update procedure. Returns the folder that actually
    holds msedgewebview2.exe, or None if there is no bundled runtime.
    """
    root = os.path.join(apps_files_dir, "webview2")
    if not os.path.isdir(root):
        return None

    if os.path.isfile(os.path.join(root, "msedgewebview2.exe")):
        return root

    try:
        entries = os.listdir(root)
    except OSError:
        if logger is not None:
            logger.warning("Could not read %s while looking for a "
                           "bundled WebView2 runtime.", root)
        return None

    candidates = [
        name for name in entries
        if os.path.isfile(os.path.join(root, name, "msedgewebview2.exe"))
    ]
    if not candidates:
        if logger is not None:
            logger.warning(
                "%s exists but contains no msedgewebview2.exe. Run "
                "tools\\get_webview2.bat, or delete the folder to use "
                "the system-wide runtime.", root)
        return None

    candidates.sort(key=_runtime_version_key)
    return os.path.join(root, candidates[-1])


def ensure_webview2_runtime(apps_files_dir, mode="auto", logger=None):
    r"""
    Points WebView2 at a runtime ZBox controls, rather than whatever
    Edge build Windows Update happens to have installed.

    Why this matters for ZBox specifically: the HTML message view's
    screen-reader behaviour depends on how one particular Chromium
    build exposes its accessibility tree to UIA, and on that build
    honouring --force-renderer-accessibility. The Evergreen runtime
    is serviced system-wide, on Microsoft's schedule, with no notice
    -- so a machine that read HTML mail correctly yesterday can stop
    doing so after a background update, with nothing in ZBox having
    changed. A Fixed Version runtime bundled in apps_files\webview2 pins
    the exact build the behaviour was verified against.

    wxPython's wx.html2.WebView gives no way to pass
    browserExecutableFolder into CreateCoreWebView2EnvironmentWithOptions,
    so this uses WEBVIEW2_BROWSER_EXECUTABLE_FOLDER, the environment
    variable the WebView2 loader reads for exactly that purpose --
    the same mechanism Microsoft documents for WinUI apps, and the
    same one-variable pattern ZBox already uses for
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS. Like that one it must be
    set before the first wx.html2.WebView is created, because that is
    when the WebView2 environment is created and the browser process
    launched.

    Falls back to the system-wide runtime whenever a bundled one is
    not usable: ZBox starting with a slightly different renderer is
    always better than ZBox not starting.
    """
    var = "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"

    # Version reporting only -- never gates anything. A build that
    # cannot be identified still renders mail; it just makes a future
    # regression report harder to read.
    try:
        from webview2_runtime import describe
    except Exception:
        describe = lambda folder=None: (
            "Bundled fixed version" if folder else "System-wide runtime"
        )

    mode = str(mode or "auto").lower()
    if mode not in ("auto", "fixed", "evergreen"):
        mode = "auto"

    existing = os.environ.get(var)
    if existing:
        # Something outside ZBox (a launcher script, an admin policy)
        # already pinned a runtime. Leave it alone.
        if logger is not None:
            logger.info(
                "WebView2 runtime already pinned externally: %s (%s)",
                existing, describe(existing),
            )
        return existing

    if mode == "evergreen":
        if logger is not None:
            logger.info(
                "WebView2 runtime: %s -- Evergreen by setting.", describe()
            )
        return None

    folder = resolve_webview2_fixed_runtime(apps_files_dir, logger)
    if folder:
        os.environ[var] = folder
        if logger is not None:
            logger.info("WebView2 runtime: %s at %s", describe(folder), folder)
        return folder

    if mode == "fixed":
        if logger is not None:
            logger.warning(
                "WebView2 runtime: 'fixed' was requested but no bundled "
                "runtime was found under %s. Falling back to the "
                "system-wide Evergreen runtime.",
                os.path.join(apps_files_dir, "webview2"))
    if logger is not None:
        logger.info(
            "WebView2 runtime: %s -- no bundled runtime present.", describe()
        )
    return None


def ensure_webview2_accessibility_args(logger=None):
    """
    Forces WebView2's browser process to build its accessibility
    tree eagerly instead of lazily. WebView2's lazy enablement is
    unreliable with JAWS/NVDA -- the virtual cursor parks at the
    document edge until one Tab, and big HTML messages become almost
    unusable -- while real Chromium browsers read the same pages
    instantly (user-verified in Cromite). Chromium's own
    --force-renderer-accessibility switch is the documented lever.

    Must run before the first wx.html2.WebView is created: that is
    when WebView2 creates its environment and launches its browser
    process, and the loader picks additional flags up from the
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS environment variable at
    environment creation. Existing flags (if any) are preserved and
    the switch is only added once.
    """
    flag = "--force-renderer-accessibility"
    var = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    current = os.environ.get(var, "")
    args = current.split()
    if flag not in args:
        args.append(flag)
        os.environ[var] = " ".join(args)
    if logger is not None:
        logger.info("WebView2 additional browser args: %s", os.environ.get(var, ""))


def ensure_webview2_data_folder(cache_dir, logger=None):
    """
    Keeps the HTML view's browser data (its cache, cookies and local
    storage) inside ZBox's own folder, in data\\cache\\webview2, so a
    portable ZBox leaves none of it on the computer it runs on. Without
    this, wxWidgets puts it in the Windows account's Local AppData folder.

    WEBVIEW2_USER_DATA_FOLDER is the WebView2 loader's documented override,
    read when the first wx.html2.WebView creates the environment, the same
    way as the two variables above, so it must be set before then. A value
    set outside ZBox is left alone. Browser data an earlier ZBox left in
    Local AppData\\ZBox\\EBWebView is deleted.
    """
    import shutil

    var = "WEBVIEW2_USER_DATA_FOLDER"
    folder = os.path.join(cache_dir, "webview2")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    if not os.environ.get(var):
        os.environ[var] = folder
    local = os.environ.get("LOCALAPPDATA")
    if local:
        old_root = os.path.join(local, "ZBox")
        old = os.path.join(old_root, "EBWebView")
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
            try:
                os.rmdir(old_root)  # only when nothing else is in it
            except OSError:
                pass
    if logger is not None:
        logger.info("WebView2 data folder: %s", os.environ.get(var, ""))


def main(minimized=False, debug=False, updated_from=None, update_failed=False):
    from app.paths import Paths
    from app.settings_manager import SettingsManager

    paths = Paths(
        base=BASE_DIR,
        app=APP_DIR,
        apps_files=APPS_FILES_DIR,
        himalaya=HIMALAYA_DIR,
        data=DATA_DIR,
        userdata=USERDATA_DIR,
        cache=CACHE_DIR,
        config=CONFIG_DIR,
        logs=LOGS_DIR,
    )
    settings_manager = SettingsManager(paths)
    # --debug turns logging on for this run only, whatever Settings
    # says, and is never saved. A packaged build's very first run has
    # logging off (settings_manager._load_shipped_defaults), so this
    # is the one way to get a log of the first-run path.
    debug_logging = settings_manager.settings.debug_logging or debug

    # Private mode (app/private_mode.py): no debug log, and whatever a run
    # that did not close normally left behind is deleted before anything
    # else starts.
    if getattr(settings_manager.settings, "private_mode", False):
        import private_mode

        private_mode.ACTIVE = True
        debug_logging = False
        private_mode.clear_local_copies(CACHE_DIR, LOGS_DIR)
    setup_logging(debug_logging)
    install_exception_logging()
    logger = logging.getLogger("zbox.main")
    if _WX_INSTANCE_RESULT:
        logger.info("wx instance handle: %s", _WX_INSTANCE_RESULT)

    # Before the app can open its first HTML message: decide which
    # WebView2 runtime it will use (see ensure_webview2_runtime) and
    # make sure that runtime launches its browser process with the
    # accessibility tree forced on (see
    # ensure_webview2_accessibility_args). Both are read by the
    # WebView2 loader when the environment is created, which happens
    # the moment the first wx.html2.WebView is constructed.
    ensure_webview2_runtime(
        APPS_FILES_DIR,
        getattr(settings_manager.settings, "webview2_runtime", "auto"),
        logger,
    )
    ensure_webview2_accessibility_args(logger)
    ensure_webview2_data_folder(CACHE_DIR, logger)

    try:
        from app.main_frame import ZBoxApp

        if not os.path.isfile(paths.himalaya_binary):
            logger.error("Himalaya binary missing at %s", paths.himalaya_binary)
            show_startup_error(
                FileNotFoundError(
                    "Himalaya CLI was not found in the himalaya folder. "
                    "Run launcher.bat, which downloads it automatically, "
                    "or place himalaya.exe in the himalaya folder yourself."
                ),
                debug_logging,
            )
            sys.exit(1)

        check_for_plaintext_password(paths, logger)

        app = ZBoxApp(
            paths, settings_manager, minimized=minimized,
            updated_from=updated_from, update_failed=update_failed,
        )
        app.MainLoop()
        # Private mode, as it is now (it may have been turned on in
        # Settings during this run): the clean-up after the window.
        if getattr(settings_manager.settings, "private_mode", False):
            import private_mode

            private_mode.close_cleanup(
                paths, getattr(settings_manager.settings, "private_home", ""),
            )

    except Exception as exc:
        logger.exception("ZBox failed to start.")
        show_startup_error(exc, debug_logging)
        sys.exit(1)


def check_for_plaintext_password(paths, logger):
    """
    Warns, but does not block startup, if himalaya.toml still has a
    leftover auth.raw line from before passwords moved to the OS
    credential store. This can only happen on a config file written
    by an older version of ZBox; new writes never include auth.raw.
    """
    if not os.path.isfile(paths.himalaya_config_file):
        return

    try:
        with open(paths.himalaya_config_file, "r", encoding="utf-8") as handle:
            contents = handle.read()
    except OSError:
        return

    if "auth.raw" not in contents:
        return

    logger.warning(
        "himalaya.toml contains a leftover auth.raw entry, "
        "a plaintext password from before ZBox moved secrets to "
        "the OS credential store."
    )

    try:
        import wx

        import lang

        warn_app = wx.App(False)
        wx.MessageBox(
            lang.t(
                "dialogs", "plain_text_password_found",
                default="data\\config\\himalaya.toml still contains a plain text "
                "password (auth.raw) from an older account setup.\n\n"
                "Remove that account and re-add it, or delete "
                "data\\config\\himalaya.toml, so it regenerates using the "
                "secure credential store instead.",
            ),
            lang.t(
                "dialogs", "title_plain_text_password",
                default="Plain Text Password Found",
            ),
            wx.OK | wx.ICON_WARNING,
        )
    except Exception:
        print(
            "WARNING: data\\config\\himalaya.toml contains a plain "
            "text password (auth.raw). Remove and re-add the affected "
            "account, or delete data\\config\\himalaya.toml."
        )


def show_startup_error(exc, debug_logging=True):
    """
    Shows a visible error dialog instead of letting the window
    close silently. Falls back to a console message if wx itself
    could not be imported.
    """
    log_note = (
        "Details were written to the newest timestamped log in data\\logs"
        if debug_logging
        else "Debug logging is currently off in Settings, so no log "
        "file was written for this error."
    )
    message = f"ZBox failed to start.\n\n{type(exc).__name__}: {exc}\n\n{log_note}"
    try:
        import wx

        import lang

        error_app = wx.App(False)
        wx.MessageBox(
            message,
            lang.t("dialogs", "title_startup_error", default="ZBox Startup Error"),
            wx.OK | wx.ICON_ERROR,
        )
    except Exception:
        print(message)
        input("Press Enter to close...")


def set_source_app_id():
    """Running from source, the process is python.exe, and Windows
    files the window under Python on the taskbar with Python's icon.
    An explicit AppUserModelID makes it ZBox's own taskbar entry, with
    the window's own icon. Not in a build: there ZBox.exe is already
    its own app, and a separate id would split it from its pinned and
    desktop shortcuts."""
    if getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ZBox.Mail")
    except Exception:  # noqa: BLE001 - only the taskbar icon depends on it
        pass


# What set_wx_instance_to_program did, logged once logging is set up.
_WX_INSTANCE_RESULT = ""


def set_wx_instance_to_program():
    """
    Hands wx ZBox's own instance handle, so every window ZBox creates
    belongs to ZBox.exe.

    wxPython sets wx's instance handle to its own _core .pyd when that
    module loads, and wx creates every window with it. JAWS names a
    program after the module its windows belong to, so it took ZBox for
    _core.cp314-win_amd64.pyd: Insert+Q named that file, and Say
    Application Version (Ctrl+Insert+V) found no version resource in it
    and said only "version". With the running program's own handle,
    JAWS reads ZBox.exe's version metadata, and JAWS settings made for
    ZBox apply to ZBox alone. NVDA goes by the process and is
    unaffected either way.

    Must run before anything creates wx.App or a window: wx registers
    its window classes under the handle current at that moment. The
    cost is that wx's own resources inside the .pyd no longer load: its
    special cursors, small file and drive icons, default window icon,
    toolbar colour bitmap, context help cursor and MDI window menu.
    ZBox uses none of them. Any failure leaves wx exactly as it was.
    """
    global _WX_INSTANCE_RESULT
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import glob

        import wx

        folders = [os.path.dirname(wx.__file__), getattr(sys, "_MEIPASS", "")]
        found = []
        for folder in folders:
            if folder:
                found = sorted(glob.glob(os.path.join(folder, "wxbase*u_vc*_x64.dll")))
            if found:
                break
        if not found:
            _WX_INSTANCE_RESULT = "unchanged, wx base DLL not found"
            return
        base = ctypes.WinDLL(found[0])
        get_instance = base.wxGetInstance
        get_instance.restype = ctypes.c_void_p
        set_instance = getattr(base, "?wxSetInstance@@YAXPEAUHINSTANCE__@@@Z")
        set_instance.argtypes = [ctypes.c_void_p]
        set_instance.restype = None
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        program = kernel32.GetModuleHandleW(None)
        previous = get_instance()
        set_instance(program)
        if get_instance() != program:
            set_instance(previous)
            _WX_INSTANCE_RESULT = "unchanged, wx did not take the program handle"
            return
        _WX_INSTANCE_RESULT = "set to the program's own"
    except Exception as exc:  # noqa: BLE001 - JAWS naming only; wx stays as it was
        _WX_INSTANCE_RESULT = "unchanged, %s: %s" % (type(exc).__name__, exc)


if __name__ == "__main__":
    # Checked before main() so the secret path never touches wx,
    # settings, logging or the account list.
    if len(sys.argv) > 1 and sys.argv[1] == "--get-secret":
        sys.exit(run_get_secret(sys.argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--get-token":
        sys.exit(run_get_token(sys.argv[1:]))
    # The update apply step (see app/updater.py): this is the new
    # ZBox.exe running from data\update\staging, swapping itself into
    # the ZBox folder once the old ZBox has exited. Like --get-secret it
    # never touches wx, settings or the single-instance lock.
    if len(sys.argv) > 1 and sys.argv[1] == "--apply-update":
        from updater import run_apply_update

        sys.exit(run_apply_update(sys.argv[1:]))
    # --minimized: a Windows-startup launch (see
    # app/startup_registration.py) -- opens straight to the tray
    # instead of showing the window. Also usable by hand.
    # One instance only. A second launch -- the desktop shortcut,
    # the Start menu, a file association -- must bring the running
    # window back, not start another ZBox: a second copy would mean a
    # second tray icon, a second set of IMAP connections and two
    # writers for one settings.json. wx.SingleInstanceChecker owns
    # the "is one already running" question because it releases its
    # lock cleanly even when a previous run crashed. Kept alive as a
    # module-level name for the life of the process; letting it be
    # collected would release the lock and allow a second instance.
    import wx

    # Before anything creates wx.App; see set_wx_instance_to_program.
    set_wx_instance_to_program()
    _instance_lock = wx.SingleInstanceChecker("ZBox-" + wx.GetUserId())
    if _instance_lock.IsAnotherRunning() and "--relaunch" not in sys.argv[1:]:
        # --relaunch marks a restart-in-place (Settings > Interface
        # language): this launch is the deliberate replacement for the
        # ZBox process that is closing right now, not a second, separate
        # copy, so the check below is skipped for it entirely rather
        # than retried -- reconstructing SingleInstanceChecker in a
        # retry loop proved unreliable in practice regardless of actual
        # timing. An ordinary launch (desktop shortcut, Start menu, a
        # genuine second copy) never carries the flag and keeps this
        # check exactly as before.
        from app.single_instance import (
            grant_foreground, request_raise, wait_until_consumed,
        )

        # This process was started by Explorer, so it holds the right
        # to set the foreground window; the running copy does not.
        # Hand that right on, ask, and stay alive until the running
        # copy has taken the request, so the window really comes to
        # the front with focus instead of only flashing its taskbar
        # button (see single_instance.grant_foreground).
        grant_foreground()
        if request_raise(CONFIG_DIR):
            wait_until_consumed(CONFIG_DIR)
        sys.exit(0)
    set_source_app_id()
    # --updated-from <label> / --update-failed: the first start after
    # an update, passed by the apply step (app/updater.py).
    _updated_from = None
    if "--updated-from" in sys.argv[1:]:
        _at = sys.argv.index("--updated-from")
        _updated_from = sys.argv[_at + 1] if _at + 1 < len(sys.argv) else ""
    main(
        minimized="--minimized" in sys.argv[1:],
        debug="--debug" in sys.argv[1:],
        updated_from=_updated_from,
        update_failed="--update-failed" in sys.argv[1:],
    )
    # The window is closed and _on_close has already stopped the
    # workers, saved the delete queue and closed the connections.
    # Anything still running now -- a pooled connect waiting out its
    # timeout, a thread that is not a daemon -- would keep this
    # process, and the console launcher.bat started it from, alive
    # after ZBox is gone. Flush the log and end here.
    logging.shutdown()
    os._exit(0)
