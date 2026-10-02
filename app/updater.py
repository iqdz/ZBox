"""
ZBox's update engine: find a newer release, download it, check it,
and swap it in while ZBox is closed. No wx, no windows, nothing
spoken -- update_dialog.py and main_frame.py do all of that. This
module only ever answers, downloads, verifies and moves files.

Where updates come from. Only the iqdz/ZBox releases on GitHub, over
HTTPS, and only GitHub's own hosts are accepted, redirects included.
A request carries nothing but "ZBox/<version>" as its user agent:
no account, address or id.

What a release carries. Next to the zip, release_zip.ps1 writes
ZBox_update.json: the plain version (26.09.27), the full label
("testing beta26.09.27"), the zip's file name, its size and its
SHA-256. A release without that file is never offered.

How the swap works. The new zip is unpacked into data/update/staging.
The staged ZBox.exe is then started with --apply-update: it waits for
the running ZBox to exit, moves the old program parts into
data/update/previous, copies the new ones in, and starts the new
ZBox. The new ZBox confirms its window opened by writing
data/update/started.flag; without that within a minute, everything is
moved back and the old ZBox is started with --update-failed.

What is never touched: everything under data/ apart from the default
settings template and the language and sound folders the new release
itself ships. Accounts, passwords, settings, mail cache, logs, custom
sound themes and any language folder the user added stay as they are.
A bundled WebView2 runtime in apps_files/webview2 is carried across
when the new release has none of its own.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
import zipfile

REPO = "iqdz/ZBox"
RELEASES_API = "https://api.github.com/repos/%s/releases?per_page=10" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO
MANIFEST_NAME = "ZBox_update.json"

# GitHub's own hosts only. A release asset link on github.com
# redirects to one of the asset hosts; every hop is checked.
ALLOWED_HOSTS = (
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
)

CHECK_TIMEOUT_SECONDS = 20
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_API_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ZIP_BYTES = 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
CHUNK_BYTES = 256 * 1024

# The folder at the top of every release zip (release_zip.ps1 zips
# the build folder itself, which is named ZBox).
ZIP_ROOT = "ZBox"

# How long the apply step waits for the old ZBox to exit, and for the
# new one to confirm its window opened.
EXIT_WAIT_SECONDS = 60
START_WAIT_SECONDS = 60

# The program parts, relative to the ZBox folder. Language and sound
# folders are added per folder from what the new release ships.
PROGRAM_ITEMS = (
    "ZBox.exe",
    "apps_files",
    "himalaya",
    os.path.join("data", "config", "settings.default.json"),
)
PER_FOLDER_ROOTS = (
    os.path.join("data", "lang"),
    os.path.join("data", "sounds"),
)
WEBVIEW2_REL = os.path.join("apps_files", "webview2")


class UpdateError(Exception):
    """Anything that stops a check, download, verify or swap. The
    message is plain and complete enough to show the user."""


class UpdateCancelled(UpdateError):
    """The user pressed Cancel during a download."""


# --- Versions -------------------------------------------------------


def parse_version(text):
    """ "testing beta26.09.27" -> (26, 9, 27). The last dotted run of
    numbers in the text wins; None when there is none."""
    if not text:
        return None
    runs = re.findall(r"\d+(?:\.\d+)+", str(text))
    if not runs:
        return None
    return tuple(int(part) for part in runs[-1].split("."))


def is_newer(candidate, current):
    """Only a known, strictly higher version is newer. An unknown
    current version means updates are never offered."""
    if candidate is None or current is None:
        return False
    return tuple(candidate) > tuple(current)


def version_file(paths):
    """apps_files/version.txt in a build, the project root's
    version.txt from source (the same two places About ZBox reads)."""
    for folder in (paths.apps_files, paths.base):
        path = os.path.join(folder, "version.txt")
        if os.path.isfile(path):
            return path
    return None


def current_label(paths):
    """The first line of version.txt, or "" when there is none."""
    path = version_file(paths)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def current_version(paths):
    return parse_version(current_label(paths))


CHECK_INTERVAL_SECONDS = 24 * 60 * 60
CHECK_IDLE_SECONDS = 10 * 60


def update_check_due(now, last_check, remind_after, idle_seconds, message_open,
                     auto_check=True, frozen=True, offline=False):
    """Whether an automatic update check should run now: the setting
    is on, this is a build, ZBox is not working offline, a day has
    passed since the last check, any Remind me later time has passed,
    and ZBox is idle -- no key for ten minutes and no message open,
    the same idle rule as the cache clean up."""
    if not auto_check or not frozen or offline or message_open:
        return False
    if idle_seconds < CHECK_IDLE_SECONDS:
        return False
    try:
        last_check = float(last_check or 0.0)
        remind_after = float(remind_after or 0.0)
    except (TypeError, ValueError):
        last_check, remind_after = 0.0, 0.0
    if now < remind_after:
        return False
    return now - last_check >= CHECK_INTERVAL_SECONDS or now < last_check


def is_frozen():
    return bool(getattr(sys, "frozen", False))


# --- Network --------------------------------------------------------


def check_url(url):
    """Raises unless url is HTTPS to one of GitHub's own hosts."""
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ALLOWED_HOSTS:
        raise UpdateError("Refused an address outside GitHub: %s" % (host or url))
    return url


class _AllowlistRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _user_agent(label):
    version = parse_version(label)
    return "ZBox/%s" % (".".join(str(n) for n in version) if version else "unknown")


def _open(url, timeout, user_agent, accept="*/*"):
    check_url(url)
    opener = urllib.request.build_opener(_AllowlistRedirects)
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept": accept},
    )
    response = opener.open(request, timeout=timeout)
    check_url(response.geturl())
    return response


def make_fetch(label):
    """A fetch(url, timeout, limit) -> bytes that never reads past
    limit and never leaves GitHub."""
    user_agent = _user_agent(label)

    def fetch(url, timeout, limit):
        try:
            with _open(url, timeout, user_agent, "application/json, */*") as response:
                data = response.read(limit + 1)
        except UpdateError:
            raise
        except Exception as exc:  # noqa: BLE001 - network errors of every kind
            raise UpdateError("Could not reach GitHub: %s" % exc) from exc
        if len(data) > limit:
            raise UpdateError("GitHub sent more data than expected.")
        return data

    return fetch


# --- The update file and the release --------------------------------


class UpdateInfo:
    def __init__(self, version, label, notes, zip_name, zip_url, size, sha256, release_url):
        self.version = version
        self.label = label
        self.notes = notes
        self.zip_name = zip_name
        self.zip_url = zip_url
        self.size = size
        self.sha256 = sha256
        self.release_url = release_url

    @property
    def version_text(self):
        return ".".join("%02d" % n if i else str(n) for i, n in enumerate(self.version))


def parse_manifest(data):
    """Checks ZBox_update.json and returns it as a dict. Raises
    UpdateError on anything missing or out of shape."""
    try:
        manifest = json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("The release's update file could not be read.") from exc
    if not isinstance(manifest, dict):
        raise UpdateError("The release's update file is not in the expected shape.")
    version = parse_version(manifest.get("version"))
    zip_name = manifest.get("zip")
    size = manifest.get("size")
    sha256 = str(manifest.get("sha256") or "").lower()
    if version is None:
        raise UpdateError("The release's update file has no version.")
    if (not isinstance(zip_name, str) or not zip_name.lower().endswith(".zip")
            or "/" in zip_name or "\\" in zip_name or zip_name.startswith(".")):
        raise UpdateError("The release's update file names no usable zip.")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ZIP_BYTES:
        raise UpdateError("The release's update file gives no usable size.")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise UpdateError("The release's update file has no usable checksum.")
    label = str(manifest.get("label") or manifest.get("version")).strip()
    return {"version": version, "label": label, "zip": zip_name, "size": size, "sha256": sha256}


def _releases_newest_first(releases):
    usable = [r for r in releases if isinstance(r, dict) and not r.get("draft")]
    return sorted(usable, key=lambda r: str(r.get("published_at") or r.get("created_at") or ""),
                  reverse=True)


def _asset(release, name):
    for asset in release.get("assets") or ():
        if isinstance(asset, dict) and asset.get("name") == name:
            return asset
    return None


def check_for_update(paths, fetch=None):
    """The newest release's UpdateInfo when it is newer than this
    ZBox, else None. Only the newest release carrying an update file
    counts: an older one is never offered over it."""
    current = current_version(paths)
    if current is None:
        return None
    if fetch is None:
        fetch = make_fetch(current_label(paths))
    try:
        releases = json.loads(fetch(RELEASES_API, CHECK_TIMEOUT_SECONDS, MAX_API_BYTES).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("GitHub's release list could not be read.") from exc
    if not isinstance(releases, list):
        raise UpdateError("GitHub's release list is not in the expected shape.")
    for release in _releases_newest_first(releases):
        manifest_asset = _asset(release, MANIFEST_NAME)
        if manifest_asset is None:
            continue
        manifest = parse_manifest(fetch(
            manifest_asset.get("browser_download_url"), CHECK_TIMEOUT_SECONDS, MAX_MANIFEST_BYTES,
        ))
        if not is_newer(manifest["version"], current):
            return None
        zip_asset = _asset(release, manifest["zip"])
        if zip_asset is None:
            raise UpdateError("The newest release is missing its zip, %s." % manifest["zip"])
        zip_url = check_url(zip_asset.get("browser_download_url"))
        return UpdateInfo(
            version=manifest["version"],
            label=manifest["label"],
            notes=str(release.get("body") or "").strip(),
            zip_name=manifest["zip"],
            zip_url=zip_url,
            size=manifest["size"],
            sha256=manifest["sha256"],
            release_url=str(release.get("html_url") or RELEASES_PAGE),
        )
    return None


# --- Download and verify --------------------------------------------


def update_dir(data_dir):
    return os.path.join(data_dir, "update")


def _remove(path, attempts=10):
    """Deletes a file or folder, retrying briefly while Windows still
    holds it (attempts=1 tries once, with no wait). A missing path is
    fine."""
    for attempt in range(attempts):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.lexists(path):
                os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.5)


def download(info, data_dir, label="", progress=None, cancelled=None, open_url=None):
    """Downloads info's zip to data/update and checks its size and
    SHA-256. Returns the zip's path. A failed or cancelled download
    leaves nothing behind."""
    folder = update_dir(data_dir)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, info.zip_name)
    part = path + ".part"
    if open_url is None:
        user_agent = _user_agent(label)
        open_url = lambda url: _open(url, DOWNLOAD_TIMEOUT_SECONDS, user_agent)
    digest = hashlib.sha256()
    received = 0
    try:
        with open_url(check_url(info.zip_url)) as response, open(part, "wb") as handle:
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateCancelled("The download was cancelled.")
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                received += len(chunk)
                if received > info.size:
                    raise UpdateError("The download is larger than the release says.")
                digest.update(chunk)
                handle.write(chunk)
                if progress is not None:
                    progress(received, info.size)
        if received != info.size:
            raise UpdateError("The download ended early.")
        if digest.hexdigest() != info.sha256:
            raise UpdateError("The download does not match the release's checksum.")
        os.replace(part, path)
        return path
    except UpdateError:
        _remove(part)
        raise
    except Exception as exc:  # noqa: BLE001 - network and disk errors of every kind
        _remove(part)
        raise UpdateError("The download failed: %s" % exc) from exc


def _member_parts(name):
    """A zip entry name as its path parts under ZIP_ROOT, or raises
    for anything that could land outside the staging folder."""
    clean = name.replace("\\", "/")
    if clean.startswith("/") or ":" in clean:
        raise UpdateError("The zip holds a file with an unsafe path.")
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UpdateError("The zip holds a file with an unsafe path.")
    if not parts or parts[0] != ZIP_ROOT:
        raise UpdateError("The zip does not have the ZBox folder at its top.")
    return parts[1:]


def verify_zip(zip_path, expected_version):
    """Checks every path in the zip, that ZBox.exe and version.txt
    are there, and that the version inside is the one announced."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set()
            total = 0
            for member in archive.infolist():
                parts = _member_parts(member.filename)
                total += member.file_size
                if parts:
                    names.add("/".join(parts))
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError("The zip unpacks to more than expected.")
            if "ZBox.exe" not in names:
                raise UpdateError("The zip has no ZBox.exe.")
            version_name = None
            for member in archive.infolist():
                if "/".join(_member_parts(member.filename)) == "apps_files/version.txt":
                    version_name = member.filename
                    break
            if version_name is None:
                raise UpdateError("The zip has no version.txt.")
            inside = archive.read(version_name).decode("utf-8-sig").splitlines()
    except zipfile.BadZipFile as exc:
        raise UpdateError("The download is not a valid zip.") from exc
    found = parse_version(inside[0] if inside else "")
    if found is None or tuple(found) != tuple(expected_version):
        raise UpdateError("The zip holds a different version than the release announced.")
    return True


def stage(zip_path, data_dir, expected_version):
    """Verifies the zip and unpacks it into data/update/staging.
    Returns the staged ZBox folder."""
    verify_zip(zip_path, expected_version)
    staging = os.path.join(update_dir(data_dir), "staging")
    _remove(staging)
    root = os.path.join(staging, ZIP_ROOT)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                parts = _member_parts(member.filename)
                if not parts:
                    continue
                target = os.path.join(root, *parts)
                if member.filename.endswith(("/", "\\")):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(member) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle, CHUNK_BYTES)
    except (OSError, zipfile.BadZipFile) as exc:
        _remove(staging)
        raise UpdateError("The update could not be unpacked: %s" % exc) from exc
    return root


def staged_label(data_dir):
    """The version label of an update already downloaded and staged
    (for silent mode's apply-on-exit), or "" when there is none."""
    path = os.path.join(update_dir(data_dir), "staging", ZIP_ROOT, "apps_files", "version.txt")
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def discard_staged(data_dir, quick=False):
    """Removes a staged update and any downloaded zip, as far as
    Windows allows. quick tries each part once with no wait. Never
    raises: whatever is still in use is left for a later clean up."""
    attempts = 1 if quick else 10
    folder = update_dir(data_dir)
    try:
        _remove(os.path.join(folder, "staging"), attempts)
    except OSError:
        pass
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.lower().endswith((".zip", ".zip.part")):
            try:
                _remove(os.path.join(folder, name), attempts)
            except OSError:
                pass


# --- Handing over to the apply step ---------------------------------


def started_flag(data_dir):
    return os.path.join(update_dir(data_dir), "started.flag")


def mark_started(data_dir):
    """Called by the new ZBox once its window is open: tells a
    waiting apply step that the update worked."""
    try:
        os.makedirs(update_dir(data_dir), exist_ok=True)
        with open(started_flag(data_dir), "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError:
        pass


def cleanup_after_start(data_dir):
    """Called a while after a successful start, once the apply step
    has finished: removes the staging folder and the zip. The
    previous version stays until the next update."""
    discard_staged(data_dir, quick=True)


def start_apply(base_dir, staged_root, pid, from_label):
    """Starts the staged ZBox.exe in apply mode. The caller exits
    ZBox normally straight after. Only possible in a build."""
    if not is_frozen():
        raise UpdateError("ZBox is running from source. Update it with git instead.")
    exe = os.path.join(staged_root, "ZBox.exe")
    if not os.path.isfile(exe):
        raise UpdateError("The staged update has no ZBox.exe.")
    _launch(exe, ["--apply-update", "--target", base_dir, "--pid", str(pid),
                  "--from", from_label or ""], cwd=staged_root)


def _launch(exe, args, cwd):
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen([exe] + list(args), cwd=cwd, close_fds=True, creationflags=flags)


# --- The apply step (runs inside the new ZBox.exe) ------------------


def plan_items(new_root):
    """The program parts the new release ships, relative to the ZBox
    folder: the fixed items plus each language and sound folder."""
    items = [rel for rel in PROGRAM_ITEMS if os.path.lexists(os.path.join(new_root, rel))]
    for root in PER_FOLDER_ROOTS:
        folder = os.path.join(new_root, root)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if os.path.isdir(os.path.join(folder, name)):
                items.append(os.path.join(root, name))
    return items


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def _move(src, dst):
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


def _write_record(previous, record):
    with open(os.path.join(previous, "swap.json"), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1)


def swap(new_root, target, previous, log=lambda text: None):
    """Moves the old program parts into previous and copies the new
    ones in. On any failure, puts everything back and re-raises."""
    items = plan_items(new_root)
    if "ZBox.exe" not in items or "apps_files" not in items:
        raise UpdateError("The staged update is incomplete.")
    _remove(previous)
    os.makedirs(previous, exist_ok=True)
    record = {"items": [], "webview2_carried": False}
    try:
        for rel in items:
            had_old = os.path.lexists(os.path.join(target, rel))
            record["items"].append({"path": rel, "had_old": had_old})
            _write_record(previous, record)
            if had_old:
                _move(os.path.join(target, rel), os.path.join(previous, rel))
            _copy(os.path.join(new_root, rel), os.path.join(target, rel))
            log("replaced %s" % rel)
        old_webview = os.path.join(previous, WEBVIEW2_REL)
        new_webview = os.path.join(target, WEBVIEW2_REL)
        if os.path.isdir(old_webview) and not os.path.lexists(new_webview):
            _move(old_webview, new_webview)
            record["webview2_carried"] = True
            _write_record(previous, record)
            log("kept the bundled WebView2 runtime")
    except Exception:
        log("swap failed; restoring the previous version")
        rollback(target, previous, log)
        raise


def rollback(target, previous, log=lambda text: None):
    """Puts the previous version back, using the record swap wrote.
    A part that cannot be restored is logged and skipped, and the
    rest is still restored. Returns True when every part came back;
    never raises."""
    try:
        with open(os.path.join(previous, "swap.json"), "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        log("nothing to restore")
        return True
    complete = True
    if record.get("webview2_carried"):
        carried = os.path.join(target, WEBVIEW2_REL)
        home = os.path.join(previous, WEBVIEW2_REL)
        try:
            if os.path.isdir(carried) and not os.path.lexists(home):
                _move(carried, home)
        except Exception as exc:  # noqa: BLE001 - keep restoring the rest
            complete = False
            log("could not move back the WebView2 runtime: %s" % exc)
    for entry in reversed(record.get("items") or []):
        rel = entry.get("path")
        if not rel:
            continue
        backup = os.path.join(previous, rel)
        current = os.path.join(target, rel)
        try:
            if os.path.lexists(backup):
                _remove(current)
                _move(backup, current)
                log("restored %s" % rel)
            elif not entry.get("had_old"):
                _remove(current)
                log("removed %s" % rel)
        except Exception as exc:  # noqa: BLE001 - keep restoring the rest
            complete = False
            log("could not restore %s: %s" % (rel, exc))
    if complete:
        try:
            os.remove(os.path.join(previous, "swap.json"))
        except OSError:
            pass
    else:
        log("the restore is incomplete; the parts above were not put back")
    return complete


def wait_for_exit(pid, seconds):
    """True once process pid has exited (or never existed)."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return True
        try:
            return kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def end_processes_in(folder, log=lambda text: None):
    """Ends every process whose program file lies in folder, apart
    from this process and anything under folder's data folder (the
    apply step itself runs from data\\update\\staging). A closing ZBox
    does not end the himalaya.exe runs it started, and a running
    program's file can be neither moved nor deleted. Windows only.
    Returns how many were ended; never raises."""
    if sys.platform != "win32":
        return 0
    try:
        return _end_processes_in(folder, log)
    except Exception as exc:  # noqa: BLE001 - never stop the apply step
        log("could not check for programs still running: %s" % exc)
        return 0


def _end_processes_in(folder, log):
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    root = os.path.normcase(os.path.abspath(folder)).rstrip("\\/") + os.sep
    data = root + "data" + os.sep
    own = os.getpid()
    invalid = ctypes.c_void_p(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == invalid:
        return 0
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ProcessID not in (0, own):
                pids.append(int(entry.th32ProcessID))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    ended = 0
    # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE
    access = 0x1000 | 0x0001 | 0x00100000
    for pid in pids:
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                continue
            path = os.path.normcase(buffer.value)
            if not path.startswith(root) or path.startswith(data):
                continue
            if kernel32.TerminateProcess(handle, 1):
                kernel32.WaitForSingleObject(handle, 5000)
                ended += 1
                log("ended %s (process %s), still running from the ZBox folder"
                    % (os.path.basename(buffer.value), pid))
        finally:
            kernel32.CloseHandle(handle)
    return ended


def apply_update(new_root, target, pid, from_label="", wait_exit=wait_for_exit,
                 launch=_launch, sleep=time.sleep, log=lambda text: None,
                 start_wait=START_WAIT_SECONDS, end_processes=end_processes_in):
    """The whole apply step. Returns 0 when the new ZBox started, 1
    when the old one was restored (or never stopped)."""
    data_dir = os.path.join(target, "data")
    previous = os.path.join(update_dir(data_dir), "previous")
    flag = started_flag(data_dir)
    exe = os.path.join(target, "ZBox.exe")
    if os.path.normcase(os.path.abspath(new_root)) == os.path.normcase(os.path.abspath(target)):
        log("refused: the update is running from the ZBox folder itself")
        return 1
    log("waiting for ZBox (process %s) to exit" % pid)
    if not wait_exit(pid, EXIT_WAIT_SECONDS):
        log("ZBox did not exit; nothing was changed")
        return 1
    end_processes(target, log)
    try:
        os.remove(flag)
    except OSError:
        pass
    try:
        swap(new_root, target, previous, log)
    except Exception as exc:  # noqa: BLE001 - swap already restored
        log("update failed: %s" % exc)
        launch(exe, ["--update-failed"], target)
        return 1
    log("starting the new ZBox")
    process = launch(exe, ["--updated-from", from_label or ""], target)
    waited = 0.0
    while waited < start_wait:
        if os.path.isfile(flag):
            try:
                os.remove(flag)
            except OSError:
                pass
            log("the new ZBox confirmed it started; update complete")
            return 0
        if process is not None and process.poll() is not None:
            break
        sleep(0.5)
        waited += 0.5
    if process is not None and process.poll() is not None:
        log("the new ZBox closed before confirming it started; restoring the previous version")
    else:
        log("the new ZBox did not confirm it started within %d seconds; restoring the previous version"
            % start_wait)
    if process is not None and process.poll() is None:
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
        wait_exit(getattr(process, "pid", 0), 10)
    end_processes(target, log)
    rollback(target, previous, log)
    launch(exe, ["--update-failed"], target)
    return 1


def run_apply_update(argv):
    """Handles "ZBox.exe --apply-update --target <dir> --pid <n>
    --from <label>". Runs before wx, settings and the single-instance
    lock, like --get-secret."""
    args = {}
    rest = list(argv[1:])
    while len(rest) >= 2 and rest[0].startswith("--"):
        args[rest[0][2:]] = rest[1]
        rest = rest[2:]
    target = args.get("target")
    try:
        pid = int(args.get("pid", "0"))
    except ValueError:
        pid = 0
    if not target or not os.path.isdir(target) or pid <= 0:
        return 2
    logs = os.path.join(target, "data", "logs")
    log_path = os.path.join(logs, "update_apply.log")

    def log(text):
        try:
            os.makedirs(logs, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass

    if is_frozen():
        new_root = os.path.dirname(sys.executable)
    else:
        new_root = os.path.dirname(os.path.abspath(sys.argv[0]))
    log("apply started from %s for %s" % (new_root, target))
    try:
        return apply_update(new_root, target, pid, args.get("from", ""), log=log)
    except Exception:  # noqa: BLE001 - logged, never the crash dialog
        log("the apply step stopped on an unexpected error:\n%s" % traceback.format_exc())
        return 1
