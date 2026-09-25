"""
Desktop shortcut creation (first-run wizard, and Settings > System
tray and window > Create desktop shortcut).

A .lnk records an absolute target path, fixed at the moment it is
written. ZBox is portable, so that path is resolved from the running
app's own BASE_DIR (paths.base) every time a shortcut is created, and
creating it again simply overwrites the old one -- which is the whole
repair procedure after the ZBox folder is moved or copied to another
computer.

The user's own Desktop is writable without elevation, so nothing here
asks for admin rights and nothing is written outside that folder.

win32com is imported lazily inside the function rather than at module
level, the same pattern startup_registration.py uses for winreg, so
this module stays importable (and shortcut_target testable) on a
machine without pywin32 or without Windows.
"""

import os
import sys

SHORTCUT_NAME = "ZBox.lnk"


def shortcut_target(base_dir, frozen=None):
    """The three .lnk fields, given ZBox's own base directory.

    Frozen build: ZBox.exe in that folder, no arguments. Running from
    source: the interpreter actually running ZBox, with the real path
    to main.py as its argument -- so a shortcut made during
    development starts the same copy the developer is working on.
    Working directory is the ZBox folder either way.

    Pure apart from reading sys.frozen/sys.executable; frozen can be
    passed explicitly so this is testable off a frozen build.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return os.path.join(base_dir, "ZBox.exe"), "", base_dir
    return sys.executable, '"%s"' % os.path.join(base_dir, "main.py"), base_dir


def shortcut_icon(base_dir, frozen=None):
    """Where the shortcut's icon comes from: ZBox.exe itself in a
    build, app\\assets\\zbox.ico from source (the interpreter's own
    icon otherwise, when the file is missing)."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return os.path.join(base_dir, "ZBox.exe")
    ico = os.path.join(base_dir, "app", "assets", "zbox.ico")
    return ico if os.path.isfile(ico) else sys.executable


def create_desktop_shortcut(base_dir, frozen=None):
    """Writes (or overwrites) ZBox.lnk on this user's Desktop and
    returns its full path. Raises RuntimeError with a sentence fit to
    show the user if it cannot.

    The Desktop folder is asked of Windows rather than assumed to be
    %USERPROFILE%\\Desktop: a OneDrive-redirected Desktop is somewhere
    else entirely, and writing to the literal path would put the
    shortcut in a folder nobody sees.

    No CoInitialize call: this only ever runs on the wx main thread,
    where wxWidgets has already initialised OLE.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    target, arguments, working_dir = shortcut_target(base_dir, frozen)
    if not os.path.isfile(target):
        raise RuntimeError(
            "ZBox's own program file was not found at %s." % target
        )
    try:
        from win32com.client import Dispatch
    except ImportError as exc:
        raise RuntimeError(
            "The pywin32 package is not available, so a shortcut "
            "cannot be created (%s)." % exc
        )
    try:
        shell = Dispatch("WScript.Shell")
        desktop = shell.SpecialFolders("Desktop")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Windows refused the shortcut request: %s" % exc)
    if not desktop:
        raise RuntimeError(
            "Windows did not report a Desktop folder for this user "
            "account."
        )
    path = os.path.join(desktop, SHORTCUT_NAME)
    try:
        link = shell.CreateShortCut(path)
        link.TargetPath = target
        link.Arguments = arguments
        link.WorkingDirectory = working_dir
        link.Description = "ZBox mail"
        link.IconLocation = shortcut_icon(base_dir, frozen)
        link.Save()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("The shortcut could not be written: %s" % exc)
    return path
