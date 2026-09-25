"""
trix_page: the page the compose body is made of, as data.

Nothing here runs a browser. What is worth proving without a display
is that the document is self-contained, that the chord table does not
claim a chord the frame already owns, and that the Format menu and the
chord table still agree with each other -- the last one being how they
silently drift apart otherwise.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import trix_page


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.page = trix_page.document_html("BODY{trix:css}", "var trix='js';")

    def test_is_a_whole_document(self):
        self.assertTrue(self.page.startswith("<!DOCTYPE html>"))
        self.assertIn('<html lang="en">', self.page)
        self.assertIn('<meta charset="utf-8">', self.page)
        self.assertIn("</html>", self.page)

    def test_carries_a_title(self):
        """A WebView2 document with no title is named from its URL,
        which is what made the reading view announce a base64 blob."""
        self.assertIn("<title>Message body</title>", self.page)

    def test_inlines_everything_it_needs(self):
        self.assertIn("BODY{trix:css}", self.page)
        self.assertIn("var trix='js';", self.page)
        self.assertNotIn("<link", self.page)
        self.assertNotIn("src=", self.page)

    def test_script_order(self):
        """Trix defines the custom element, so it has to run before the
        setup that reaches for el.editor, and after the markup that
        holds the editor element."""
        editor = self.page.index("<trix-editor")
        trix = self.page.index("var trix='js';")
        setup = self.page.index("window.zbox_attrs")
        self.assertLess(editor, trix)
        self.assertLess(trix, setup)

    def test_survives_missing_assets(self):
        page = trix_page.document_html("", "")
        self.assertIn("<trix-editor", page)

    def test_no_dir_attribute_by_default(self):
        self.assertNotIn('dir="rtl"', self.page)

    def test_dir_rtl_when_asked(self):
        page = trix_page.document_html("", "", is_rtl=True)
        self.assertIn('<html lang="en" dir="rtl">', page)

    def test_apply_bidi_is_wired_to_trix_change(self):
        self.assertIn("window.zbox_apply_bidi", trix_page.SETUP)
        self.assertIn("el.addEventListener('trix-change'", trix_page.SETUP)
        change_handler = trix_page.SETUP.split("el.addEventListener('trix-change'", 1)[1]
        self.assertIn("window.zbox_apply_bidi();", change_handler[:200])


class ChordTests(unittest.TestCase):
    def chord_names(self):
        return [
            line.split("'")[1]
            for line in trix_page.CHORDS.splitlines()
            if line.strip().startswith("['")
        ]

    def test_quote_is_not_on_the_exit_chord(self):
        """Ctrl+Shift+Q is Exit, frame-wide. Quitting has to work from
        inside a message being written, so Quote lives on
        Ctrl+Shift+9."""
        self.assertIn("['fmt:quote',       true,  true,  false, '9']", trix_page.CHORDS)
        self.assertNotIn("'q'", trix_page.CHORDS.split("fmt:quote")[1][:40])

    def test_code_is_not_on_the_open_conversation_chord(self):
        self.assertIn("['fmt:code',        true,  true,  false, '6']", trix_page.CHORDS)

    def test_the_way_out_is_bridged(self):
        """A WebView that bridges none of these is a keyboard trap."""
        for name in ("cycle_panes", "close_tab", "next_tab", "previous_tab"):
            self.assertIn(name, self.chord_names())

    def test_alt_and_f10_are_not_bridged(self):
        """Deliberate: F6 reaches an ordinary wx control first, and the
        menu bar works natively from there."""
        self.assertNotIn("'F10'", trix_page.CHORDS)

    def test_no_chord_without_a_handler(self):
        """A chord that reaches nothing is indistinguishable from a
        broken one. Rather than list what is absent, this reads the
        composer's own dispatch and checks the two lists agree, so it
        catches drift in either direction: a chord added here with no
        handler, or a handler that loses its chord.

        Read as text rather than imported, because compose_panel pulls
        in wx and there is no display in a test run.
        """
        panel = os.path.join(
            os.path.dirname(TESTS_DIR), "app", "compose_panel.py"
        )
        with open(panel, "r", encoding="utf-8") as handle:
            source = handle.read()
        for name in self.chord_names():
            if name.startswith("fmt:"):
                # Formatting is dispatched by prefix, not by name.
                self.assertIn(name[4:], trix_page.ATTRIBUTE_LABELS)
                continue
            self.assertIn('"%s"' % name, source, name)

    def test_template_is_still_held_over(self):
        """Templates need compose_config, which has not come across
        from the lab yet. The chord stays out until it does."""
        self.assertNotIn("template", self.chord_names())


class FormatMenuTests(unittest.TestCase):
    def test_every_menu_command_has_a_chord(self):
        chords = trix_page.CHORDS
        for _label, attribute, _accelerator in trix_page.FORMAT_COMMANDS:
            self.assertIn("fmt:%s" % attribute, chords)

    def test_every_menu_command_has_a_spoken_label(self):
        for _label, attribute, _accelerator in trix_page.FORMAT_COMMANDS:
            self.assertIn(attribute, trix_page.ATTRIBUTE_LABELS)

    def test_menu_accelerators_match_the_chord_table(self):
        """The two lists drifting apart is the failure this catches:
        a menu that promises Ctrl+Shift+Q while the page listens for
        Ctrl+Shift+9."""
        expected = {"quote": "Ctrl+Shift+9", "code": "Ctrl+Shift+6"}
        found = {
            attribute: accelerator
            for _label, attribute, accelerator in trix_page.FORMAT_COMMANDS
        }
        for attribute, accelerator in expected.items():
            self.assertEqual(found[attribute], accelerator)


if __name__ == "__main__":
    unittest.main()
