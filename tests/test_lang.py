"""
app/lang.py -- the interface translation loader.

Covers reading a language file, laying a translation over English,
the fallback chain when a key is missing, and what happens to a
translation whose placeholders are wrong. The Settings list and the
call sites are elsewhere; this is the part that has to be right
before any of that is worth wiring.
"""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import lang


ENGLISH = """
[meta]
name = English
code = en

[actions_announcements]
draft_saved = Draft saved.
refresh_all_done = Refreshed {count} accounts.
signature_none = This account has no signature.
"""

FRENCH = """
[meta]
name = Francais
code = fr

[actions_announcements]
draft_saved = Brouillon enregistre.
refresh_all_done = {count} comptes actualises.
"""


class LanguageFiles(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="zbox_lang_")
        self._write("en.lang", ENGLISH)
        self._write("fr.lang", FRENCH)

    def tearDown(self):
        lang.load(self.folder, lang.DEFAULT_CODE)

    def _write(self, name, text, encoding="utf-8"):
        # Each language is its own folder now: data/lang/<code>/<code>.lang.
        code = name[: -len(".lang")] if name.endswith(".lang") else name
        os.makedirs(os.path.join(self.folder, code), exist_ok=True)
        with open(os.path.join(self.folder, code, name), "w", encoding=encoding) as handle:
            handle.write(text)

    def test_english_loads_by_default(self):
        self.assertEqual(lang.load(self.folder), "en")
        self.assertEqual(
            lang.t("actions_announcements", "draft_saved"), "Draft saved."
        )

    def test_a_translation_is_laid_over_english(self):
        self.assertEqual(lang.load(self.folder, "fr"), "fr")
        self.assertEqual(
            lang.t("actions_announcements", "draft_saved"), "Brouillon enregistre."
        )

    def test_a_key_the_translation_lacks_falls_back_to_english(self):
        lang.load(self.folder, "fr")
        # signature_none is in en.lang only. A part-finished
        # translation must be heard in English, never as a blank.
        self.assertEqual(
            lang.t("actions_announcements", "signature_none"),
            "This account has no signature.",
        )

    def test_a_missing_language_file_stays_in_english(self):
        self.assertEqual(lang.load(self.folder, "zz"), "en")
        self.assertEqual(
            lang.t("actions_announcements", "draft_saved"), "Draft saved."
        )

    def test_an_unknown_key_uses_the_call_site_default(self):
        lang.load(self.folder)
        self.assertEqual(
            lang.t("actions_announcements", "not_a_key", default="Spoken anyway."),
            "Spoken anyway.",
        )

    def test_an_unknown_key_with_no_default_returns_the_key(self):
        lang.load(self.folder)
        self.assertEqual(
            lang.t("actions_announcements", "not_a_key"), "not_a_key"
        )

    def test_placeholders_are_filled(self):
        lang.load(self.folder, "fr")
        self.assertEqual(
            lang.t("actions_announcements", "refresh_all_done", count=3),
            "3 comptes actualises.",
        )

    def test_a_translation_with_a_wrong_placeholder_falls_back_to_english(self):
        self._write("xx.lang", """
[meta]
name = Broken
code = xx

[actions_announcements]
refresh_all_done = Refreshed {conut} accounts.
""")
        lang.load(self.folder, "xx")
        self.assertEqual(
            lang.t("actions_announcements", "refresh_all_done", count=2),
            "Refreshed 2 accounts.",
        )

    def test_backslash_n_in_a_value_becomes_a_newline(self):
        # A dialog body has paragraph breaks. An INI value carries them
        # as backslash n and nothing else in a value is touched.
        self._write("nl.lang", """
[meta]
name = Newlines
code = nl

[dialogs]
two_paragraphs = First line.\\n\\nSecond line.
plain = No break here.
windows_path = data\\config\\himalaya.toml
""")
        strings = lang.read_file(os.path.join(self.folder, "nl", "nl.lang"))
        self.assertEqual(
            strings[("dialogs", "two_paragraphs")], "First line.\n\nSecond line."
        )
        self.assertEqual(strings[("dialogs", "plain")], "No break here.")
        self.assertEqual(
            strings[("dialogs", "windows_path")], "data\\config\\himalaya.toml"
        )

    def test_a_byte_order_mark_does_not_hide_the_first_section(self):
        # Notepad on Windows writes one by default, and it would
        # otherwise stop the first section header matching.
        self._write("bo.lang", ENGLISH.replace("English", "Bommed"), encoding="utf-8-sig")
        strings = lang.read_file(os.path.join(self.folder, "bo", "bo.lang"))
        self.assertEqual(strings[("meta", "name")], "Bommed")

    def test_an_unreadable_file_is_empty_rather_than_raising(self):
        self.assertEqual(lang.read_file(os.path.join(self.folder, "nope.lang")), {})

    def test_a_broken_file_is_empty_rather_than_raising(self):
        self._write("bad.lang", "this is not an ini file at all\n")
        self.assertEqual(lang.read_file(os.path.join(self.folder, "bad", "bad.lang")), {})

    def test_unknown_sections_are_ignored(self):
        self._write("qq.lang", """
[meta]
name = Quirk

[not_a_real_section]
whatever = nothing
""")
        strings = lang.read_file(os.path.join(self.folder, "qq", "qq.lang"))
        self.assertIn(("meta", "name"), strings)
        self.assertNotIn(("not_a_real_section", "whatever"), strings)

    def test_keys_keep_their_case(self):
        self._write("cc.lang", """
[actions_announcements]
Draft_Saved = Kept.
""")
        strings = lang.read_file(os.path.join(self.folder, "cc", "cc.lang"))
        self.assertIn(("actions_announcements", "Draft_Saved"), strings)

    def test_a_percent_sign_is_left_alone(self):
        self._write("pp.lang", """
[actions_announcements]
done = 50% complete.
""")
        strings = lang.read_file(os.path.join(self.folder, "pp", "pp.lang"))
        self.assertEqual(strings[("actions_announcements", "done")], "50% complete.")

    def test_available_lists_every_file_by_its_own_name(self):
        found = dict(lang.available(self.folder))
        self.assertEqual(found["en"], "English")
        self.assertEqual(found["fr"], "Francais")

    def test_available_on_a_missing_folder_is_empty(self):
        self.assertEqual(lang.available(os.path.join(self.folder, "gone")), [])


class ShippedEnglishFile(unittest.TestCase):
    """The file that actually ships, data/lang/en/en.lang. A typo in
    it is a typo every translation inherits."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.path = os.path.join(root, "data", "lang", "en", "en.lang")

    def test_it_exists_and_parses(self):
        self.assertTrue(os.path.isfile(self.path), self.path)
        self.assertTrue(lang.read_file(self.path))

    def test_it_declares_itself_as_english(self):
        strings = lang.read_file(self.path)
        self.assertEqual(strings[("meta", "code")], "en")

    def test_every_section_it_uses_is_a_known_one(self):
        # read_file drops unknown sections silently, so this reads the
        # raw file instead of trusting it.
        with open(self.path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    self.assertIn(line[1:-1], lang.SECTIONS)


MENU_ENGLISH = """
[meta]
name = English
code = en

[menus]
bar_file = &File
bar_help = &Help
message_reply = Reply
message_forward = Forward
"""

MENU_FRENCH = """
[meta]
name = Francais
code = fr

[menus]
bar_file = Fichier
bar_help = &Aide
message_reply = Repondre\tCtrl+X
message_forward =
"""


class MenuLabels(unittest.TestCase):
    """lang.menu_label. The words change; the chord after the tab and
    the Alt letter are the code's and stay the code's, whatever a
    language file says."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="zbox_menu_")
        self._write("en.lang", MENU_ENGLISH)
        self._write("fr.lang", MENU_FRENCH)

    def tearDown(self):
        lang.load(self.folder, lang.DEFAULT_CODE)

    def _write(self, name, text):
        code = name[: -len(".lang")] if name.endswith(".lang") else name
        os.makedirs(os.path.join(self.folder, code), exist_ok=True)
        with open(os.path.join(self.folder, code, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_english_is_returned_exactly_as_it_came_in(self):
        lang.load(self.folder)
        self.assertEqual(
            lang.menu_label("message_reply", "Reply\tCtrl+R"), "Reply\tCtrl+R"
        )

    def test_a_translation_keeps_the_chord(self):
        lang.load(self.folder, "fr")
        label = lang.menu_label("message_reply", "Reply\tCtrl+R")
        self.assertTrue(label.endswith("\tCtrl+R"))
        self.assertEqual(label.count("\t"), 1)

    def test_a_translation_cannot_invent_a_chord(self):
        # The French value carries a tab and a key name of its own.
        lang.load(self.folder, "fr")
        self.assertNotIn("\tCtrl+X", lang.menu_label("message_reply", "Reply\tCtrl+R"))

    def test_the_alt_letter_comes_from_the_english(self):
        lang.load(self.folder, "fr")
        self.assertEqual(lang.menu_label("bar_file", "&File"), "Fichier (&F)")

    def test_a_translations_own_ampersand_is_dropped(self):
        lang.load(self.folder, "fr")
        self.assertEqual(lang.menu_label("bar_help", "&Help"), "Aide (&H)")

    def test_a_label_with_no_alt_letter_gains_none(self):
        lang.load(self.folder, "fr")
        self.assertNotIn("(&", lang.menu_label("message_reply", "Reply\tCtrl+R"))

    def test_an_untranslated_key_keeps_the_english_label(self):
        lang.load(self.folder, "fr")
        self.assertEqual(lang.menu_label("not_translated", "Archive"), "Archive")

    def test_an_empty_translation_keeps_the_english_label(self):
        lang.load(self.folder, "fr")
        self.assertEqual(
            lang.menu_label("message_forward", "Forward\tCtrl+L"), "Forward\tCtrl+L"
        )


class ShippedMenuKeys(unittest.TestCase):
    """A key the code asks for and the file does not carry is a line no
    translator ever sees. This is the guard that stops the two drifting
    apart."""

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.strings = lang.read_file(
            os.path.join(self.root, "data", "lang", "en", "en.lang")
        )

    def _keys_used(self):
        # Only keys written as a literal. The two call sites that build
        # their key from a command name are covered by their own test
        # below and by the compose table they read.
        pattern = re.compile(r'menu_label\(\s*"([A-Za-z0-9_]+)"\s*,')
        found = set()
        app_dir = os.path.join(self.root, "app")
        for name in sorted(os.listdir(app_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(app_dir, name), "r", encoding="utf-8") as handle:
                found.update(pattern.findall(handle.read()))
        return found

    def test_the_code_asks_for_menu_keys_at_all(self):
        self.assertTrue(len(self._keys_used()) > 50)

    def test_the_shipped_file_has_no_duplicate_keys(self):
        # A key written twice in one section makes configparser reject
        # the whole file, and every line in it falls back to English at
        # once. This says which key, rather than leaving 900 tests to
        # fail with an empty dictionary.
        path = os.path.join(self.root, "data", "lang", "en", "en.lang")
        section = None
        seen = set()
        duplicates = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    continue
                if "=" not in line:
                    continue
                key = (section, line.split("=", 1)[0].strip())
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
        self.assertEqual(duplicates, [])

    def test_the_shipped_file_actually_parses(self):
        self.assertTrue(len(self.strings) > 200)

    def test_every_key_the_code_uses_is_in_the_file(self):
        missing = sorted(
            key for key in self._keys_used() if ("menus", key) not in self.strings
        )
        self.assertEqual(missing, [])

    def test_every_formatting_command_has_a_key(self):
        import trix_page

        missing = [
            attribute
            for _label, attribute, _accelerator in trix_page.FORMAT_COMMANDS
            if ("menus", "format_" + attribute) not in self.strings
        ]
        self.assertEqual(missing, [])

    def test_the_tray_keys_are_there(self):
        for key in (
            "tray_open", "tray_unread_one", "tray_unread_many",
            "tray_no_unread", "tray_settings", "tray_quit",
        ):
            self.assertIn(("menus", key), self.strings)

    def test_no_menu_entry_carries_a_shortcut(self):
        # A chord lives in the code. A tab in this file would be a
        # language file quietly redefining one.
        carrying = [
            key for (section, key), value in self.strings.items()
            if section == "menus" and "\t" in value
        ]
        self.assertEqual(carrying, [])


BULK_ENGLISH = """
[meta]
name = English
code = en
"""

BULK_FRENCH = """
[meta]
name = Francais
code = fr

[actions_announcements]
bulk_failed_many = {done} {failed} sur {total} messages ont echoue.
"""


class SummarizeInAnotherLanguage(unittest.TestCase):
    """bulk_execution.summarize speaks the end of a bulk run. It is
    pure and wx-free and stays that way; only its words are looked
    up."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="zbox_bulk_")
        for code, text in (("en", BULK_ENGLISH), ("fr", BULK_FRENCH)):
            os.makedirs(os.path.join(self.folder, code), exist_ok=True)
            with open(
                os.path.join(self.folder, code, "%s.lang" % code), "w", encoding="utf-8"
            ) as handle:
                handle.write(text)

    def tearDown(self):
        lang.load(self.folder, lang.DEFAULT_CODE)

    def _partly_failed(self):
        from bulk_execution import BulkResult

        return BulkResult(completed=3, total=3, failures=[(object(), Exception())])

    def test_a_clean_run_is_the_callers_own_text(self):
        from bulk_execution import BulkResult, summarize

        lang.load(self.folder)
        result = BulkResult(completed=2, total=2)
        self.assertEqual(summarize(result, "Archived 2 messages."), "Archived 2 messages.")

    def test_english_is_unchanged(self):
        from bulk_execution import summarize

        lang.load(self.folder)
        self.assertEqual(
            summarize(self._partly_failed(), "Deleted 3 messages."),
            "Deleted 3 messages. 1 of 3 messages failed.",
        )

    def test_a_translation_is_used(self):
        from bulk_execution import summarize

        lang.load(self.folder, "fr")
        spoken = summarize(self._partly_failed(), "Supprime 3 messages.")
        self.assertIn("sur 3", spoken)
        self.assertTrue(spoken.startswith("Supprime 3 messages."))

    def test_an_untranslated_line_stays_english(self):
        from bulk_execution import BulkResult, summarize

        lang.load(self.folder, "fr")
        cancelled = BulkResult(completed=1, total=4, failures=[], cancelled=True)
        self.assertEqual(
            summarize(cancelled, "Deleted."),
            "Cancelled after 1 of 4. 1 succeeded, 0 failed.",
        )


class ShippedAnnouncementKeys(unittest.TestCase):
    """The same guard as the menus, for the announcements the sweep
    wired: an action ZBox can name has to have a line to name it
    with."""

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.strings = lang.read_file(
            os.path.join(self.root, "data", "lang", "en", "en.lang")
        )

    def test_every_action_key_the_code_uses_is_in_the_file(self):
        path = os.path.join(self.root, "app", "main_frame.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        keys = re.findall(r'"[A-Za-z ]+": "(action_[a-z_]+)",', source)
        self.assertTrue(len(keys) >= 9)
        missing = sorted(
            key for key in keys
            if ("actions_announcements", key) not in self.strings
        )
        self.assertEqual(missing, [])

    def test_the_bulk_lines_are_there(self):
        for key in (
            "undo_done_one", "undo_done_many",
            "junk_marked_one", "junk_marked_many", "junk_no_new_sender",
            "not_junk_marked_one", "not_junk_marked_many",
            "moved_to_one", "moved_to_many", "copied_to_many",
            "marked_read", "marked_unread",
            "bulk_cancelled", "bulk_failed_one", "bulk_failed_many",
        ):
            self.assertIn(("actions_announcements", key), self.strings)

    def test_both_thread_edges_are_whole_sentences(self):
        # The word that used to be slotted in -- First or Last -- is
        # why these are two keys and not one with a placeholder.
        for key, opening in (
            ("thread_edge_first", "First"), ("thread_edge_last", "Last"),
        ):
            value = self.strings[("actions_announcements", key)]
            self.assertTrue(value.startswith(opening))
            self.assertNotIn("{", value)
            self.assertIn("Press again", value)


CONTROL_ENGLISH = """
[meta]
name = English
code = en

[dialogs]
wiz_test_connection = Test &Connection
wiz_show_password = &Show password
acct_display_name = Display name
"""

CONTROL_FRENCH = """
[meta]
name = Francais
code = fr

[dialogs]
wiz_test_connection = Tester la connexion
wiz_show_password = &Afficher le mot de passe
acct_display_name = Nom affiche
"""


class ControlLabels(unittest.TestCase):
    """control_label: the same protection menu_label gives a menu,
    for a button or a checkbox. The ampersand is the Alt key for that
    control, so it is the code's and never the language file's."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="zbox_control_")
        for code, text in (("en", CONTROL_ENGLISH), ("fr", CONTROL_FRENCH)):
            os.makedirs(os.path.join(self.folder, code), exist_ok=True)
            with open(
                os.path.join(self.folder, code, "%s.lang" % code), "w", encoding="utf-8"
            ) as handle:
                handle.write(text)

    def tearDown(self):
        lang.load(self.folder, lang.DEFAULT_CODE)

    def test_english_is_returned_unchanged(self):
        lang.load(self.folder)
        self.assertEqual(
            lang.control_label("wiz_test_connection", "Test &Connection"),
            "Test &Connection",
        )

    def test_the_english_access_key_survives_translation(self):
        lang.load(self.folder, "fr")
        self.assertEqual(
            lang.control_label("wiz_test_connection", "Test &Connection"),
            "Tester la connexion (&C)",
        )

    def test_a_translations_own_ampersand_is_dropped(self):
        lang.load(self.folder, "fr")
        self.assertEqual(
            lang.control_label("wiz_show_password", "&Show password"),
            "Afficher le mot de passe (&S)",
        )

    def test_a_label_with_no_access_key_is_only_translated(self):
        lang.load(self.folder, "fr")
        self.assertEqual(
            lang.control_label("acct_display_name", "Display name"), "Nom affiche"
        )


class LanguageSetting(unittest.TestCase):
    """settings.language: which file lang.load is asked for at
    startup. Defaults to English, and a settings.json written before
    this setting existed loads as English rather than as nothing."""

    def test_defaults_to_english(self):
        from settings_manager import Settings

        self.assertEqual(Settings().language, "en")

    def test_round_trips(self):
        from settings_manager import Settings

        restored = Settings.from_dict(Settings(language="fr").to_dict())
        self.assertEqual(restored.language, "fr")

    def test_missing_key_loads_as_english(self):
        from settings_manager import Settings

        self.assertEqual(Settings.from_dict({}).language, "en")

    def test_blank_falls_back_to_english(self):
        from settings_manager import Settings

        self.assertEqual(Settings(language="   ").language, "en")
        self.assertEqual(Settings(language=None).language, "en")


class MatchSystemLanguage(unittest.TestCase):
    """lang.match_system_language: which folder the first-run language
    page preselects for the Windows display language."""

    CODES = ["ar", "de", "en", "es", "fa", "fr", "pt-br", "pt-pt", "ru", "tr", "ur"]

    def _match(self, name, codes=None):
        return lang.match_system_language(self.CODES if codes is None else codes, name)

    def test_an_exact_regional_match_wins(self):
        self.assertEqual(self._match("pt-BR"), "pt-br")
        self.assertEqual(self._match("pt-PT"), "pt-pt")

    def test_a_region_falls_back_to_the_bare_language(self):
        self.assertEqual(self._match("de-AT"), "de")
        self.assertEqual(self._match("ar-SA"), "ar")
        self.assertEqual(self._match("fa-IR"), "fa")

    def test_a_region_with_no_folder_gets_the_same_language(self):
        self.assertEqual(self._match("pt-AO"), "pt-br")

    def test_an_underscore_and_case_are_ignored(self):
        self.assertEqual(self._match("PT_br"), "pt-br")
        self.assertEqual(self._match("TR_tr"), "tr")

    def test_a_language_not_installed_is_english(self):
        self.assertEqual(self._match("ja-JP"), "en")

    def test_nothing_known_is_english(self):
        self.assertEqual(self._match(""), "en")
        self.assertEqual(self._match(None), "en")
        self.assertEqual(self._match("fr-FR", codes=[]), "en")

    def test_system_locale_name_never_raises(self):
        self.assertIsInstance(lang.system_locale_name(), str)


class IsRtl(unittest.TestCase):
    """lang.is_rtl(): the [meta] direction key, absent or anything but
    "rtl" meaning left to right."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="zbox_rtl_")

    def tearDown(self):
        lang.load(self.folder, lang.DEFAULT_CODE)

    def _write(self, name, text):
        code = name[: -len(".lang")] if name.endswith(".lang") else name
        os.makedirs(os.path.join(self.folder, code), exist_ok=True)
        with open(os.path.join(self.folder, code, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_english_with_no_direction_key_is_not_rtl(self):
        self._write("en.lang", "[meta]\nname = English\ncode = en\n")
        lang.load(self.folder)
        self.assertFalse(lang.is_rtl())

    def test_direction_rtl_is_rtl(self):
        self._write("en.lang", "[meta]\nname = English\ncode = en\n")
        self._write(
            "ar.lang",
            "[meta]\nname = Arabic\ncode = ar\ndirection = rtl\n",
        )
        lang.load(self.folder, "ar")
        self.assertTrue(lang.is_rtl())

    def test_direction_ltr_is_not_rtl(self):
        self._write("en.lang", "[meta]\nname = English\ncode = en\n")
        self._write(
            "xx.lang",
            "[meta]\nname = Explicit\ncode = xx\ndirection = ltr\n",
        )
        lang.load(self.folder, "xx")
        self.assertFalse(lang.is_rtl())

    def test_direction_is_case_insensitive(self):
        self._write("en.lang", "[meta]\nname = English\ncode = en\n")
        self._write(
            "xx.lang",
            "[meta]\nname = Loud\ncode = xx\ndirection = RTL\n",
        )
        lang.load(self.folder, "xx")
        self.assertTrue(lang.is_rtl())

    def test_switching_back_to_english_clears_it(self):
        self._write("en.lang", "[meta]\nname = English\ncode = en\n")
        self._write(
            "ar.lang",
            "[meta]\nname = Arabic\ncode = ar\ndirection = rtl\n",
        )
        lang.load(self.folder, "ar")
        self.assertTrue(lang.is_rtl())
        lang.load(self.folder, "en")
        self.assertFalse(lang.is_rtl())


if __name__ == "__main__":
    unittest.main()
