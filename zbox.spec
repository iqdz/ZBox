# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ZBox's portable layout.

The built folder is meant to contain exactly four things:

    ZBox.exe
    apps_files/   every Python/wx/pywin32 dependency, plus an
                  optional bundled WebView2 fixed-version runtime
    himalaya/     himalaya.exe
    data/         config, userdata, logs, sounds, lang -- the user's own

contents_directory below is what keeps the root clean: without it
PyInstaller drops several hundred DLLs and .pyd files next to the
executable. It needs PyInstaller 6.0 or newer; build.bat checks.

onedir, not onefile, and deliberately so. A onefile build unpacks the
entire bundle to a temp folder on every launch -- and himalaya.toml
points password.command at ZBox.exe --get-secret, so Himalaya starts
this executable once per connection. Onefile therefore pays a full
unpack per IMAP/SMTP connection, not just per launch.

himalaya/ and data/ are not listed as datas here. Their contents are
copied in by build.bat after this spec runs, so that the developer's
own accounts.json, secrets.dat, himalaya.toml, mail cache and logs
can never be swept into a build by accident -- only the shipped
defaults (settings.default.json), the sound themes and the language
folders are copied.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, get_module_file_attribute

# wxPython's Edge/WebView2 backend is loaded through
# WebView2Loader.dll, which ships inside the wx package beside the wx
# DLLs. PyInstaller does not collect it on its own. Without it
# wx.html2.WebView silently falls back to the old IE backend in a
# build: the load event this app waits for never fires, every
# RunScript call fails, and HTML mail is unusable with a screen
# reader -- while running from source is perfectly fine, because wx
# finds the DLL in its own site-packages folder. That asymmetry cost
# several sessions of chasing an app bug that was only ever a
# packaging one.
#
# Resolved from the wx actually being built against, never a
# hard-coded interpreter path.
_WX_DIR = os.path.dirname(get_module_file_attribute('wx'))
_WEBVIEW2_LOADER = os.path.join(_WX_DIR, 'WebView2Loader.dll')

# A build without it is broken in a way that only shows up at
# runtime, on a machine that is not this one. Stop here instead.
if not os.path.isfile(_WEBVIEW2_LOADER):
    raise SystemExit(
        'WebView2Loader.dll was not found in %s. wxPython normally '
        'ships it there; without it the built app cannot use the '
        'Edge/WebView2 backend and HTML mail will not render. '
        'Reinstall wxPython, or copy the DLL into that folder, then '
        'build again.' % _WX_DIR
    )

# Screen reader client libraries.
#
# ZBox has no speech of its own and never will. An announcement is
# handed to whichever screen reader is running by calling that
# reader's own client library, and the reader speaks it in the user's
# voice, rate and settings. Those client libraries are DLLs that ship
# inside the accessible_output2 package, in a lib folder beside its
# Python files: NVDA's controller client, and the equivalents for
# Dolphin, System Access, PC Talker and ZDSR.
#
# PyInstaller does not collect a package's data folder on its own.
# From source the DLLs are found in site-packages and everything
# works, which is exactly what hides the problem: in a build they are
# simply absent, every announcement reaches nothing, and the app goes
# silent on the only machine that matters, somebody else's. The same
# asymmetry as WebView2Loader.dll above, and the same remedy.
#
# The destination is not arbitrary. Frozen, accessible_output2 looks
# for a client library under <embedded data path>/accessible_output2/
# lib, and its embedded data path is PyInstaller's contents folder,
# which contents_directory above names apps_files. collect_data_files
# keeps the package-relative path, so the DLLs land at
# apps_files/accessible_output2/lib, which is where the lookup goes.
_AO2_CLIENT_LIBS = collect_data_files(
    'accessible_output2', includes=['lib/*.dll']
)

# Nothing about a build without them looks wrong: it starts, it
# works, it says nothing. Stop here instead, as with the loader.
if not _AO2_CLIENT_LIBS:
    raise SystemExit(
        'No screen reader client libraries were found inside the '
        'accessible_output2 package. A build without them cannot '
        'reach any screen reader, so every announcement would be '
        'silent. Reinstall accessible-output2 (it is in '
        'requirements.txt), then build again.'
    )

# The current build's version text, shown in About ZBox. Local only
# and never committed, so a build from a copy without it goes ahead
# and About simply shows no version. Lands at apps_files/version.txt,
# beside apps_files/docs, which is where about_dialog looks.
_VERSION_FILE = [('version.txt', '.')] if os.path.isfile('version.txt') else []

a = Analysis(
    ['main.py'],
    pathex=['app'],
    # Destination 'wx' puts it in apps_files\wx, beside
    # wxmsw*_webview_*.dll and _html2*.pyd, which is where wx looks
    # for it. See the note above the import block.
    binaries=[(_WEBVIEW2_LOADER, 'wx')],
    # Help > Documentation reads the first of these at runtime; the
    # EULA gate and About > View License read the second.
    # contents_directory puts both at apps_files/docs/, which
    # Paths.documentation_file and Paths.license_file fall back to
    # when there is no docs/ folder beside the exe. Without
    # LICENSE.txt here a built copy has no licence text to show on
    # first run, and the gate is the first thing a new user meets.
    # The client libraries collected above are appended here; see
    # the note on them for why the destination must not be changed.
    datas=[
        ('docs/zbox_documentation.txt', 'docs'),
        ('docs/LICENSE.txt', 'docs'),
        # Trix and its stylesheet, for the compose body. These are
        # data, not modules, so the Analysis above never sees them:
        # from source app/compose_body.py finds them beside itself at
        # app/vendor/trix, and in a build they have to be carried
        # here or the message body comes up empty while running from
        # source stays perfect. The same asymmetry as
        # WebView2Loader.dll above, and the same remedy. The
        # destination is package-relative to the contents folder, so
        # they land at apps_files/vendor/trix, which is where
        # compose_body.vendor_dir looks when frozen.
        ('app/vendor/trix/trix.umd.min.js', 'vendor/trix'),
        ('app/vendor/trix/trix.css', 'vendor/trix'),
        ('app/vendor/trix/LICENSE.txt', 'vendor/trix'),
        # The spelling dictionary. Same reasoning as Trix above: data,
        # not modules, so Analysis never sees it, and a build without
        # it has no spell check at all while running from source works
        # perfectly. words.txt.gz is read lazily, the first time spell
        # check is used, not at startup. The SCOWL README carries the
        # licence and has to travel with the word list.
        ('app/vendor/dictionary/words.txt.gz', 'vendor/dictionary'),
        ('app/vendor/dictionary/suggest_rules.txt', 'vendor/dictionary'),
        ('app/vendor/dictionary/README_en_US-large.txt', 'vendor/dictionary'),
        # The ZBox logo. tray_icon.ICON_PATH looks for it in an assets
        # folder beside its own module, which frozen is apps_files.
        ('app/assets/zbox.ico', 'assets'),
    ] + _AO2_CLIENT_LIBS + _VERSION_FILE,
    # accessible_output2.outputs.auto is reached through an import
    # inside a function in app/announce.py. The analysis does follow
    # it today; naming it here keeps the build correct if that import
    # ever moves.
    hiddenimports=['win32com.client', 'accessible_output2.outputs.auto'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ZBox',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off on purpose: it rewrites the wxPython and pywin32
    # DLLs, which is a known source of both load failures and
    # antivirus false positives, and buys nothing that matters for a
    # folder the user keeps on disk anyway.
    upx=False,
    console=False,
    # ZBox.exe's own icon, which Explorer and the desktop shortcut
    # show (desktop_shortcut sets IconLocation to the exe).
    icon='app/assets/zbox.ico',
    contents_directory='apps_files',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ZBox',
)
