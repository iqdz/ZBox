"""
The two findings about ZBox leaking things it should not: message
bodies into the debug log, and sender-controlled filenames into a
save path.
"""

import json
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
# The wx stand-in first: message_view_panel imports wx, and these
# tests must run on a machine with no wxPython, like the rest.
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import himalaya_client as hc


class LogRedaction(unittest.TestCase):
    """logs/ is an ordinary folder that any backup or sync client
    picks up. The log's value is the shape, the ids and the timings;
    it was never the mail."""

    def _read_response(self, body="Dear Harith, the password is hunter2."):
        return json.dumps({
            "id": "42",
            "flags": [{"iana": "seen", "raw": "\\Seen"}],
            "text_body": [0],
            "parts": [{
                "headers": [{"name": "subject", "value": {"Text": "Quarterly report"}}],
                "body": {"Text": body},
            }],
        })

    def test_body_text_never_reaches_the_log(self):
        preview = hc._response_preview(self._read_response())
        self.assertNotIn("hunter2", preview)
        self.assertNotIn("Dear Harith", preview)

    def test_structure_survives_so_the_log_is_still_useful(self):
        preview = hc._response_preview(self._read_response())
        for keeper in ("text_body", "parts", "headers", '"id"', "seen"):
            self.assertIn(keeper, preview)

    def test_the_subject_header_is_redacted_too(self):
        # A message read carries the subject in a header rather than a
        # plain field. Redacting only the plain field would mean a
        # read leaks what a listing no longer does.
        preview = hc._response_preview(self._read_response())
        self.assertNotIn("Quarterly report", preview)

    def test_redaction_says_what_it_removed(self):
        preview = hc._response_preview(self._read_response("x" * 500))
        self.assertIn("sensitive field(s) redacted", preview)

    def test_a_body_is_logged_as_a_text_block_with_no_length(self):
        # A length is a fingerprint: it is enough to tell one message
        # from another, and to confirm a guess about which one it was.
        preview = hc._response_preview(self._read_response("x" * 500))
        self.assertIn("<text block>", preview)
        self.assertNotIn("500", preview)
        self.assertIn("sensitive field(s) redacted", preview)

    def test_subjects_are_numbered_so_lines_can_still_be_matched_up(self):
        payload = json.dumps([
            {"id": "1", "subject": "First one"},
            {"id": "2", "subject": "Second one"},
        ])
        preview = hc._response_preview(payload)
        self.assertIn("<subject 1>", preview)
        self.assertIn("<subject 2>", preview)
        self.assertNotIn("First one", preview)
        self.assertNotIn("Second one", preview)

    def test_attachment_filenames_never_reach_the_log(self):
        # Sender-supplied text, and routinely a person's name, a case
        # number or an invoice number. The content type stays, so an
        # attachment, an embedded image and a tracking pixel are still
        # told apart in the log.
        payload = json.dumps({"parts": [{
            "content_type": "application/pdf",
            "filename": "Medical results Jane Doe.pdf",
            "size": 20481,
        }]})
        preview = hc._response_preview(payload)
        self.assertNotIn("Medical", preview)
        self.assertNotIn("Jane Doe", preview)
        self.assertIn("<attachment 1>", preview)
        self.assertIn("application/pdf", preview)

    def test_html_bodies_are_redacted_too(self):
        payload = json.dumps({"parts": [{"body": {"Html": "<p>secret</p>"}}]})
        self.assertNotIn("secret", hc._response_preview(payload))

    def test_unparseable_output_is_not_logged_raw(self):
        # Unparseable output is exactly when a body might be sitting
        # in it unrecognised, so it is described, not printed.
        preview = hc._response_preview("Dear Harith, this is not JSON")
        self.assertNotIn("Dear Harith", preview)
        self.assertIn("unparseable", preview)

    def test_envelope_listings_are_redacted(self):
        """The gap the first version of this shipped with. A folder
        listing has no body at all, so redacting only bodies left
        every subject, sender and address of a whole folder in the
        log in clear text."""
        payload = json.dumps([{
            "id": "38759",
            "message-id": "27cb9c38@example.com",
            "subject": "Exclusive CODE for our store fans",
            "from": [{"name": "Sam Recipient", "email": "recipient@example.net"}],
            "to": [{"name": "Sam Recipient", "email": "account@example.com"}],
            "flags": [{"raw": "\\Seen", "iana": "seen"}],
            "date": "2026-09-04T03:01:25+01:00",
            "size": 150706,
        }])
        preview = hc._response_preview(payload)
        for leaked in ("Exclusive CODE", "Sam Recipient", "recipient",
                       "account@example.com", "27cb9c38"):
            self.assertNotIn(leaked, preview)

    def test_what_a_listing_keeps_is_what_diagnoses_a_bug(self):
        payload = json.dumps([{
            "id": "38759",
            "subject": "anything",
            "from": [{"name": "Someone", "email": "someone@gmail.com"}],
            "flags": [{"raw": "\\Seen", "iana": "seen"}],
            "date": "2026-09-04T03:01:25+01:00",
            "size": 150706,
        }])
        preview = hc._response_preview(payload)
        for keeper in ("38759", "seen", "2026-09-04", "150706", "gmail.com"):
            self.assertIn(keeper, preview, "%s is diagnostic, not private" % keeper)

    def test_addresses_keep_their_domain_and_lose_the_person(self):
        self.assertEqual(hc._mask_address("sender@example.com"), "***@example.com")
        # Google Voice message-ids embed a phone number.
        self.assertNotIn(
            "15555550123",
            hc._mask_address("+15555550123.1779908@txt.voice.google.com"),
        )
        self.assertEqual(hc._mask_address("no-at-sign"), "<redacted>")


class TrashFolderComparison(unittest.TestCase):
    """Delete checks whether the message is already in Trash before
    moving it there. IMAP does not guarantee canonical casing, and the
    two names come from different lookups."""

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(hc._same_folder("Trash", "trash"))
        self.assertTrue(hc._same_folder("[Gmail]/Trash", "[gmail]/trash"))
        self.assertTrue(hc._same_folder(" Trash ", "Trash"))

    def test_different_folders_are_different(self):
        self.assertFalse(hc._same_folder("INBOX", "[Gmail]/Trash"))
        self.assertFalse(hc._same_folder("Trash", "[Gmail]/Trash"))
        self.assertFalse(hc._same_folder(None, "Trash"))


class AttachmentFilenames(unittest.TestCase):
    """The filename comes from the message, which means it comes from
    whoever sent it."""

    def setUp(self):
        from message_view_panel import _clean_filename
        self.clean = _clean_filename

    def test_ordinary_names_are_left_alone(self):
        self.assertEqual(self.clean("report.pdf"), "report.pdf")
        self.assertEqual(self.clean('"quoted name.docx"'), "quoted name.docx")

    def test_traversal_is_reduced_to_a_name(self):
        self.assertEqual(self.clean(r"..\..\Startup\evil.exe"), "evil.exe")
        self.assertEqual(self.clean("../../etc/passwd"), "passwd")

    def test_drive_letters_and_unc_paths_lose_their_root(self):
        self.assertEqual(self.clean(r"C:evil.txt"), "evil.txt")
        self.assertEqual(self.clean(r"C:\Windows\evil.txt"), "evil.txt")
        self.assertEqual(self.clean(r"\\server\share\evil.txt"), "evil.txt")

    def test_dot_only_names_address_a_directory_not_a_file(self):
        self.assertIsNone(self.clean(".."))
        self.assertIsNone(self.clean("."))
        self.assertIsNone(self.clean("   "))

    def test_windows_reserved_device_names_are_defused(self):
        self.assertEqual(self.clean("CON"), "_CON")
        self.assertEqual(self.clean("lpt1.txt"), "_lpt1.txt")

    def test_control_characters_and_wildcards_go(self):
        self.assertEqual(self.clean("bad\x00name.txt"), "bad_name.txt")
        self.assertEqual(self.clean("a<b>c.txt"), "a_b_c.txt")


if __name__ == "__main__":
    unittest.main()
