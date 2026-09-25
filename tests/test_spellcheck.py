"""
spellcheck: the word list and the suggester.

These run against the dictionary that is actually committed, not a
fixture, because the thing most likely to break here is the dictionary
itself -- a rebuild that silently drops the affix expansion, or a
frequency file that went missing, would leave every test passing
against a stub while the real feature flagged "walking" as a
misspelling.

The suggestion tests are written as "what does a screen reader user
hear first", because that is the whole design constraint. A suggestion
list is scanned by eye and skipped past; it is heard one item at a
time, and item one has to be right.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import spellcheck


class DictionaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = spellcheck.Dictionary()
        cls.loaded = cls.dictionary.load()

    def test_the_dictionary_is_actually_there(self):
        """If this fails, spell check is silently unavailable in every
        copy of ZBox, which is the failure this whole file exists to
        catch early."""
        self.assertTrue(self.loaded)
        self.assertGreater(len(self.dictionary.ranks), 100000)

    def test_inflected_forms_are_known(self):
        """The proof that the affix expansion ran. A .dic holds stems,
        so without expansion every one of these is a misspelling."""
        for word in ("walk", "walks", "walked", "walking",
                     "unhappiness", "reconsidered", "quickly"):
            self.assertTrue(self.dictionary.is_known(word), word)

    def test_ordinary_misspellings_are_not_known(self):
        for word in ("recieve", "teh", "seperate", "definately"):
            self.assertFalse(self.dictionary.is_known(word), word)

    def test_case_does_not_decide_validity(self):
        for word in ("Walk", "WALK", "walk"):
            self.assertTrue(self.dictionary.is_known(word), word)

    def test_possessives_and_contractions(self):
        for word in ("don't", "children's"):
            self.assertTrue(self.dictionary.is_known(word), word)

    def test_common_words_rank_above_obscure_ones(self):
        """The ordering that keeps a suggestion list sensible. SCOWL's
        large list carries ort and calender, and its own README warns
        they are likely misspellings of more common words."""
        self.assertLess(
            self.dictionary.rank("or"), self.dictionary.rank("ort")
        )
        self.assertLess(
            self.dictionary.rank("calendar"), self.dictionary.rank("calender")
        )

    def test_silent_words_are_marked(self):
        self.assertTrue(self.dictionary.silent)
        for word in self.dictionary.silent:
            self.assertIn(word, self.dictionary.ranks)

    def test_suggestion_rules_loaded(self):
        """The affix file's REP pairs. Only the markup fallback uses
        them now -- the Trix body gets Chromium's own suggestions,
        which are instant and are not ours to improve on."""
        self.assertGreaterEqual(len(self.dictionary.replacements), 80)


class SuggestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = spellcheck.Dictionary()
        cls.dictionary.load()

    def first(self, word):
        found = self.dictionary.suggest(word)
        return found[0] if found else None

    def test_single_edit_typos(self):
        self.assertEqual(self.first("teh"), "the")
        self.assertEqual(self.first("recieve"), "receive")

    def test_replacement_pairs_beat_edit_distance(self):
        """fotograph is two edits from photograph and one substitution
        in somebody's head. The affix file's own REP pairs are what
        catch this class of misspelling."""
        self.assertIn("photograph", self.dictionary.suggest("fotograph"))

    def test_no_silent_word_is_ever_offered(self):
        for word in ("definately", "seperate", "wrok", "freind"):
            for suggestion in self.dictionary.suggest(word):
                self.assertNotIn(suggestion.lower(), self.dictionary.silent)

    def test_case_variants_are_collapsed(self):
        """Without this a reader hears suggestion one OF, suggestion
        two of, suggestion three Of. The frequency list is keyed on
        lowercase, so every variant inherits the same rank and they
        sort together."""
        for word in ("teh", "adn", "hte"):
            found = self.dictionary.suggest(word)
            lowered = [suggestion.lower() for suggestion in found]
            self.assertEqual(len(lowered), len(set(lowered)), found)

    def test_suggestions_follow_the_typed_capitalisation(self):
        self.assertEqual(self.first("Teh"), "The")
        self.assertEqual(self.first("TEH"), "THE")

    def test_a_capitalisation_fix_comes_first(self):
        """The word is real and only its case is wrong, which is the
        one case where the answer is certain."""
        found = self.dictionary.suggest("english")
        self.assertTrue(found)
        self.assertEqual(found[0].lower(), "english")

    def test_the_list_is_short_enough_to_hear(self):
        self.assertLessEqual(
            len(self.dictionary.suggest("recieve")), spellcheck.SUGGESTION_LIMIT
        )

    def test_nothing_suggested_for_nothing(self):
        self.assertEqual(self.dictionary.suggest(""), [])


class WalkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = spellcheck.Dictionary()
        cls.dictionary.load()

    def words(self, text):
        return [word for _start, _end, word in
                spellcheck.misspellings(text, self.dictionary)]

    def test_finds_misspellings_in_order(self):
        found = spellcheck.misspellings(
            "I recieve teh message.", self.dictionary
        )
        self.assertEqual([word for _s, _e, word in found], ["recieve", "teh"])
        start, end, word = found[0]
        self.assertEqual("I recieve teh message."[start:end], word)

    def test_quoted_lines_are_left_alone(self):
        """Somebody else's spelling is not the writer's business, and a
        reply is mostly somebody else's text."""
        text = "> teh recieve seperate\nMy own definately.\n"
        self.assertEqual(self.words(text), ["definately"])

    def test_addresses_and_links_are_not_words(self):
        text = (
            "Write to zbox.tester@exampel.com or see "
            "https://exampel.com/qwertyx for teh details.\n"
        )
        self.assertEqual(self.words(text), ["teh"])

    def test_a_personal_word_stops_being_a_misspelling(self):
        personal = spellcheck.Dictionary()
        personal.load()
        self.assertTrue(personal.is_known("walk"))
        self.assertFalse(personal.is_known("zbox"))
        personal.personal.add("zbox")
        self.assertTrue(personal.is_known("zbox"))
        self.assertEqual(
            spellcheck.misspellings("ZBox is fine.", personal), []
        )

    def test_clean_text_reports_nothing(self):
        self.assertEqual(
            self.words("Thanks for the update, the figures look right.\n"), []
        )

    def test_next_misspelling_walks_forward(self):
        """What Ctrl+Shift+F7 is: find the next one, select it, say it,
        and let Chromium's own context menu do the fixing."""
        text = "I recieve teh message."
        first = spellcheck.next_misspelling(text, self.dictionary, 0)
        self.assertIsNotNone(first)
        start, end, word = first
        self.assertEqual(word, "recieve")
        self.assertEqual(text[start:end], word)
        second = spellcheck.next_misspelling(text, self.dictionary, end)
        self.assertIsNotNone(second)
        self.assertEqual(second[2], "teh")
        self.assertIsNone(
            spellcheck.next_misspelling(text, self.dictionary, second[1])
        )

    def test_next_misspelling_on_clean_text(self):
        self.assertIsNone(
            spellcheck.next_misspelling("All fine here.", self.dictionary, 0)
        )


if __name__ == "__main__":
    unittest.main()
