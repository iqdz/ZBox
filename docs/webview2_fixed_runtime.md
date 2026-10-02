# Pinning the WebView2 runtime

Status: implemented, 2026-09-03. Applies to the HTML message view
only. Nothing in this document changes how the Text view works.

## What changed

ZBox can now carry its own copy of the WebView2 runtime in
`apps_files\webview2` and render HTML mail with that copy, instead of
whatever Microsoft Edge build Windows Update last left on the
machine. Microsoft calls these two options Fixed Version and
Evergreen. A new setting, Settings > WebView2 runtime, chooses
between them: Automatic (use the bundled copy when it is there),
Bundled fixed version only, or System-wide. The default is
Automatic, and with no bundled copy installed that is byte-for-byte
the behaviour ZBox had before.

## Why this matters here more than it does for most apps

For a normal app the Evergreen runtime is the right default and the
reasoning is simple: security patches arrive without shipping a new
build. ZBox is the case where that reasoning inverts, for three
reasons.

1. The HTML view's accessibility is not a property of ZBox's code.
It is a property of how one specific Chromium build hands its
accessibility tree to Windows' UIA layer, and of that build
honouring `--force-renderer-accessibility`. Both are internal
Chromium behaviours with no compatibility guarantee. Getting HTML
messages to read correctly took many rounds of work against
observed behaviour, not against documented API, and the result was
verified against the runtime version that happened to be installed
at the time.

2. Evergreen updates are silent, system-wide, and on Microsoft's
schedule. There is no notification, no per-app pinning, and no way
to stage them. That means a machine where a blind user read HTML
mail correctly yesterday can stop reading it today with nothing
about ZBox having changed and no event for the user to point at.
For a sighted user a renderer regression looks like a layout
glitch; here it looks like the message is empty. That is the
failure mode ZBox spent the longest fixing, and leaving it exposed
to a background update is leaving it unfixed.

3. An Evergreen runtime that is missing, mid-update, or newly
repaired can fail environment creation outright. WebView2 creates
its environment and launches a separate browser process the first
time a `wx.html2.WebView` is constructed; when that fails, it fails
at the COM layer, deep under wxPython, where ZBox gets an exception
with nothing useful in it rather than a recoverable error. Pinning
a runtime that ships with the app removes that class of startup
failure, and it fits what ZBox already is: a portable folder that
runs from any drive without an installer.

## How it is wired in

The mechanism is one environment variable set before the first
WebView is created, in `main.py`:

    os.environ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"] = <folder>

The Win32 way to do this is the `browserExecutableFolder` parameter
of `CreateCoreWebView2EnvironmentWithOptions`. wxPython does not
expose it: `wx.html2.WebView.New()` takes no backend options and
creates the environment itself. `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER`
is the documented override for exactly that parameter, and is the
mechanism Microsoft's own guidance gives for WinUI apps that cannot
pass it either. Environment variables override the programmatic
values, so this works regardless of what wxWidgets passes
internally.

This is also the same shape as the accessibility fix already in
`main.py`, which sets `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` to add
`--force-renderer-accessibility`. Both are read by the WebView2
loader when the environment is created, so both must be set before
the first WebView exists and neither can be changed afterwards
without restarting ZBox. That is why the setting says it takes
effect on next start.

`main.py` gained two functions:
`resolve_webview2_fixed_runtime()` looks for `msedgewebview2.exe`
either directly in `apps_files\webview2` or one level down in a versioned
folder (the Fixed Version package expands into a folder named after
its version), picking the highest version when several are present;
`ensure_webview2_runtime()` applies the setting and sets the
variable. Every failure path falls through to the system-wide
runtime and logs why. ZBox not starting is worse than ZBox starting
on a different renderer, so a missing or unreadable bundled runtime
is never fatal.

Note for anyone adapting the sketch this came from: there is no
`browser_executable_folder` argument to pass to
`wx.html2.WebView.New`, and ZBox does not `LoadURL` a local server.
It renders message HTML in memory with `SetPage`, which on the Edge
backend goes through `NavigateToString` — the detail that made the
load event report `about:blank` and broke focus detection earlier
this year.

## What we take on by doing this

The bundled runtime is over 250 MB expanded, and it does not
update itself. Chromium security fixes for the HTML view become a
manual step on our side rather than Microsoft's. That cost is
not optional, because of what the HTML view actually does: it
renders untrusted mail. Since the content blocker landed the
exposure is much narrower — remote images, stylesheets, fonts and
media are stripped from the markup before the engine sees them, and
scripts, frames and embedded objects are removed unconditionally —
but "narrower" is not "none". The reader can load a message's
remote content deliberately, and even a blocked message still puts
sender-controlled markup and CSS through the renderer's parser.
That argues for updating the pinned runtime deliberately and on a
schedule, not for pinning one and forgetting it. Treat it like the vendored Himalaya binary: a
pinned dependency with a known version, refreshed when we choose to.

Because of the size, `apps_files\webview2` and any downloaded `.cab` are
gitignored, like `himalaya\himalaya.exe` already was.

## Installing or updating the runtime

Microsoft generates the Fixed Version download link per version on
their download page, so there is no stable URL to script against
and the package has to be fetched by hand once.

1. Open https://developer.microsoft.com/microsoft-edge/webview2 and
   find "Download the WebView2 Runtime".
2. Under Fixed Version, download the x64 package. It is a `.cab`.
3. Put it in the `tools` folder and run `tools\get_webview2.bat`, or
   run `tools\get_webview2.bat C:\path\to\package.cab`. The script
   expands it into `apps_files\webview2` and checks that
   `msedgewebview2.exe` actually landed there.
4. Start ZBox. Settings > WebView2 runtime shows the installed
   version, and `data\logs\zbox_debug.log` records which runtime was
   chosen on that launch.

To update, install the newer package the same way and delete the
older version folder. To go back to Evergreen, delete
`apps_files\webview2` or set the Settings option to System-wide.

Keep an archived copy of any version we ship against — Microsoft
only keeps the most-patched build of the latest two major releases
on the download page.

## Testing a runtime before we adopt it

The reason for pinning is that renderer accessibility can regress.
So a new runtime is not adopted because it is newer. Before
switching, on a machine with a screen reader: open an HTML message
and confirm the virtual cursor lands in the message body rather
than at the document edge or on the wrapper control; confirm the
body reads in full, not just the subject line; toggle Ctrl+B to
Text and back and confirm HTML still reads on the way back; and
confirm the first message opened after launch behaves the same as
later ones, since the first one is what creates the environment.
Those are the four symptoms that defined the original bug.

## Knowing which build you were on

Because the Evergreen runtime changes underneath us without notice,
"it used to work" is only useful if we know what it used to be.
ZBox reads the installed runtime version at every launch -- from
EdgeUpdate's registry entry for the WebView2 client id, falling back
to the versioned folder under `Microsoft\EdgeWebView\Application` --
and writes it to `data\logs\zbox_debug.log` on the line that says which
runtime was chosen. Settings > WebView2 runtime shows the same
thing, along with any bundled version installed.

So a regression report is a diff between two log lines rather than a
guess, and a rollback has a version number to roll back to. The
lookup is diagnostic only: it never gates anything, and a version it
cannot determine is logged as unknown rather than changing what
loads.

## Sources

- https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
- https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/evergreen-vs-fixed-version
- https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/webview2-idl
