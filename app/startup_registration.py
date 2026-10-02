"""
Run ZBox at Windows sign-in (Settings > System tray & window > Run
ZBox at Windows startup).

Uses the per-user HKCU ...\\CurrentVersion\\Run registry value rather
than the shell Startup folder or a Scheduled Task -- no admin rights
needed to write it, it takes effect immediately without a reboot, and
it is the same mechanism Windows' own Task Manager > Startup tab
reads, so a user can also see or turn this off from there without
opening ZBox at all.

winreg is imported lazily inside each function, not at module level,
so this module can still be imported (and startup_command() tested)
on a non-Windows machine -- the same pattern dpapi_secret_store.py
and webview2_runtime.py already use for their own winreg calls.
"""

import os
import sys

REGISTRY_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REGISTRY_VALUE_NAME = "ZBox"


def startup_command(base_dir, frozen=None):
    """
    The exact command line to register, given ZBox's own base
    directory (main.py's BASE_DIR).

    Frozen build: "<base_dir>\\ZBox.exe" --minimized -- ZBox.exe is
    the entry point itself, the same executable himalaya.toml's
    password.command already invokes (see main.py's run_get_secret
    docstring). Passed --minimized so a startup launch opens straight
    to the tray rather than showing the window (per how this was
    scoped) -- main.py parses that flag before anything heavy imports.

    Running from source: sys.executable (whatever interpreter is
    actually running ZBox, e.g. pythonw.exe) plus the real path to
    main.py, so a startup entry still works during development even
    though a packaged install's Run value points at ZBox.exe.

    Pure aside from reading sys.frozen/sys.executable -- frozen can be
    passed explicitly so this is testable on a machine (or in CI)
    that never runs a frozen build at all.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        exe = os.path.join(base_dir, "ZBox.exe")
        return f'"{exe}" --minimized'
    main_py = os.path.join(base_dir, "main.py")
    return f'"{sys.executable}" "{main_py}" --minimized'


def is_registered():
    """Whether the HKCU Run value currently exists. Best-effort: any
    OSError (key missing, value missing, no access) reads as "not
    registered" rather than raising, since this only ever feeds a
    Settings checkbox's initial state."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH, 0, winreg.KEY_READ
        ) as key:
            winreg.QueryValueEx(key, REGISTRY_VALUE_NAME)
        return True
    except OSError:
        return False


def set_registered(enabled, base_dir):
    """Adds or removes the HKCU Run value immediately -- called from
    Settings as soon as the checkbox is toggled and OK'd, not only
    applied on next launch, matching how every other "takes effect
    immediately" setting in this app behaves. Returns True on success,
    False if the registry write itself failed (surfaced as a status
    bar message by the caller, never a hard error -- ZBox's own
    startup behavior is not worth blocking Settings from saving over)."""
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH, 0, winreg.KEY_WRITE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, REGISTRY_VALUE_NAME, 0, winreg.REG_SZ,
                    startup_command(base_dir),
                )
            else:
                try:
                    winreg.DeleteValue(key, REGISTRY_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
