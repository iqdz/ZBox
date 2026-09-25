"""
Audio notification / sound-theme system.

sounds/default/ ships with ZBox and is always present. A user can add
a sibling folder under sounds/ (e.g. sounds/custom-theme/) with the
same file names to make a custom theme selectable in Settings > Sound
& Notifications; it doesn't need every file -- resolve_sound_path
falls back to default's copy of whichever one is missing, file by
file, not theme by theme.

Every required name is a fixed stem (no ".wav" -- resolve_sound_path
appends that), spelled exactly as ZBox looks them up everywhere else
in the app:

    progress-tick     rapid tick, played repeatedly during a bulk
                       action's progress bar
    incoming-message  a new message has arrived
    action-done       sending, replying or forwarding completed
    delete            a delete (to Trash or permanent) completed
    searching         a search started
    searching_done    a search completed with at least one match
    none_found        a search completed with no matches

The shipped sounds/default/*.wav files are short tones generated from
scratch (see docs/decisions.md) rather than sourced third-party audio:
the closest real "open, freely bundleable" catalog (freedesktop.org's
sound-theme-freedesktop, pulled from KDE/Ekiga/Pidgin/ALSA) carries a
mix of CC-BY-3.0, CC-BY-SA-3.0 and GPL-2 licensed files, each needing
its own per-file attribution rather than being simply "free to use" --
not something to get right by guessing, and not necessary when an
original tone does the same job with no license to track at all.
Anyone can still drop real recordings into sounds/default/ (or a new
theme folder) under these exact names.
"""

import logging
import os

try:
    import winsound
    WINSOUND_AVAILABLE = True
except ImportError:
    # Always the case off Windows (this project's own Linux-side dev
    # tooling and test runs included) -- ZBox itself only ever runs on
    # Windows, where winsound ships with every Python. `winsound` is
    # still bound (to None) rather than left undefined so tests can
    # patch it uniformly regardless of platform.
    winsound = None
    WINSOUND_AVAILABLE = False

logger = logging.getLogger("zbox.sound")

# Exact stems every theme is checked for, in the order Settings shows
# them for reference. A theme need not have all of them -- see
# resolve_sound_path -- but at least one is what makes a folder count
# as a theme at all in available_themes().
REQUIRED_SOUND_NAMES = (
    "progress-tick",
    "incoming-message",
    "action-done",
    "delete",
    "searching",
    "searching_done",
    "none_found",
)

DEFAULT_THEME = "default"


def resolve_sound_path(sounds_dir, theme, name):
    """
    Path to `name`.wav for `theme` under `sounds_dir`, falling back to
    the default theme's own copy when the chosen theme doesn't have
    that one file -- a custom theme is allowed to override only some
    sounds. Returns None if neither has it (including when sounds_dir
    itself doesn't exist), which callers treat as "nothing to play",
    never an error.
    """
    if not sounds_dir:
        return None
    theme = (theme or DEFAULT_THEME).strip() or DEFAULT_THEME
    filename = f"{name}.wav"

    candidate = os.path.join(sounds_dir, theme, filename)
    if os.path.isfile(candidate):
        return candidate

    if theme != DEFAULT_THEME:
        fallback = os.path.join(sounds_dir, DEFAULT_THEME, filename)
        if os.path.isfile(fallback):
            return fallback

    return None


def available_themes(sounds_dir):
    """
    Subfolder names directly under sounds_dir that look like a usable
    theme -- containing at least one of REQUIRED_SOUND_NAMES, so a
    stray empty or unrelated folder someone drops in sounds/ doesn't
    show up as a selectable theme. "default" sorts first when present;
    everything else follows alphabetically. Empty list (not an
    exception) if sounds_dir doesn't exist or isn't readable.
    """
    if not sounds_dir:
        return []
    try:
        entries = os.listdir(sounds_dir)
    except OSError:
        return []

    themes = []
    for entry in entries:
        folder = os.path.join(sounds_dir, entry)
        if not os.path.isdir(folder):
            continue
        if any(
            os.path.isfile(os.path.join(folder, f"{name}.wav"))
            for name in REQUIRED_SOUND_NAMES
        ):
            themes.append(entry)

    themes.sort(key=lambda name: (name != DEFAULT_THEME, name.lower()))
    return themes


def play(sounds_dir, settings, name):
    """
    Plays `name`.wav from settings.sound_theme (falling back to
    default -- see resolve_sound_path), unless settings.sounds_enabled
    is off, winsound isn't available, or no matching file exists
    either way. Always a silent no-op rather than a dialog or a
    raised exception on any failure -- a decorative sound is never
    worth interrupting anything over, per the same reasoning
    announce.py documents for its own dialog.

    Uses the stdlib winsound module, not wx.Sound. Two real problems
    with wx.Sound in turn, both found live rather than guessed at:
    first, its Windows backend plays asynchronously by handing the
    sound data off to the OS, which needs that data to stay alive for
    as long as playback takes -- a wx.Sound built as a local variable
    here was garbage-collected the instant this function returned,
    cutting every notification off before it was ever audible, with
    nothing about that looking like a failure (IsOk() true, Play()
    returning normally). Caching an instance per path fixed that, but
    then a real user hit a second, more fundamental problem: their
    installed wxPython raised `AttributeError: module 'wx' has no
    attribute 'Sound'` outright, even though the exact same .wav
    files played fine in Windows Media Player -- Sound support
    depends on how that particular wxWidgets/wxPython build was
    compiled, not just on the file being valid. ZBox only ever runs
    on Windows (DPAPI, WebView2, pywin32 already assume it), so
    winsound.PlaySound(path, SND_FILENAME | SND_ASYNC) sidesteps both
    problems at once: it ships with every Windows Python rather than
    depending on wxWidgets' own build options, and it hands the file
    to the OS directly with no Python object that needs to outlive
    this call.
    """
    if not getattr(settings, "sounds_enabled", True):
        return
    if not WINSOUND_AVAILABLE:
        return

    path = resolve_sound_path(
        sounds_dir, getattr(settings, "sound_theme", DEFAULT_THEME), name,
    )
    if not path:
        return

    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        logger.warning("Could not play sound %s.", path, exc_info=True)
