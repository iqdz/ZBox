"""
Spell check: the part Chromium cannot do.

The WebView already has a spell checker. It is C++, it is already
running against the body being typed, and its suggestions appear
instantly on the Application key. Nothing written in Python will ever
beat it, so nothing here tries to.

What Chromium will not give you is navigation. There is no way to ask
it where the misspellings are and no keystroke that moves the caret to
the next one, because a sighted writer does not need either: the
squiggle is visible. That gap is the whole reason this module exists.

So the division is: this finds the words, Chromium fixes them. F7 moves
the caret to the next misspelling and says it; the Application key then
opens Chromium's own suggestions on the word already under the caret.
No dialog, no suggestion list of ours, no waiting.

One suggester survives, at the bottom of this file, and only for the
markup fallback -- the engine used when a machine has no WebView at
all, where there is no Chromium to ask. It is deliberately limited to
a single edit: that path is already the degraded one and is not worth
spending milliseconds on.

The word list is SCOWL size 70, expanded from stems into 168,231 forms
and ordered by how often each word is written. See
app/vendor/dictionary/README.txt.
"""

import gzip
import logging
import os
import re
import sys
import threading

logger = logging.getLogger("zbox.spellcheck")

# A word: a letter, then letters and apostrophes. Both apostrophes,
# because the affix file added U+2019 to its word characters in
# February 2026 so that can't and can’t are both recognised.
WORD = re.compile(r"[^\W\d_][^\W\d_'\u2019]+", re.UNICODE)

# Skipped wholesale. An address or a link is not a misspelling, and a
# reply is full of both.
SKIP_SPANS = re.compile(
    r"""(?:[a-zA-Z][a-zA-Z0-9+.-]*://\S+)"""      # any scheme
    r"""|(?:www\.\S+)"""                           # bare www
    r"""|(?:[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+)""",    # an address
    re.UNICODE,
)

WORDS_FILE = "words.txt.gz"
RULES_FILE = "suggest_rules.txt"
PERSONAL_FILE = "personal_dictionary.txt"

SUGGESTION_LIMIT = 7


def vendor_dir():
    """Where the dictionary lives, from source and frozen. Same two
    place fallback as compose_body's, and for the same reason: a build
    carries these as data into the contents folder, which the spec
    names apps_files."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "vendor", "dictionary")]
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", here)
        candidates.insert(0, os.path.join(base, "vendor", "dictionary"))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, WORDS_FILE)):
            return candidate
    return None


class Dictionary:
    """
    The word list and the user's own additions.

    Built once and kept. Nothing loads at startup; a compose tab warms
    it on a background thread, so the first F7 is not paying for it.
    """

    def __init__(self):
        # Lowercase form to its rank in the file, which is frequency
        # order. Membership here is what "is a real word" means.
        self.ranks = {}
        # Only words that are not plain lowercase. Around ninety per
        # cent of the list is lowercase and needs no entry at all.
        self.spellings = {}
        # Valid, but never offered as a suggestion. Profanity, mostly.
        self.silent = set()
        self.replacements = []
        self.personal = set()
        self.personal_path = None
        self.loaded = False

    # --- loading ------------------------------------------------------

    def load(self, folder=None, personal_path=None):
        folder = folder or vendor_dir()
        self.personal_path = personal_path
        if not folder:
            logger.warning(
                "No spelling dictionary was found. Spell check is unavailable."
            )
            return False
        try:
            self._load_words(os.path.join(folder, WORDS_FILE))
        except OSError:
            logger.exception("The spelling dictionary could not be read.")
            return False
        self._load_rules(os.path.join(folder, RULES_FILE))
        self._load_personal()
        self.loaded = True
        logger.info("Spelling dictionary loaded: %d forms.", len(self.ranks))
        return True

    def _load_words(self, path):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for rank, raw in enumerate(handle):
                word = raw.rstrip("\n")
                if not word:
                    continue
                quiet = word.startswith("!")
                if quiet:
                    word = word[1:]
                    if not word:
                        continue
                lowered = word.lower()
                if lowered not in self.ranks:
                    self.ranks[lowered] = rank
                if word != lowered:
                    self.spellings.setdefault(lowered, []).append(word)
                if quiet:
                    self.silent.add(lowered)

    def _load_rules(self, path):
        """The affix file's own REP pairs: ninety substitutions of what
        people write for what they meant, ph for f, shun for tion. Only
        the markup fallback uses them. Missing rules cost nothing but
        suggestion quality on that one path, so this never fails
        loudly."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    parts = raw.rstrip("\n").split("\t")
                    if parts[0] == "REP" and len(parts) == 3:
                        self.replacements.append((parts[1], parts[2]))
        except OSError:
            logger.warning("The suggestion rules could not be read.")

    def _load_personal(self):
        if not self.personal_path:
            return
        try:
            with open(self.personal_path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    word = raw.strip()
                    if word:
                        self.personal.add(word.lower())
        except OSError:
            # No file yet is the ordinary case, not a problem.
            pass

    # --- what it answers ----------------------------------------------

    def is_known(self, word):
        lowered = (word or "").lower()
        if not lowered:
            return True
        if lowered in self.ranks or lowered in self.personal:
            return True
        # A possessive or a contraction whose stem is a word: the affix
        # file covers most of these, this catches the rest rather than
        # stopping on somebody's own surname twice.
        stem = lowered.split("'")[0].split("\u2019")[0]
        return bool(stem) and (stem in self.ranks or stem in self.personal)

    def add_personal(self, word):
        """Kept for good, not for the length of a session.

        Deliberately separate from Chromium's own Add to Dictionary,
        which writes into the WebView2 user data folder and is
        invisible from here. Two lists that are honestly two lists
        beats one that pretends and then walks you back to the same
        word.
        """
        lowered = (word or "").strip().lower()
        if not lowered:
            return False
        self.personal.add(lowered)
        if not self.personal_path:
            return False
        try:
            folder = os.path.dirname(self.personal_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(self.personal_path, "a", encoding="utf-8") as handle:
                handle.write(lowered + "\n")
            return True
        except OSError:
            logger.warning("Could not write to the personal dictionary.")
            return False

    def rank(self, lowered):
        return self.ranks.get(lowered, len(self.ranks))

    # --- suggestions, for the markup fallback only ---------------------

    def suggest(self, word, limit=SUGGESTION_LIMIT):
        """
        A capitalisation fix, then the REP pairs, then single edits,
        each tier in frequency order.

        One edit only. Two-edit candidates are where all the time goes
        and this is the fallback path, reached only on a machine with
        no WebView at all. The Trix body never calls this: it uses
        Chromium's own suggestions, which are instant.
        """
        raw = (word or "").strip()
        lowered = raw.lower()
        if not lowered:
            return []

        tiers = []
        if lowered in self.ranks and raw not in self.spellings.get(lowered, ()):
            tiers.append([lowered])
        tiers.append(self._replacement_candidates(lowered))
        tiers.append([
            candidate for candidate in self._edits(lowered)
            if candidate in self.ranks
        ])
        return self._collect(tiers, raw, limit)

    def _replacement_candidates(self, lowered):
        found = []
        for wrong, right in self.replacements:
            start = lowered.find(wrong)
            while start != -1:
                candidate = (
                    lowered[:start] + right.replace("_", " ")
                    + lowered[start + len(wrong):]
                )
                if candidate in self.ranks:
                    found.append(candidate)
                start = lowered.find(wrong, start + 1)
        return found

    def _edits(self, word):
        alphabet = "abcdefghijklmnopqrstuvwxyz'"
        out = []
        for index in range(len(word) + 1):
            left, right = word[:index], word[index:]
            if right:
                out.append(left + right[1:])
            if len(right) > 1:
                out.append(left + right[1] + right[0] + right[2:])
            for letter in alphabet:
                if right:
                    out.append(left + letter + right[1:])
                out.append(left + letter + right)
        return out

    def _collect(self, tiers, raw, limit):
        found = []
        seen = set()
        for tier in tiers:
            for candidate in sorted(set(tier), key=self.rank):
                if candidate in seen or candidate in self.silent:
                    continue
                seen.add(candidate)
                found.append(self._spell_like(candidate, raw))
                if len(found) >= limit:
                    return found
        return found

    def _spell_like(self, lowered, typed):
        """One entry per word, spelled the way the writer was going.
        Without this the frequency list, which is keyed on lowercase,
        puts OF directly beside of and a reader hears both."""
        variants = self.spellings.get(lowered)
        options = [lowered] + variants if variants else [lowered]
        if typed.isupper() and len(typed) > 1:
            for option in options:
                if option.isupper():
                    return option
            return lowered.upper()
        if typed[:1].isupper():
            for option in options:
                if option[:1].isupper() and not option.isupper():
                    return option
            return lowered.capitalize()
        return lowered


_DICTIONARY = None
_WARMING = None
_LOAD_LOCK = threading.Lock()


def warm(personal_path=None):
    """Starts the load on a background thread when a compose tab opens,
    so the first F7 does not pay for it. Nothing here touches wx, so a
    plain thread is safe, and get_dictionary waits on the same lock if
    the keystroke arrives first."""
    global _WARMING
    if _DICTIONARY is not None or _WARMING is not None:
        return
    _WARMING = threading.Thread(
        target=get_dictionary, args=(personal_path,), daemon=True,
        name="zbox-spellcheck-warm",
    )
    _WARMING.start()


def get_dictionary(personal_path=None):
    global _DICTIONARY
    with _LOAD_LOCK:
        if _DICTIONARY is None:
            dictionary = Dictionary()
            dictionary.load(personal_path=personal_path)
            _DICTIONARY = dictionary
        elif personal_path and not _DICTIONARY.personal_path:
            _DICTIONARY.personal_path = personal_path
            _DICTIONARY._load_personal()
        return _DICTIONARY


def personal_path_for(userdata_dir):
    if not userdata_dir:
        return None
    return os.path.join(userdata_dir, PERSONAL_FILE)


def misspellings(text, dictionary):
    """
    Every unknown word as (start, end, word), in order.

    A regex pass and a set lookup per word, which is the whole cost of
    spell check now that suggestions are Chromium's job. On a message
    sized body it is a couple of milliseconds.

    Three things are skipped, and each would otherwise make the walk
    useless in a real reply. Quoted lines, because somebody else's
    spelling is not the writer's business. Addresses and links, because
    they are not words. And anything the dictionary knows, including
    what the writer has added themselves.
    """
    source = text or ""
    if not dictionary.loaded:
        return []

    skip = [(match.start(), match.end()) for match in SKIP_SPANS.finditer(source)]

    quoted = []
    position = 0
    for line in source.splitlines(True):
        if line.lstrip().startswith(">"):
            quoted.append((position, position + len(line)))
        position += len(line)

    def inside(start, end, spans):
        for span_start, span_end in spans:
            if start >= span_start and end <= span_end:
                return True
        return False

    found = []
    for match in WORD.finditer(source):
        word = match.group(0)
        if dictionary.is_known(word):
            continue
        if inside(match.start(), match.end(), skip):
            continue
        if inside(match.start(), match.end(), quoted):
            continue
        found.append((match.start(), match.end(), word))
    return found


def next_misspelling(text, dictionary, after=0):
    """The first misspelling at or past a position, or None.

    What F7 is: find it, select it, say it, and let Chromium's own
    context menu do the rest.
    """
    for start, end, word in misspellings(text, dictionary):
        if start >= after:
            return start, end, word
    return None
