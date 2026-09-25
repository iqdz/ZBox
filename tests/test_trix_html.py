"""
trix_html: what leaves the composer when the body is a Trix editor.

The samples are real Trix output shapes -- div for lines, class
attributes on quotes and code, a figure for an attached file -- not
invented ones. The emptiness cases exist because Trix leaves a div
holding a single line break behind once everything is deleted, which
an earlier version of this code counted as a message.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import trix_html

SAMPLE = (
    "<div>Thanks for the update, the figures look <strong>right</strong> "
    "to me.</div><div><br></div>"
    "<ul><li>First point</li><li>Second, with a "
    '<a href="https://example.com">link</a></li></ul>'
    '<blockquote class="attachment__caption"><div>The quoted line.</div></blockquote>'
    "<pre>code sample</pre>"
    '<figure class="attachment attachment--file" data-trix-attachment="{}">'
    "<figcaption>notes.txt</figcaption></figure>"
)


class Conversion(unittest.TestCase):
    def setUp(self):
        self.html = trix_html.to_email_html(SAMPLE)

    def test_a_div_line_becomes_a_paragraph(self):
        self.assertIn("<p", self.html)

    def test_no_class_attributes_survive(self):
        self.assertNotIn("class=", self.html)

    def test_no_trix_data_attributes_survive(self):
        self.assertNotIn("data-trix", self.html)

    def test_an_attached_figure_is_removed_whole(self):
        self.assertNotIn("figcaption", self.html)
        self.assertNotIn("notes.txt", self.html)

    def test_bold_is_kept(self):
        self.assertIn("<strong>", self.html)

    def test_a_list_is_kept(self):
        self.assertIn("<ul", self.html)
        self.assertIn("<li", self.html)

    def test_a_link_keeps_its_address(self):
        self.assertIn('href="https://example.com"', self.html)

    def test_a_quote_carries_an_inline_style(self):
        self.assertIn("blockquote style=", self.html)

    def test_code_carries_an_inline_style(self):
        self.assertIn("pre style=", self.html)

    def test_the_document_sets_a_font(self):
        self.assertIn("font-family", self.html.split(">")[0])


class UnsafeInput(unittest.TestCase):
    def setUp(self):
        self.html = trix_html.to_email_html(
            "<div>ok</div><script>alert(1)</script>"
            '<div><a href="javascript:alert(1)">x</a></div>'
        )

    def test_a_script_element_is_dropped(self):
        self.assertNotIn("script", self.html)

    def test_a_javascript_link_is_dropped(self):
        self.assertNotIn("javascript", self.html)

    def test_the_real_text_survives(self):
        self.assertIn("ok", self.html)


class Emptiness(unittest.TestCase):
    def test_an_empty_editor_gives_an_empty_string(self):
        self.assertEqual(trix_html.to_email_html("<div><br></div>"), "")

    def test_an_empty_editor_is_not_content(self):
        self.assertFalse(trix_html.has_content("<div><br></div>"))

    def test_typed_text_is_content(self):
        self.assertTrue(trix_html.has_content("<div>hello</div>"))

    def test_an_image_alone_is_content(self):
        self.assertTrue(trix_html.has_content('<div><img src="cid:one@zbox"></div>'))

    def test_nothing_at_all_is_not_content(self):
        self.assertFalse(trix_html.has_content(""))


class PlainText(unittest.TestCase):
    def test_the_words_come_through(self):
        self.assertIn("figures look", trix_html.plain_text(SAMPLE))

    def test_an_attached_figure_contributes_nothing(self):
        self.assertNotIn("notes.txt", trix_html.plain_text(SAMPLE))


if __name__ == "__main__":
    unittest.main()
