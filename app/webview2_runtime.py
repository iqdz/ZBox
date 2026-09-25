"""
Which WebView2 build is actually rendering mail.

The point of this module is diagnostic, not functional. ZBox's HTML
message view reads correctly because of how a particular Chromium
build exposes its accessibility tree to UIA -- behaviour with no
compatibility guarantee, on a runtime that Windows Update replaces
silently. If that ever regresses, the first question is "which
version was working before", and a log that only says "Evergreen"
cannot answer it. So the version goes in the log at every launch,
and in Settings where it can be read aloud.

Nothing here affects rendering. Every lookup fails quietly: not
knowing the version is a worse log, never a worse mail client.
"""

import logging
import os
import re

logger = logging.getLogger("zbox.webview2")

# Microsoft's client id for the Evergreen WebView2 Runtime in
# EdgeUpdate's registry. Documented as the way to detect whether the
# runtime is installed; the "pv" value under it carries the version.
RUNTIME_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

_REGISTRY_LOCATIONS = (
    # 64-bit Windows: EdgeUpdate writes to the 32-bit registry view,
    # so the explicit WOW6432Node path works from either bitness.
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s"),
    ("HKLM", r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s"),
    # Per-user install (the runtime can be installed without admin).
    ("HKCU", r"Software\Microsoft\EdgeUpdate\Clients\%s"),
)

_VERSION_RE = re.compile(r"\d+(?:\.\d+){2,3}")


def version_from_path(path):
    """Pulls a version out of a fixed-runtime folder name, which
    Microsoft's package expands as
    Microsoft.WebView2.FixedVersionRuntime.<version>.x64."""
    if not path:
        return ""
    match = _VERSION_RE.search(os.path.basename(os.path.normpath(path)))
    return match.group(0) if match else ""


def _registry_version():
    try:
        import winreg
    except ImportError:
        return ""  # not Windows
    roots = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    for root_name, template in _REGISTRY_LOCATIONS:
        path = template % RUNTIME_CLIENT_GUID
        try:
            with winreg.OpenKey(roots[root_name], path) as key:
                value, _kind = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        value = str(value or "").strip()
        if value:
            return value
    return ""


def _filesystem_version():
    """Fallback for a machine where the registry entry is missing or
    unreadable: the runtime installs into a folder named after its
    own version."""
    roots = [
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    ]
    best = ""
    best_key = ()
    for root in roots:
        if not root:
            continue
        application = os.path.join(root, "Microsoft", "EdgeWebView", "Application")
        try:
            names = os.listdir(application)
        except OSError:
            continue
        for name in names:
            if not _VERSION_RE.fullmatch(name):
                continue
            if not os.path.isfile(
                os.path.join(application, name, "msedgewebview2.exe")
            ):
                continue
            key = tuple(int(part) for part in name.split("."))
            if key > best_key:
                best_key, best = key, name
    return best


def installed_evergreen_version():
    """Version of the system-wide WebView2 runtime, or "" if it
    cannot be determined (which is not the same as "not
    installed" -- treat it as unknown, never as a reason to change
    behaviour)."""
    try:
        version = _registry_version() or _filesystem_version()
    except Exception:
        logger.debug("Could not determine the Evergreen runtime version.",
                     exc_info=True)
        return ""
    return version


def describe(fixed_folder=None):
    """One line naming the runtime in use, for the log and for
    Settings. Written to be read aloud."""
    if fixed_folder:
        version = version_from_path(fixed_folder)
        if version:
            return "Bundled fixed version %s" % version
        return "Bundled fixed version (version not identifiable from the folder name)"
    version = installed_evergreen_version()
    if version:
        return "System-wide runtime, version %s" % version
    return "System-wide runtime, version could not be determined"
