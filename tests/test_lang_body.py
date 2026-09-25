"""
lang.body: the two help pages live in data/lang/<code>/<name>.txt
when a translation supplies one, and in the code otherwise.

No English file ships, so the call-site default is the English and
these tests pin that fallback as much as the override.
"""

import os
import shutil
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import lang


class BodyFallsBackToTheCode(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.folder, "en"), exist_ok=True)
        with open(
            os.path.join(self.folder, "en", "en.lang"), "w", encoding="utf-8"
        ) as handle:
            handle.write("[meta]\nname = English\n")
        lang.load(self.folder, lang.DEFAULT_CODE)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_no_file_gives_the_default(self):
        self.assertEqual(lang.body("about", "Built in."), "Built in.")

    def test_an_empty_file_gives_the_default(self):
        os.makedirs(os.path.join(self.folder, "en"), exist_ok=True)
        with open(
            os.path.join(self.folder, "en", "about.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("   \n\n")
        self.assertEqual(lang.body("about", "Built in."), "Built in.")

    def test_a_directory_in_its_place_gives_the_default(self):
        os.makedirs(os.path.join(self.folder, "en", "about.txt"), exist_ok=True)
        self.assertEqual(lang.body("about", "Built in."), "Built in.")

    def test_an_english_file_is_used_when_one_exists(self):
        os.makedirs(os.path.join(self.folder, "en"), exist_ok=True)
        with open(
            os.path.join(self.folder, "en", "about.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("From the file.\n")
        self.assertEqual(lang.body("about", "Built in."), "From the file.\n")


class BodyPrefersTheChosenLanguage(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        for code in ("en", "fr"):
            os.makedirs(os.path.join(self.folder, code), exist_ok=True)
            with open(
                os.path.join(self.folder, code, "%s.lang" % code), "w", encoding="utf-8"
            ) as handle:
                handle.write("[meta]\nname = %s\n" % code)
        with open(
            os.path.join(self.folder, "fr", "about.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("Texte francais.\n")
        lang.load(self.folder, "fr")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_the_language_file_wins(self):
        self.assertEqual(lang.body("about", "Texte anglais."), "Texte francais.\n")

    def test_a_body_that_language_lacks_falls_back_to_english(self):
        os.makedirs(os.path.join(self.folder, "en"), exist_ok=True)
        with open(
            os.path.join(self.folder, "en", "shortcuts.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("English shortcuts.\n")
        self.assertEqual(
            lang.body("shortcuts", "Built in."), "English shortcuts.\n"
        )

    def test_a_body_neither_has_falls_back_to_the_code(self):
        self.assertEqual(lang.body("nothing", "Built in."), "Built in.")
