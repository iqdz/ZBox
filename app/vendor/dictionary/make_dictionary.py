"""
Builds ZBox's spelling dictionary, once, from the vendored sources.

Run by scripts/z_make_dictionary.ps1. Its output is committed, so this
never runs on a user's machine and nothing about spell check depends
on it at runtime. That is the whole point: what ships is a plain file,
and a plain file either arrives in a build or it does not.

It lives beside the data rather than in scripts/, which is ignored
whole, because a dictionary nobody can rebuild is not a vendored
asset, it is a mystery file.

Inputs, all in this folder:

  en_US-large.dic   SCOWL size 70, stems with affix flags
  en_US-large.aff   the affix rules, the REP suggestion pairs, and the
                    NOSUGGEST and ONLYINCOMPOUND flags
  en_US.dic         OPTIONAL, SCOWL size 60. Used only as a coarse
                    commonness tier: a word in the smaller list is a
                    more ordinary word than one only in the larger.
  count_1w.txt      OPTIONAL, word then count, most frequent first.
                    Used only to order suggestions.

Outputs, also in this folder:

  words.txt.gz      every valid form, most common first, one per line.
                    A leading "!" marks a word that is valid but must
                    never be offered as a suggestion.
  suggest_rules.txt the REP pairs and the TRY alphabet, lifted from
                    the affix file so the suggester does not have to
                    parse it at runtime.

Why a .dic cannot be used directly: it holds stems, not words. A line
reads walk/SGD, and checking text against the raw stems would flag
walks, walked and walking as misspellings, which is far worse than no
spell check at all.

Why the frequency order matters more here than it would elsewhere: the
large SCOWL list deliberately includes uncommon words that are
themselves likely misspellings of common ones, its own README naming
ort and calender. A sighted writer glances past those in a list. A
screen reader user hears suggestion one. Ordering by how often a word
is actually written is what keeps that first suggestion sensible.
"""

import gzip
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = HERE

DIC_LARGE = os.path.join(VENDOR, "en_US-large.dic")
AFF = os.path.join(VENDOR, "en_US-large.aff")
DIC_SMALL = os.path.join(VENDOR, "en_US.dic")
COUNTS = os.path.join(VENDOR, "count_1w.txt")

OUT_WORDS = os.path.join(VENDOR, "words.txt.gz")
OUT_RULES = os.path.join(VENDOR, "suggest_rules.txt")


class Rule:
    __slots__ = ("strip", "add", "condition")

    def __init__(self, strip, add, condition):
        self.strip = "" if strip == "0" else strip
        self.add = "" if add == "0" else add
        self.condition = condition


def read_affix(path):
    """The affix file, as far as this needs it: prefix and suffix
    rules, which flags cross-product, the two flags that change what a
    word means, the REP pairs and the TRY alphabet."""
    prefixes = {}
    suffixes = {}
    cross = {}
    nosuggest = None
    only_compound = None
    reps = []
    try_letters = ""

    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        keyword = parts[0]

        if keyword == "NOSUGGEST" and len(parts) > 1:
            nosuggest = parts[1]
        elif keyword == "ONLYINCOMPOUND" and len(parts) > 1:
            only_compound = parts[1]
        elif keyword == "TRY" and len(parts) > 1:
            try_letters = parts[1]
        elif keyword == "REP" and len(parts) == 3:
            reps.append((parts[1], parts[2]))
        elif keyword in ("PFX", "SFX") and len(parts) >= 4:
            flag, cross_flag, count = parts[1], parts[2], parts[3]
            try:
                count = int(count)
            except ValueError:
                continue
            cross[flag] = (cross_flag.upper() == "Y")
            table = prefixes if keyword == "PFX" else suffixes
            table.setdefault(flag, [])
            for _ in range(count):
                if index >= len(lines):
                    break
                rule_parts = lines[index].split()
                index += 1
                if len(rule_parts) < 4 or rule_parts[0] != keyword:
                    continue
                strip = rule_parts[2]
                add = rule_parts[3]
                condition = rule_parts[4] if len(rule_parts) > 4 else "."
                # An affix can carry its own flags after a slash. None
                # in this file do, but stripping them costs nothing and
                # stops a silent wrong form if the dictionary is ever
                # updated.
                add = add.split("/")[0]
                table[flag].append(Rule(strip, add, condition))

    return {
        "prefixes": prefixes,
        "suffixes": suffixes,
        "cross": cross,
        "nosuggest": nosuggest,
        "only_compound": only_compound,
        "reps": reps,
        "try": try_letters,
    }


def compile_conditions(table, at_end):
    """Each rule's condition is a character class matched against the
    stem, at the end for a suffix and at the start for a prefix. A
    lone dot means anything."""
    compiled = {}
    for flag, rules in table.items():
        built = []
        for rule in rules:
            if rule.condition == ".":
                pattern = None
            elif at_end:
                pattern = re.compile(rule.condition + "$")
            else:
                pattern = re.compile("^" + rule.condition)
            built.append((rule, pattern))
        compiled[flag] = built
    return compiled


def apply_suffix(word, rule):
    if rule.strip and not word.endswith(rule.strip):
        return None
    stem = word[: len(word) - len(rule.strip)] if rule.strip else word
    return stem + rule.add


def apply_prefix(word, rule):
    if rule.strip and not word.startswith(rule.strip):
        return None
    stem = word[len(rule.strip):] if rule.strip else word
    return rule.add + stem


def expand(dic_path, affix):
    """Every form the dictionary describes. Returns a dict of form to
    True when it must never be suggested."""
    suffixes = compile_conditions(affix["suffixes"], at_end=True)
    prefixes = compile_conditions(affix["prefixes"], at_end=False)
    cross = affix["cross"]
    nosuggest_flag = affix["nosuggest"]
    compound_flag = affix["only_compound"]

    forms = {}
    entries = 0

    with open(dic_path, "r", encoding="utf-8") as handle:
        first = True
        for raw in handle:
            if first:
                # The first line is the entry count, not a word.
                first = False
                if raw.strip().isdigit():
                    continue
            line = raw.strip()
            if not line or line.startswith("\t"):
                continue
            # Morphological fields come after whitespace; drop them.
            line = line.split("\t")[0].split(" ")[0]
            if not line:
                continue
            if "/" in line:
                word, flags = line.split("/", 1)
            else:
                word, flags = line, ""
            if not word:
                continue
            if compound_flag and compound_flag in flags:
                continue
            entries += 1
            silent = bool(nosuggest_flag and nosuggest_flag in flags)

            produced = [word]
            suffixed = []
            for flag in flags:
                for rule, pattern in suffixes.get(flag, ()):
                    stem_ok = pattern is None or pattern.search(word)
                    if not stem_ok:
                        continue
                    made = apply_suffix(word, rule)
                    if made:
                        produced.append(made)
                        if cross.get(flag, False):
                            suffixed.append(made)

            for flag in flags:
                if flag not in prefixes:
                    continue
                for rule, pattern in prefixes.get(flag, ()):
                    if pattern is not None and not pattern.search(word):
                        continue
                    made = apply_prefix(word, rule)
                    if made:
                        produced.append(made)
                    if not cross.get(flag, False):
                        continue
                    for base in suffixed:
                        if pattern is not None and not pattern.search(base):
                            continue
                        combined = apply_prefix(base, rule)
                        if combined:
                            produced.append(combined)

            for form in produced:
                if form in forms:
                    forms[form] = forms[form] and silent
                else:
                    forms[form] = silent

    return forms, entries


def read_small_tier(path):
    """SCOWL size 60, stems only, used as a commonness tier. Membership
    is what matters, so the affixes are irrelevant here."""
    if not os.path.isfile(path):
        return set()
    words = set()
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip().split("/")[0].split("\t")[0]
            if line and not line.isdigit():
                words.add(line.lower())
    return words


def read_counts(path):
    """count_1w, word then count, already most frequent first. Only the
    order is kept; the counts themselves are never used."""
    if not os.path.isfile(path):
        return {}
    ranks = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for position, raw in enumerate(handle):
            word = raw.split("\t")[0].split()[0] if raw.strip() else ""
            if word and word.lower() not in ranks:
                ranks[word.lower()] = position
    return ranks


def main():
    for required in (DIC_LARGE, AFF):
        if not os.path.isfile(required):
            print("MISSING: %s" % required)
            return 1

    print("reading the affix file")
    affix = read_affix(AFF)
    print("  prefixes: %d classes" % len(affix["prefixes"]))
    print("  suffixes: %d classes" % len(affix["suffixes"]))
    print("  REP pairs: %d" % len(affix["reps"]))
    print("  no-suggest flag: %r" % affix["nosuggest"])
    print("  compound-only flag: %r" % affix["only_compound"])

    print("expanding the dictionary")
    forms, entries = expand(DIC_LARGE, affix)
    silent_count = sum(1 for quiet in forms.values() if quiet)
    print("  stems read: %d" % entries)
    print("  forms produced: %d" % len(forms))
    print("  never suggested: %d" % silent_count)

    small = read_small_tier(DIC_SMALL)
    print("  commonness tier (SCOWL 60): %s" % (
        "%d stems" % len(small) if small else "not present, skipped"
    ))

    ranks = read_counts(COUNTS)
    print("  frequency list: %s" % (
        "%d words" % len(ranks) if ranks else "not present, alphabetical order"
    ))

    print("ordering")
    unranked = len(ranks) + 1

    def sort_key(word):
        lowered = word.lower()
        rank = ranks.get(lowered)
        if rank is not None:
            return (0, rank, lowered)
        if lowered in small:
            return (1, len(word), lowered)
        return (2, len(word), lowered)

    ordered = sorted(forms, key=sort_key)

    print("writing")
    with gzip.open(OUT_WORDS, "wt", encoding="utf-8", newline="\n") as handle:
        for word in ordered:
            handle.write(("!" if forms[word] else "") + word + "\n")

    with open(OUT_RULES, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Lifted from en_US-large.aff by z_make_dictionary.py.\n")
        handle.write("# TRY is the affix file's own letter frequency order.\n")
        handle.write("TRY\t%s\n" % affix["try"])
        handle.write("# REP pairs: what people actually write, and what\n")
        handle.write("# they meant. These beat edit distance on the\n")
        handle.write("# misspellings that are two or three edits away but\n")
        handle.write("# one substitution in somebody's head.\n")
        for wrong, right in affix["reps"]:
            handle.write("REP\t%s\t%s\n" % (wrong, right))

    words_size = os.path.getsize(OUT_WORDS)
    rules_size = os.path.getsize(OUT_RULES)
    print("  %s  %d bytes" % (OUT_WORDS, words_size))
    print("  %s  %d bytes" % (OUT_RULES, rules_size))

    print("first 20 in order:")
    print("  " + " ".join(ordered[:20]))
    print("a sample from the middle:")
    middle = len(ordered) // 2
    print("  " + " ".join(ordered[middle:middle + 20]))

    for probe in ("walk", "walked", "walking", "walks", "unhappiness",
                  "reconsidered", "children's", "ort", "calender",
                  "photograph", "recieve", "teh"):
        print("  %-14s %s" % (probe, "in" if probe in forms else "NOT in"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
