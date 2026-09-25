"""
body_html: the conversions a compose body goes through on its way to
a message. Every case here came from the composer work in session y;
the markup cases in particular guard a real bug, where escaping the
whole body first and putting tags back with a regex meant an
attribute could never match, so every link a writer typed was
silently dropped while a javascript one was, by luck, dropped too.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import body_html


class MarkupToHtml(unittest.TestCase):
    def test_an_allowed_tag_is_kept(self):
        self.assertIn("<b>", body_html.markup_to_html("a <b>bold</b> word"))

    def test_anything_else_is_shown_as_text(self):
        out = body_html.markup_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_a_real_link_keeps_its_address(self):
        out = body_html.markup_to_html('<a href="https://example.com">site</a>')
        self.assertIn('href="https://example.com"', out)

    def test_a_javascript_link_loses_its_address(self):
        out = body_html.markup_to_html('<a href="javascript:alert(1)">x</a>')
        self.assertNotIn("javascript", out)
        self.assertIn("<a>", out)

    def test_single_quoted_attributes_are_read_too(self):
        out = body_html.markup_to_html("<a href='https://example.com'>site</a>")
        self.assertIn('href="https://example.com"', out)

    def test_an_unknown_attribute_is_dropped(self):
        out = body_html.markup_to_html('<b onclick="x()">hi</b>')
        self.assertNotIn("onclick", out)

    def test_newlines_become_breaks(self):
        self.assertIn("<br>", body_html.markup_to_html("one\ntwo"))


class TextAndHtml(unittest.TestCase):
    def test_blank_lines_split_paragraphs(self):
        out = body_html.text_to_html("one\n\ntwo")
        self.assertEqual(out.count("<p>"), 2)

    def test_html_to_text_keeps_the_words(self):
        text = body_html.html_to_text("<p>a <b>bold</b> word</p>")
        self.assertIn("bold", text)
        self.assertNotIn("<", text)

    def test_list_items_become_dashes(self):
        text = body_html.html_to_text("<ul><li>one</li><li>two</li></ul>")
        self.assertIn("- one", text)

    def test_plain_text_is_not_formatting(self):
        self.assertFalse(body_html.has_formatting(body_html.text_to_html("just words")))

    def test_bold_is_formatting(self):
        self.assertTrue(body_html.has_formatting("<b>x</b>"))


class WrapLines(unittest.TestCase):
    def test_zero_leaves_the_text_alone(self):
        line = "x" * 200
        self.assertEqual(body_html.wrap_lines(line, 0), line)

    def test_a_long_line_is_broken(self):
        wrapped = body_html.wrap_lines("word " * 40, 72)
        self.assertTrue(all(len(l) <= 72 for l in wrapped.split("\n")))

    def test_a_quoted_line_is_never_rewrapped(self):
        quoted = "> " + "x" * 200
        self.assertEqual(body_html.wrap_lines(quoted, 72), quoted)

    def test_the_signature_delimiter_is_never_rewrapped(self):
        self.assertEqual(body_html.wrap_lines("-- ", 72), "-- ")


if __name__ == "__main__":
    unittest.main()
