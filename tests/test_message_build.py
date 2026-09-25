"""
message_build: what a recipient actually receives. These check the
structure of the message rather than the code that made it -- single
part when nothing is formatted, an alternative when something is, an
inline image inside the HTML part's own container rather than beside
it as a stray attachment.

The address cases exist because a sender fixing a typo should be told
about all of them at once, which is the behaviour invalid_addresses
guarantees and the reason it returns a list rather than the first bad
one.
"""

import base64
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import message_build

# A one pixel PNG, so the inline image cases run on real image bytes
# rather than on something that only looks like a file.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def spec(**overrides):
    base = {
        "from_name": "Harith",
        "from_email": "harith@example.org",
        "to": "amina@example.com",
        "subject": "Subject",
        "text_body": "Hello.",
        "html_body": None,
        "priority": "normal",
        "format_flowed": False,
    }
    base.update(overrides)
    return base


class Addresses(unittest.TestCase):
    def test_a_good_address_passes(self):
        self.assertEqual(message_build.invalid_addresses("a@b.com"), [])

    def test_every_bad_address_is_reported_not_just_the_first(self):
        self.assertEqual(len(message_build.invalid_addresses("bad, worse, a@b.com")), 2)

    def test_duplicates_collapse_case_insensitively(self):
        self.assertEqual(message_build.dedupe_addresses("A@b.com, a@B.com"), "A@b.com")

    def test_a_display_name_survives_deduping(self):
        self.assertIn("Amina", message_build.dedupe_addresses("Amina <a@b.com>"))

    def test_recipients_are_counted_across_every_field(self):
        self.assertEqual(message_build.recipient_count("a@b.com, c@d.com", "e@f.com"), 3)

    def test_an_attachment_mention_is_noticed(self):
        self.assertTrue(message_build.mentions_attachment("see the attached notes"))

    def test_an_unrelated_body_is_not(self):
        self.assertFalse(message_build.mentions_attachment("see you Tuesday"))


class PlainMessage(unittest.TestCase):
    def setUp(self):
        self.message = message_build.build_message(spec(format_flowed=True))

    def test_it_is_a_single_part(self):
        self.assertFalse(self.message.is_multipart())

    def test_it_is_text_plain(self):
        self.assertEqual(self.message.get_content_type(), "text/plain")

    def test_it_is_marked_flowed(self):
        self.assertEqual(self.message.get_param("format"), "flowed")

    def test_a_normal_priority_adds_no_header(self):
        self.assertIsNone(self.message.get("X-Priority"))

    def test_it_has_a_message_id(self):
        self.assertTrue(self.message.get("Message-ID"))


class FormattedMessage(unittest.TestCase):
    def setUp(self):
        self.message = message_build.build_message(spec(
            html_body="<p>A <b>formatted</b> message.</p>",
            text_body="A formatted message.",
            attachments=[("notes.txt", b"notes", "text", "plain")],
            inline_images=[("sig0@zbox", "logo.png", PNG, "image", "png")],
            priority="high",
            read_receipt=True,
            dsn=True,
            sent_copy="folder:Archive",
        ))

    def test_it_is_multipart(self):
        self.assertTrue(self.message.is_multipart())

    def test_it_has_both_alternatives(self):
        self.assertIsNotNone(self.message.get_body(preferencelist=("plain",)))
        self.assertIsNotNone(self.message.get_body(preferencelist=("html",)))

    def test_a_high_priority_sets_both_headers(self):
        self.assertTrue(self.message.get("X-Priority", "").startswith("1"))
        self.assertEqual(self.message.get("Importance"), "High")

    def test_a_read_receipt_names_the_sender(self):
        self.assertEqual(
            self.message.get("Disposition-Notification-To"), "harith@example.org"
        )

    def test_the_dsn_intent_is_recorded(self):
        self.assertIsNotNone(self.message.get("X-ZBox-DSN"))

    def test_the_sent_copy_destination_is_recorded(self):
        self.assertEqual(self.message.get("X-ZBox-Sent-Copy"), "folder:Archive")

    def test_the_attachment_keeps_its_name(self):
        names = [p.get_filename() for p in self.message.walk() if p.get_filename()]
        self.assertIn("notes.txt", names)

    def test_the_attachment_type_is_guessed_not_octet_stream(self):
        types = [p.get_content_type() for p in self.message.walk()
                 if p.get_filename() == "notes.txt"]
        self.assertIn("text/plain", types)

    def test_the_inline_image_sits_with_the_html_not_beside_it(self):
        related = [p for p in self.message.walk()
                   if p.get_content_type() == "multipart/related"]
        self.assertTrue(related)
        inside = [p.get_content_type() for p in related[0].iter_parts()]
        self.assertIn("image/png", inside)
        self.assertIn("text/html", inside)

    def test_the_inline_image_carries_its_content_id(self):
        ids = [p.get("Content-ID") for p in self.message.walk() if p.get("Content-ID")]
        self.assertTrue(any("sig0@zbox" in (i or "") for i in ids))


class Threading(unittest.TestCase):
    def test_reply_headers_are_carried(self):
        message = message_build.build_message(spec(
            in_reply_to="<one@example.com>",
            references="<one@example.com>",
        ))
        self.assertEqual(message.get("In-Reply-To"), "<one@example.com>")
        self.assertEqual(message.get("References"), "<one@example.com>")


if __name__ == "__main__":
    unittest.main()
