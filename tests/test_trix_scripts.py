"""
The compose body's load and insert scripts, and the first body a
reply opens with.

The scripts used to take their content at a bare HTML placeholder,
filled in with str.replace, which also rewrote the HTML inside
loadHTML and insertHTML. Every reply then opened with its quoted
original missing (the log's "set_html ok=False", 25 September 2026).
"""

import json
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import trix_page
from compose_panel import _initial_body_html


def _filled(template, html):
    return template.replace(trix_page.HTML_ARG, json.dumps(html))


class LoadAndInsertScripts(unittest.TestCase):
    def test_set_html_keeps_load_html_and_takes_the_content_once(self):
        content = "<p>HTML &gt; quoted</p>"
        script = _filled(trix_page.SET_HTML, content)
        self.assertIn("el.editor.loadHTML(" + json.dumps(content) + ")", script)
        self.assertEqual(script.count(json.dumps(content)), 1)
        self.assertNotIn(trix_page.HTML_ARG, script)

    def test_insert_html_keeps_insert_html_and_takes_the_content_once(self):
        content = "<blockquote>HTML</blockquote>"
        script = _filled(trix_page.INSERT_HTML, content)
        self.assertIn("el.editor.insertHTML(" + json.dumps(content) + ")", script)
        self.assertEqual(script.count(json.dumps(content)), 1)
        self.assertNotIn(trix_page.HTML_ARG, script)


class ReplyOpensWithAnEmptyLineOnTop(unittest.TestCase):
    def test_a_reply_body_starts_with_an_empty_line_then_the_quote(self):
        html = _initial_body_html("\n\n\nSomeone wrote:\n> first line\n", "")
        self.assertTrue(html.startswith("<div><br></div>"))
        self.assertIn("Someone wrote:", html)
        self.assertIn("&gt; first line", html)

    def test_a_body_that_is_not_a_reply_is_left_as_given(self):
        html = _initial_body_html("Hello there", "")
        self.assertFalse(html.startswith("<div><br></div>"))

    def test_a_new_message_is_unchanged(self):
        self.assertEqual(_initial_body_html("", ""), "")


if __name__ == "__main__":
    unittest.main()
