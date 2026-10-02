ZBox's spelling dictionary
==========================

What ships, and what only lives here
------------------------------------

words.txt.gz and suggest_rules.txt are generated, committed, and read
at runtime. Everything else in this folder is either a source the
generator reads or the licence text that has to travel with it.

  words.txt.gz        Every valid word form, one per line, most common
                      first. A leading "!" marks a word that is valid
                      but must never be offered as a suggestion.
                      Gzipped because the plain file is around five
                      megabytes; gzip is in the standard library and
                      the reader opens it directly.

  suggest_rules.txt   The REP pairs and the TRY alphabet, lifted out of
                      the affix file so the suggester does not parse
                      Hunspell rules at runtime.

  en_US-large.dic     SCOWL size 70, stems with affix flags.
  en_US-large.aff     The affix rules, the REP pairs, and the NOSUGGEST
                      and ONLYINCOMPOUND flags.
  README_en_US-large.txt
                      SCOWL's own README, carrying its licence. It has
                      to stay with the .dic and .aff.

  count_1w.txt        NOT committed, see .gitignore. Word then count,
                      most frequent first, 333,333 words. Only used to
                      order the output. Fetch it again from
                      https://www.norvig.com/ngrams/ if the dictionary
                      is ever rebuilt.

  en_US.dic           OPTIONAL and not present. SCOWL size 60. If it is
                      added, the generator uses membership in it as a
                      second commonness tier, which gives sane ordering
                      even with no frequency list at all.

Rebuilding
----------

  .\scripts\z_make_dictionary.ps1

Reads this folder, writes words.txt.gz and suggest_rules.txt back into
it, and reports to scripts/z_dict_report.txt. It runs here and never on
a user's machine: what a build carries is a plain file, not a pipeline.

Why the dictionary is expanded rather than used as-is
-----------------------------------------------------

A Hunspell .dic holds stems, not words. A line reads walk/SGD. Checking
text against the raw stems would flag walks, walked and walking as
misspellings, which is worse than having no spell check at all. The
generator applies the affix rules once, here, and what ships is the
finished list.

Why the order matters more than usual
-------------------------------------

SCOWL's large list deliberately includes uncommon words that are
themselves likely misspellings of common ones. Its own README gives
ort and calender as the examples. A sighted writer's eye skips those in
a suggestion list. A screen reader user hears suggestion one. Ordering
by how often a word is actually written is what keeps that first
suggestion sensible, and it is the reason a frequency list is involved
at all.

Licences
--------

SCOWL's word lists are largely public domain; the affix file is BSD
with an attribution clause, Copyright 1993 Geoff Kuenning. The full
terms are in README_en_US-large.txt, which is why that file ships
beside the dictionary rather than being treated as documentation.

count_1w.txt is a harder question and worth recording rather than
forgetting. Peter Norvig's page releases the accompanying CODE under
MIT, and separately notes that the DATA is derived from the Google Web
Trillion Word Corpus, distributed by the Linguistic Data Consortium.
The position taken here is that aggregated word frequency counts are
facts rather than creative expression and so carry no copyright of
their own. The residual question is contractual rather than one of
copyright: the LDC distributed the original corpus under an agreement,
and such terms bind whoever accepted them. Whether that reaches a file
republished on a personal site years later is a judgement, and it was
made deliberately rather than by accident.

This matters less than it looks, because the frequency data is never
the dictionary. SCOWL decides whether a word is valid; the frequency
list only decides the order of suggestions among words SCOWL has
already accepted. Nothing that fails SCOWL can ever be suggested, and a
missing or partial frequency file degrades the ordering rather than
breaking the feature.

Considered and rejected
-----------------------

wordfreq, https://github.com/rspeer/wordfreq, is better frequency data
and the wrong fit. Its README explicitly asks people not to convert it
into a plain data file, on the grounds that such a file has nowhere to
carry attribution and licence, which is precisely what ZBox would be
doing. Its data is CC BY-SA 4.0, so a word list derived from it would
carry ShareAlike obligations inside a closed build. As a library it
costs three new dependencies for a job that happens once. The project
is also sunset, with data frozen around 2021.

The Windows spell check API, ISpellChecker, would give system
dictionaries and the user's own languages for free. It is a plain COM
interface with no IDispatch and no shipped typelib, so pywin32 cannot
reach it; it needs comtypes or hand-declared vtable calls. Both are the
shape of failure this project has already been bitten by twice, with
WebView2Loader.dll and the screen reader client libraries: perfect from
source, silent on somebody else's computer. A vendored file either
arrives in a build or it does not, and the spec can stop the build when
it has not.
