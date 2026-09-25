"""
Audit finding 51: printing a message only ever sent the plain-text
body -- no From/To/Cc/Date/Subject, and no way to preview page breaks
before committing to paper. This covers the two pure/mockable seams:
MessageViewPanel.printable_text() now prepending the same header
block the on-screen header area shows (envelope_format.
_message_header_block), and print_preview() building wx.PrintPreview
from two SEPARATE _PlainTextPrintout instances rather than one
(reusing a single Printout for both the preview render and the actual
OS print job is a documented wx footgun). The print/preview dialogs
themselves are real wx plumbing tests/wxstub can't meaningfully
exercise -- same accepted gap as print_text's own lack of a test
before this finding.
"""

import os
import sys
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import message_view_panel
from message_view_panel import MessageViewPanel, print_preview


def _make_panel_stub(envelope, message):
    stub = mock.Mock(spec=MessageViewPanel)
    stub.envelope = envelope
    stub.message = message
    return stub


class PrintableTextIncludesHeaders(unittest.TestCase):
    def test_headers_precede_body_with_blank_line_between(self):
        envelope = {
            "from": [{"name": "Ada Lovelace", "email": "ada@example.com"}],
            "to": [{"name": "Bob", "email": "bob@example.com"}],
            "date": "Sat, 05 Sep 2026 10:00:00 +0100",
            "subject": "Engine progress",
        }
        stub = _make_panel_stub(envelope, {"id": "1"})
        with mock.patch.object(
            message_view_panel, "extract_message_body", return_value="Body text."
        ):
            text = MessageViewPanel.printable_text(stub)

        self.assertTrue(text.startswith("From: Ada Lovelace <ada@example.com>\n"))
        self.assertIn("To: Bob <bob@example.com>\n", text)
        self.assertIn("Subject: Engine progress", text)
        self.assertTrue(text.endswith("\n\nBody text."))

    def test_falls_back_to_no_body_placeholder(self):
        stub = _make_panel_stub({"subject": "Empty"}, {"id": "2"})
        with mock.patch.object(
            message_view_panel, "extract_message_body", return_value=""
        ):
            text = MessageViewPanel.printable_text(stub)
        self.assertTrue(text.endswith("(no message body)"))
        self.assertIn("Subject: Empty", text)

    def test_headers_included_regardless_of_show_headers_setting(self):
        # printable_text has no settings_manager lookup at all -- a
        # printout always carries headers, unlike the on-screen
        # header block above the body, which is gated by Settings'
        # "Show message headers above an open message" checkbox. This
        # test exists so that gate never accidentally gets copied
        # into printable_text in a future edit.
        envelope = {"from": [{"email": "solo@example.com"}], "subject": "S"}
        stub = _make_panel_stub(envelope, {"id": "3"})
        with mock.patch.object(
            message_view_panel, "extract_message_body", return_value="B"
        ):
            text = MessageViewPanel.printable_text(stub)
        self.assertIn("From: solo@example.com", text)


class PrintPreviewUsesTwoSeparatePrintouts(unittest.TestCase):
    """wx.PrintPreview's own docs require two distinct Printout
    instances (one consumed by the preview render, one handed to the
    real print job); passing the same object twice is a classic wx
    bug that silently breaks the second use. This pins that contract
    down independent of wxstub's permissive Stub class actually
    catching the mistake."""

    def test_preview_and_printer_printouts_are_distinct_objects(self):
        captured = {}

        def fake_print_preview(preview_printout, printer_printout):
            captured["preview"] = preview_printout
            captured["printer"] = printer_printout
            result = mock.Mock()
            result.IsOk.return_value = True
            return result

        with mock.patch.object(
            message_view_panel.wx, "PrintPreview", side_effect=fake_print_preview
        ), mock.patch.object(
            message_view_panel.wx, "PreviewFrame"
        ) as preview_frame_cls:
            preview_frame_cls.return_value = mock.Mock()
            print_preview(None, "Hello", "A subject")

        self.assertIn("preview", captured)
        self.assertIsNot(captured["preview"], captured["printer"])

    def test_not_ok_preview_shows_error_and_does_not_open_frame(self):
        def fake_print_preview(preview_printout, printer_printout):
            result = mock.Mock()
            result.IsOk.return_value = False
            return result

        with mock.patch.object(
            message_view_panel.wx, "PrintPreview", side_effect=fake_print_preview
        ), mock.patch.object(
            message_view_panel.wx, "PreviewFrame"
        ) as preview_frame_cls, mock.patch.object(
            message_view_panel.wx, "MessageBox"
        ) as message_box:
            print_preview(None, "Hello", "A subject")

        message_box.assert_called_once()
        preview_frame_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
