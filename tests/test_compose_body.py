"""
compose_body: the parts that can be proved without a display.

Not the engines themselves -- one of them is a WebView and the other
is a wx.TextCtrl, and neither exists without a running app. What is
worth pinning here is the part that breaks silently in a build: where
the vendored Trix is found, and whether it is actually on disk. A
missing asset does not raise; it quietly falls back to the markup
engine, and a fallback nobody notices is how a feature disappears.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import body_html
import compose_body
import trix_page


class VendorTests(unittest.TestCase):
    def test_trix_is_on_disk(self):
        """If this fails the composer silently drops to the markup
        engine on every machine, which is the whole reason it is a
        test rather than a comment."""
        folder = compose_body.vendor_dir()
        self.assertIsNotNone(folder)
        for name in ("trix.umd.min.js", "trix.css", "LICENSE.txt"):
            self.assertTrue(os.path.isfile(os.path.join(folder, name)), name)

    def test_assets_are_readable(self):
        script = compose_body.read_vendor_asset("trix.umd.min.js")
        styles = compose_body.read_vendor_asset("trix.css")
        self.assertIn("trix", script.lower())
        self.assertIn("trix", styles.lower())

    def test_a_missing_asset_is_empty_rather_than_an_error(self):
        self.assertEqual(compose_body.read_vendor_asset("not-here.js"), "")

    def test_the_page_assembles_from_what_is_on_disk(self):
        page = trix_page.document_html(
            compose_body.read_vendor_asset("trix.css"),
            compose_body.read_vendor_asset("trix.umd.min.js"),
        )
        self.assertIn("<trix-editor", page)
        self.assertIn("window.zbox_chords", page)
        self.assertGreater(len(page), 200000)


class MarkupFallbackTests(unittest.TestCase):
    """The fallback's formatting is limited by what body_html will let
    through, so the two have to agree on which tags exist."""

    def test_every_markup_tag_is_allowed_by_the_converter(self):
        for tag in compose_body.MARKUP_TAGS.values():
            self.assertIn(tag, body_html.ALLOWED)
        for tag in compose_body.MARKUP_LISTS.values():
            self.assertIn(tag, body_html.ALLOWED)

    def test_every_markup_attribute_has_a_spoken_label(self):
        for attribute in list(compose_body.MARKUP_TAGS) + list(
            compose_body.MARKUP_LISTS
        ):
            self.assertIn(attribute, trix_page.ATTRIBUTE_LABELS)

    def test_unsupported_formatting_is_named_rather_than_ignored(self):
        """Strikethrough and code have no tag the markup engine would
        keep. The writer has to be told that, not left pressing a
        chord that does nothing."""
        for attribute in ("strike", "code"):
            self.assertNotIn(attribute, compose_body.MARKUP_TAGS)
            self.assertIn(attribute, trix_page.ATTRIBUTE_LABELS)


if __name__ == "__main__":
    unittest.main()
