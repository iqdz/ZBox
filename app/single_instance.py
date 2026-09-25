"""
The one-instance guard's file side.

Why a file rather than an IPC channel. The second launch must not
build an app at all -- no wx.App, no tray icon, no IMAP connections,
no second writer for settings.json -- so it has to say "come to the
front" and exit in the few milliseconds before any of that. Dropping
one empty file in the config folder does that with nothing but the
standard library, and the running instance notices it on a 250 ms
timer (main_frame._check_raise_request).

Deliberately not a lock file: wx.SingleInstanceChecker already owns
the question of whether another instance exists, and it survives a
crash cleanly, which a hand-rolled lock file does not. This file only
ever means "somebody just tried to launch me again".

Pure and wx-free, so both halves are tested directly.
"""

import logging
import os
import sys
import time

logger = logging.getLogger("zbox.instance")

# AllowSetForegroundWindow's "any process" value.
_ASFW_ANY = -1
_SW_RESTORE = 9

REQUEST_FILENAME = "raise_request"


def _request_path(config_dir):
    return os.path.join(config_dir, REQUEST_FILENAME)


def request_raise(config_dir):
    """Asks the running instance to show itself. Returns True when the
    request was left where it will be found.

    Never raises: a second launch that cannot write the file has still
    done the important half of its job, which is not starting a second
    copy of ZBox.
    """
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(_request_path(config_dir), "w", encoding="utf-8") as handle:
            handle.write("")
        return True
    except OSError:
        logger.warning("Could not leave a raise request in %s.", config_dir, exc_info=True)
        return False


def consume_raise_request(config_dir):
    """True exactly once per request: the file is removed as it is
    read, so a window is raised once per launch attempt rather than
    every tick for as long as the file sits there."""
    path = _request_path(config_dir)
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError:
        # Removable but not right now (a virus scanner has it open).
        # Reporting False means the next tick tries again, which is
        # better than raising the window on every tick until it frees.
        logger.debug("Could not consume the raise request at %s.", path, exc_info=True)
        return False
    return True


def wait_until_consumed(config_dir, timeout=3.0, interval=0.05):
    """Keeps the second launch alive until the running copy has taken
    its request, or until timeout. True when it was taken.

    Why wait at all. Windows only lets a process take the foreground
    if it was handed that right, and the process Explorer just started
    from a shortcut is the one that holds it. grant_foreground passes
    it on to any process, but a grant from a process that has already
    exited is not something to rely on. Waiting the fraction of a
    second the running copy's timer needs costs nothing visible.
    """
    path = _request_path(config_dir)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not os.path.exists(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def grant_foreground():
    """Lets any process take the foreground next. Called by the second
    launch, which Explorer started and which therefore holds the right
    the running copy needs to come to the front. Without this, the
    running copy's Raise only flashes its taskbar button: the window
    stays behind, focus stays on the desktop, and a screen reader says
    nothing at all. True when Windows accepted it. Never raises."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY))
    except Exception:  # noqa: BLE001
        logger.debug("AllowSetForegroundWindow failed.", exc_info=True)
        return False


def force_foreground(hwnd):
    """Brings a top-level window to the front and gives it the
    keyboard. True when it is the foreground window afterwards.
    Never raises.

    SetForegroundWindow first, which works whenever grant_foreground
    ran. If Windows still refuses, the usual fallback: attach this
    thread's input to the current foreground window's thread for the
    one call, which Windows accepts as the same input queue asking.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        # Own WinDLL instances, so the prototypes below never touch
        # the shared ctypes.windll objects other code relies on. A
        # typed HWND keeps a 64-bit handle from overflowing a C int
        # and keeps the comparisons below exact.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        hwnd = int(hwnd)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        if (user32.GetForegroundWindow() or 0) == hwnd:
            return True
        foreground = user32.GetForegroundWindow()
        other_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        this_thread = kernel32.GetCurrentThreadId()
        if other_thread and other_thread != this_thread:
            user32.AttachThreadInput(this_thread, other_thread, True)
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(this_thread, other_thread, False)
        return (user32.GetForegroundWindow() or 0) == hwnd
    except Exception:  # noqa: BLE001
        logger.debug("Bringing ZBox to the front failed.", exc_info=True)
        return False
