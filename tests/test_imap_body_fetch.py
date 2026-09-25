"""
The raw-RFC822 -> himalaya-'message read'-shape converter used by the
pooled-connection fast path.

The whole point of imap_body_fetch is that the rest of the app cannot
tell where a message dict came from, so these tests check the shape
against exactly what message_body.py and message_view_panel.py read
out of it: parts flat, text_body/html_body/attachments as indexes
into it, tagged-union bodies, and snake_case-or-{"other": ...} header
names with tagged values.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import imap_body_fetch as ibf
import message_body as mb


PLAIN = b"""\
From: Alice Example <alice@example.com>
To: Bob <bob@example.org>, carol@example.org
Subject: Quarterly numbers
Date: Mon, 7 Sep 2026 11:49:57 -0700
Message-ID: <root@example.com>
X-Mailer: Something Custom

Here are the numbers.
Second line.
"""

MULTIPART = b"""\
From: Alice Example <alice@example.com>
To: bob@example.org
Subject: Re: Quarterly numbers
Message-ID: <reply@example.com>
In-Reply-To: <root@example.com>
References: <root@example.com> <mid@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUTER"

--OUTER
Content-Type: multipart/alternative; boundary="INNER"

--INNER
Content-Type: text/plain; charset="utf-8"

Plain version.
--INNER
Content-Type: text/html; charset="utf-8"

<p>HTML version.</p>
--INNER--
--OUTER
Content-Type: application/pdf; name="report.pdf"
Content-Disposition: attachment; filename="report.pdf"
Content-Transfer-Encoding: base64

aGVsbG8gd29ybGQ=
--OUTER--
"""

ENCODED_SUBJECT = (
    b"From: =?utf-8?B?w4RubmU=?= <anne@example.com>\r\n"
    b"Subject: =?utf-8?B?U3Ryw6ZuZ2U=?=\r\n"
    b"\r\n"
    b"body\r\n"
)


class SinglePartMessages(unittest.TestCase):
    def test_body_is_extractable_by_the_shared_extractor(self):
        message = ibf.build_message_dict(PLAIN)
        self.assertEqual(
            mb.extract_message_body(message),
            "Here are the numbers.\nSecond line.",
        )

    def test_text_body_indexes_into_parts(self):
        message = ibf.build_message_dict(PLAIN)
        self.assertEqual(message["text_body"], [0])
        self.assertEqual(message["html_body"], [])
        self.assertEqual(message["attachments"], [])
        self.assertIn("Text", message["parts"][0]["body"])

    def test_headers_live_on_the_first_part(self):
        message = ibf.build_message_dict(PLAIN)
        self.assertEqual(mb.header_text(message, "subject"), "Quarterly numbers")
        self.assertEqual(
            mb.header_addresses(message, "from"),
            [("Alice Example", "alice@example.com")],
        )
        self.assertEqual(
            mb.header_addresses(message, "to"),
            [("Bob", "bob@example.org"), ("", "carol@example.org")],
        )

    def test_unknown_headers_use_the_other_shape(self):
        message = ibf.build_message_dict(PLAIN)
        names = [header["name"] for header in message["parts"][0]["headers"]]
        self.assertIn({"other": "X-Mailer"}, names)
        self.assertIn("subject", names)

    def test_encoded_words_are_decoded(self):
        message = ibf.build_message_dict(ENCODED_SUBJECT)
        self.assertEqual(mb.header_text(message, "subject"), "Strænge")
        self.assertEqual(
            mb.header_addresses(message, "from"),
            [("Änne", "anne@example.com")],
        )


class MultipartMessages(unittest.TestCase):
    def setUp(self):
        self.message = ibf.build_message_dict(MULTIPART)

    def test_plain_text_is_preferred_for_the_reader(self):
        self.assertEqual(mb.extract_message_body(self.message), "Plain version.")

    def test_html_is_available_for_the_rendered_view(self):
        self.assertEqual(
            mb.extract_message_html(self.message), "<p>HTML version.</p>"
        )

    def test_container_parts_carry_child_indexes(self):
        root_body = self.message["parts"][0]["body"]
        self.assertIn("Multipart", root_body)
        # Depth-first: the alternative container and the attachment
        # are the root's own children.
        self.assertEqual(root_body["Multipart"], [1, 4])

    def test_attachment_is_indexed_with_a_filename_and_size(self):
        self.assertEqual(self.message["attachments"], [4])
        part = self.message["parts"][4]
        self.assertEqual(part["filename"], "report.pdf")
        self.assertEqual(part["size"], len(b"hello world"))
        # Deliberately no decoded payload: Save All goes through
        # himalaya's own 'attachment download'.
        self.assertNotIn("body", part)

    def test_threading_headers_survive_the_conversion(self):
        # Reply/Reply All/Forward and thread grouping all read these.
        self.assertEqual(
            mb.thread_ids(self.message),
            {"<reply@example.com>", "<root@example.com>", "<mid@example.com>"},
        )
        in_reply_to, references = mb.threading_headers(self.message)
        self.assertEqual(in_reply_to, "<reply@example.com>")
        self.assertEqual(
            references,
            "<root@example.com> <mid@example.com> <reply@example.com>",
        )


class Robustness(unittest.TestCase):
    def test_an_empty_message_still_produces_the_shape(self):
        message = ibf.build_message_dict(b"\r\n")
        self.assertEqual(message["attachments"], [])
        self.assertIsInstance(message["parts"], list)
        # No body is a legitimate answer, not a crash.
        self.assertFalse(mb.extract_message_body(message))

    def test_a_bad_charset_does_not_raise(self):
        raw = (
            b"Subject: bad charset\r\n"
            b'Content-Type: text/plain; charset="definitely-not-a-charset"\r\n'
            b"\r\n"
            b"still readable\r\n"
        )
        message = ibf.build_message_dict(raw)
        self.assertEqual(mb.extract_message_body(message), "still readable")


if __name__ == "__main__":
    unittest.main()
