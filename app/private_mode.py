"""
Private mode: ZBox keeps no copy of any mail on this computer or on the
drive it runs from, for use from a USB drive on a computer that is not the
user's own.

While it is on, messages are read from the server and kept in memory: no
offline copies, no saved message bodies, no remembered folder names or
message numbers on disk, no debug log. Opened attachments and the HTML
view's browser data still need files for the programs that use them; they
are deleted when ZBox closes, and again at the next start in case ZBox did
not close normally. What stays in the ZBox folder is encrypted: the account
list, the passwords, the address book and the records of the user's own
decisions (secure_json).

Closing ends every program ZBox started and any program holding an opened
attachment, empties the clipboard, deletes himalaya.toml (it holds the
addresses and servers in plain text and is written again at start), and
removes this computer from the list that opens the passwords without the
master password, unless it is the computer private mode was turned on
from.

ACTIVE is read by the modules that write to disk. Nothing here raises.
"""

import logging
import os
import shutil
import sys
import time

ACTIVE = False
WEBVIEW_FOLDER_NAME = "webview2"
ATTACHMENTS_FOLDER_NAME = "attachments"

logger = logging.getLogger("zbox.private")


def webview_data_folder(cache_dir):
    """The HTML view's browser data, inside ZBox's cache folder."""
    return os.path.join(cache_dir, WEBVIEW_FOLDER_NAME)


def _remove(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except OSError:
        pass


def clear_local_copies(cache_dir, logs_dir, keep_webview=False):
    """Deletes everything in the cache folder (offline copies, saved
    message bodies, opened attachments, the HTML view's browser data, the
    remembered folder names and message numbers) and every debug log.
    Accounts, passwords, contacts and settings live elsewhere and are kept.
    keep_webview leaves the browser data while the HTML view still uses
    it; it goes when ZBox closes."""
    for folder, skip in (
        (cache_dir, WEBVIEW_FOLDER_NAME if keep_webview else None),
        (logs_dir, None),
    ):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if name == skip:
                continue
            _remove(os.path.join(folder, name))


def stop_file_logging():
    """Closes this run's debug log so it can be deleted; nothing is logged
    to a file from here on."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
            root.removeHandler(handler)
    if not root.handlers:
        root.addHandler(logging.NullHandler())


# --- ending related programs ---------------------------------------------

def _process_table():
    """{pid: (parent pid, program file name)} for every process, from a
    Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
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
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    table = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def _attachment_holders(attachments_dir):
    """Processes whose command line names a file in the attachments folder:
    the programs Windows opened for an attachment, even when they were not
    started as ZBox's own children."""
    pids = set()
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            needle = os.path.normcase(os.path.abspath(attachments_dir))
            wmi = win32com.client.GetObject("winmgmts:")
            for process in wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process"):
                line = process.CommandLine or ""
                if needle in os.path.normcase(line):
                    pids.add(int(process.ProcessId))
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:  # noqa: BLE001 - the children are still ended
        logger.debug("Programs holding attachments not listed: %s", type(exc).__name__)
    return pids


def _terminate(pid):
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x0001, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def related_processes(table, own_pid, own_name, holders=()):
    """The processes to end: every process descended from ZBox, and every
    holder of an attachment, never ZBox itself and never a process with
    ZBox's own program name (a ZBox restarting in place, or the update
    step, is a child too)."""
    children = {}
    for pid, (parent, _name) in table.items():
        children.setdefault(parent, []).append(pid)
    targets = set()
    pending = [own_pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child != own_pid and child not in targets:
                targets.add(child)
                pending.append(child)
    targets |= set(holders)
    own_name = (own_name or "").casefold()
    return sorted(
        pid for pid in targets
        if pid > 4 and pid != own_pid
        and str(table.get(pid, (0, ""))[1] or "").casefold() != own_name
    )


def end_related_processes(attachments_dir):
    """Ends every program ZBox started (the HTML view's browser processes,
    a Himalaya call still running, a program it opened an attachment with)
    and any program holding an opened attachment. A program that was
    already open and took the file over cannot be told apart from the
    user's other work and is left alone. Returns how many were ended."""
    if os.name != "nt":
        return 0
    try:
        table = _process_table()
    except Exception:  # noqa: BLE001
        table = {}
    targets = related_processes(
        table, os.getpid(), os.path.basename(sys.executable),
        _attachment_holders(attachments_dir),
    )
    ended = sum(1 for pid in targets if _terminate(pid))
    if ended:
        logger.info("Private mode: %d related program(s) ended.", ended)
    return ended


def clear_clipboard():
    """Empties the Windows clipboard."""
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32")
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
            finally:
                user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        pass


def close_cleanup(paths, home_machine=""):
    """Everything private mode does when ZBox closes, after its window is
    gone."""
    attachments = os.path.join(paths.cache, ATTACHMENTS_FOLDER_NAME)
    if end_related_processes(attachments):
        # Windows releases an ended program's files a moment later.
        time.sleep(1.0)
    clear_local_copies(paths.cache, paths.logs)
    _remove(paths.himalaya_config_file)
    clear_clipboard()
    try:
        import dpapi_secret_store

        if dpapi_secret_store.machine_id() != (home_machine or ""):
            dpapi_secret_store.forget_this_machine(paths.config)
    except Exception:  # noqa: BLE001 - the master password still guards the passwords
        pass
