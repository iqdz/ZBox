"""
Translating ZBox's interface.

One folder per language in data/lang, named by its code, holding
that language's own <code>.lang file: data/lang/en/en.lang,
data/lang/fr/fr.lang, data/lang/ar/ar.lang. The same folder is where
that language's about.txt and shortcuts.txt live, if it has them --
one place per language rather than a file and a same-named folder
side by side. INI shape, read by Python's own configparser, so a
translator needs nothing installed and can work in Notepad. That is
the whole reason for this format over JSON or PO: a missing comma in
JSON breaks the file and says so unreadably, while a wrong line here
breaks only its own line, and a screen reader moves through key
equals value one line at a time.

English is always loaded first and every other language is laid over
it, so a key a translation has not reached yet is heard in English
rather than as a blank or a raw key name. A translator can therefore
ship a file covering the announcements alone and it works.

Keys are stable identifiers, never the English sentence. Rewording an
English string must not silently orphan every translation. The
English file carries the English text as its value, so translating is
copying en.lang, renaming it, and replacing the right-hand side.

Placeholders are named, not positional: {count}, {name}. A translator
can move them where their own grammar needs them, and a missing or
misspelled one is reported rather than crashing the app, because a
signature that cannot be spoken is worse than one spoken in English.
"""

import configparser
import logging
import os

logger = logging.getLogger("zbox.lang")

DEFAULT_CODE = "en"
FILE_SUFFIX = ".lang"

# The areas a file is divided into. Section names are identifiers and
# are never translated. Anything outside this list is ignored, so a
# future section cannot break an older build.
SECTIONS = (
    "meta",
    "main_ui",
    "menus",
    "dialogs",
    "actions_announcements",
    "errors",
    "help",
)

# {(section, key): text} for the chosen language, laid over English.
_strings = {}
# {(section, key): text} for English alone, the fallback layer.
_english = {}
_code = DEFAULT_CODE
_dir = ""
_missing_reported = set()


def _parser():
    """interpolation=None because the text is prose: a percent sign in
    a signature or an announcement is a percent sign, not a
    substitution. optionxform=str because keys are case-sensitive
    identifiers and configparser lowercases them otherwise."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    return parser


def read_file(path):
    """One language file as {(section, key): text}, or an empty dict
    if it cannot be read. Never raises: a translator's half-finished
    file must not stop ZBox starting.

    utf-8-sig, because a file saved by Notepad on Windows usually
    carries a byte order mark and the first section header would
    otherwise not match.
    """
    parser = _parser()
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        logger.warning("Could not read the language file %s.", path, exc_info=True)
        return {}
    result = {}
    for section in parser.sections():
        if section not in SECTIONS:
            logger.info("Ignoring unknown section [%s] in %s.", section, path)
            continue
        for key, value in parser.items(section):
            # A dialog body has paragraph breaks and an INI value cannot
            # hold a real newline, so backslash n in the file is one here.
            result[(section, key)] = value.replace("\\n", "\n")
    return result


def file_for(lang_dir, code):
    return os.path.join(lang_dir, code, "%s%s" % (code, FILE_SUFFIX))


def available(lang_dir):
    """Every language present, as a list of (code, name) sorted by
    name, for the Language list in Settings. Each language is its own
    folder, data/lang/<code>/<code>.lang; a folder with no .lang file
    inside it is not a language and is skipped. The name is the
    meta/name value, which is the language written the way its own
    speakers write it; a file without one falls back to its code."""
    result = []
    try:
        entries = sorted(os.listdir(lang_dir))
    except OSError:
        return result
    for entry in entries:
        if not os.path.isdir(os.path.join(lang_dir, entry)):
            continue
        path = file_for(lang_dir, entry)
        if not os.path.isfile(path):
            continue
        strings = read_file(path)
        result.append((entry, strings.get(("meta", "name"), entry)))
    result.sort(key=lambda pair: pair[1].lower())
    return result


def system_locale_name():
    """The Windows display language as a locale name, "pt-BR" or
    "ar-SA", or "" when it cannot be read. The display language
    rather than the regional format, because it is the language the
    rest of Windows is speaking to this person in. Falls back to
    Python's own locale off Windows. Never raises."""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        lcid = kernel32.GetUserDefaultUILanguage()
        buffer = ctypes.create_unicode_buffer(85)
        if kernel32.LCIDToLocaleName(lcid, buffer, len(buffer), 0):
            return buffer.value
    except Exception:  # noqa: BLE001 -- not Windows, or no such call
        pass
    try:
        import locale

        name = locale.getlocale()[0] or ""
        return name.replace("_", "-")
    except Exception:  # noqa: BLE001
        return ""


def match_system_language(codes, locale_name):
    """The language folder that best fits a locale name, or English.

    codes are the folder names present, as available() lists them.
    An exact match wins (pt-BR is pt-br). Then the bare language (de-AT
    is de). Then the first folder of the same language, so a regional
    variant with no folder of its own (pt-AO) still gets Portuguese
    rather than English. Anything else is English. Case and the
    underscore Python uses in place of a hyphen are both ignored.
    """
    present = [str(code).lower() for code in codes or ()]
    name = str(locale_name or "").strip().lower().replace("_", "-")
    if not name:
        return DEFAULT_CODE
    if name in present:
        return name
    base = name.split("-", 1)[0]
    if base in present:
        return base
    for code in sorted(present):
        if code.split("-", 1)[0] == base:
            return code
    return DEFAULT_CODE


def load(lang_dir, code=DEFAULT_CODE):
    """Loads English, then the chosen language over it. Returns the
    code actually in force, which is English when the requested file
    is missing or unreadable -- said plainly in the log rather than
    left as a silently untranslated interface."""
    global _strings, _english, _code, _missing_reported, _dir
    _missing_reported = set()
    _dir = lang_dir
    _english = read_file(file_for(lang_dir, DEFAULT_CODE))
    if not _english:
        logger.warning(
            "No English strings were loaded from %s; the interface falls "
            "back to the text built into the code.", lang_dir,
        )
    if not code or code == DEFAULT_CODE:
        _strings = dict(_english)
        _code = DEFAULT_CODE
        return _code
    chosen = read_file(file_for(lang_dir, code))
    if not chosen:
        logger.warning("No usable %s language file; staying in English.", code)
        _strings = dict(_english)
        _code = DEFAULT_CODE
        return _code
    merged = dict(_english)
    merged.update(chosen)
    _strings = merged
    _code = code
    return _code


def current_code():
    return _code


def is_rtl():
    """Whether the current language reads right to left, from the
    [meta] direction key. Absent, or anything but "rtl", means left
    to right."""
    return _strings.get(("meta", "direction"), "").strip().lower() == "rtl"


def body(name, default=""):
    """One long body of prose, from data/lang/<code>/<name>.txt.

    For the two help texts, which are pages rather than sentences and
    do not belong in an INI value. The chosen language first, then
    English, then the default the call site passed -- which is the
    English built into the code, so no English file has to ship and
    there is no second copy of it to drift.

    Never raises. A missing, unreadable or empty file falls through to
    the default the same way a missing key does.
    """
    codes = [_code] if _code == DEFAULT_CODE else [_code, DEFAULT_CODE]
    for code in codes:
        if not _dir or not code:
            break
        path = os.path.join(_dir, code, "%s.txt" % name)
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
        except OSError:
            continue
        if text.strip():
            return text
        logger.info("The body file %s is empty; using the English.", path)
    return default


def t(section, key, default=None, **values):
    """The translated text for one key.

    Falls through in order: the chosen language, English, then the
    default given at the call site, then the key itself. The call-site
    default is what keeps ZBox speaking before any language file
    exists, and is why wiring a call site can never make it silent.

    Named placeholders are filled from values. A translation missing
    one, or carrying one that does not exist, is reported and the
    English is used instead -- the app says something correct rather
    than raising inside an announcement.
    """
    text = _strings.get((section, key))
    if text is None:
        text = _english.get((section, key))
        if text is None:
            marker = (section, key)
            if marker not in _missing_reported:
                _missing_reported.add(marker)
                logger.info("No language entry for [%s] %s.", section, key)
            text = default if default is not None else key
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        logger.warning(
            "Placeholder problem in [%s] %s for language %s.", section, key, _code,
        )
        fallback = _english.get((section, key), default)
        if fallback is None:
            return text
        try:
            return fallback.format(**values)
        except (KeyError, IndexError, ValueError):
            return fallback


def _mnemonic(label):
    """The letter wx would underline in label, or "" for none. A
    doubled ampersand is a literal ampersand and carries none."""
    index = 0
    while index < len(label) - 1:
        if label[index] == "&":
            if label[index + 1] == "&":
                index += 2
                continue
            return label[index + 1]
        index += 1
    return ""


def _labelled(section, key, text):
    """The shared body of menu_label and control_label. Everything
    from the first tab on is the accelerator and is copied through
    untouched. The mnemonic letter is read from the English and put
    back on the translation, so the same Alt letter reaches the same
    thing in every language. Any ampersand or tab a translation
    carries of its own is removed first."""
    if _code == DEFAULT_CODE:
        return text
    try:
        label, tab, chord = text.partition("\t")
        translated = t(section, key, default=label)
        if translated == label:
            return text
        translated = translated.replace("\t", " ").replace("\n", " ")
        translated = translated.replace("&", "").strip()
        if not translated:
            return text
        mnemonic = _mnemonic(label)
        if mnemonic:
            translated = "%s (&%s)" % (translated, mnemonic)
        return translated + tab + chord
    except Exception:  # noqa: BLE001 -- a label must never stop a window being built
        logger.warning("Could not build the label for %s.", key, exc_info=True)
        return text


def control_label(key, text):
    """One control's label -- a button, a checkbox, a radio button --
    translated, with its mnemonic letter kept exactly as the code
    wrote it. Reads [dialogs], because these are the words in the
    dialogs rather than on the menus.

    A language file cannot move or drop an access key here any more
    than it can on a menu. A label with no ampersand needs none of
    this and goes through t() like any other sentence.
    """
    return _labelled("dialogs", key, text)


def menu_label(key, text):
    """One menu item's label, translated, with its chord and its
    mnemonic letter kept exactly as the code wrote them.

    text is the English label the menu already writes, chord and all:
    Reply then a tab then Ctrl+R. Everything from that tab onwards is
    what wx reads as the accelerator, so it is copied through
    untouched and is never taken from a language file. The mnemonic,
    the letter after the ampersand, is read from the English and put
    back on the translation, appended in brackets the way Windows
    does for a language that cannot carry it inside a word, so the
    same Alt letter opens the same menu whatever it is called. Any
    ampersand or tab a translation carries of its own is removed
    before use.

    A language file therefore cannot create, move or remove a key
    binding. It can only change the words.

    In English the text is returned exactly as it came in.
    """
    return _labelled("menus", key, text)
