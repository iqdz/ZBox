"""
Offline-copy dedup with folded headers: a Message-ID written on the
line after "Message-ID:" must be read, and copies made before that was
fixed must be reduced to one.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import himalaya_client as hc

FOLDED = (
    "From: Someone <someone@example.com>\r\r\n"
    "Subject: Folded header test\r\r\n"
    "Message-ID: \r\r\n"
    " <ZB0X000TEST01@ZB0X000TEST01.PROD.OUTLOOK.COM>\r\r\n"
    "Accept-Language: en-US\r\r\n"
    "\r\r\n"
    "Body.\r\r\n"
)


class FoldedHeaderTests(unittest.TestCase):
    def test_folded_message_id_is_read(self):
        self.assertEqual(
            hc._message_id_from_raw(FOLDED),
            "zb0x000test01@zb0x000test01.prod.outlook.com",
        )

    def test_folded_id_matches_the_server_spelling(self):
        self.assertEqual(
            hc._dedup_key_from_raw(FOLDED),
            hc._envelope_message_id({"message-id": "<ZB0X000TEST01@ZB0X000TEST01.PROD.OUTLOOK.COM>"}),
        )

    def test_folded_subject_is_joined(self):
        raw = "Subject: first part\n second part\nFrom: a@b\n\nbody"
        self.assertEqual(hc._header_fields(raw, ("subject",))["subject"], "first part second part")


class DuplicateCopiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zbox_dupes_")
        for sub in ("cur", "new", "tmp"):
            os.makedirs(os.path.join(self.tmp, sub))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.tmp, "cur", name), "wb") as handle:
            handle.write(text.encode("latin-1"))

    def test_extra_copies_are_removed_keeping_the_oldest(self):
        for stamp in ("1790225500", "1790225818", "1790226138"):
            self.write(stamp + ".#0M1.HSPC;2,", FOLDED)
        self.write("1790220000.#0M9.HSPC;2,", "Message-ID: <other@x>\n\nbody")
        self.assertEqual(hc._remove_duplicate_copies(self.tmp), 2)
        left = sorted(os.listdir(os.path.join(self.tmp, "cur")))
        self.assertEqual(left, ["1790220000.#0M9.HSPC;2,", "1790225500.#0M1.HSPC;2,"])
        self.assertEqual(len(hc._stored_message_ids(self.tmp)), 2)


class MaildirListingDedupTests(unittest.TestCase):
    def run_listing(self, backend, rows):
        original = hc._run
        hc._run = lambda *args, **kwargs: {"envelopes": rows}
        try:
            return hc.list_envelopes(None, None, folder="INBOX", backend=backend)
        finally:
            hc._run = original

    def test_maildir_listing_keeps_one_row_per_message_id(self):
        rows = [
            {"id": "a", "message-id": "<X@outlook.com>"},
            {"id": "b", "message-id": "x@OUTLOOK.com"},
            {"id": "c", "message-id": "<y@example.com>"},
            {"id": "d"},
            {"id": "e", "message-id": "<>"},
            {"id": "f", "message-id": "<X@outlook.com>"},
        ]
        ids = [row["id"] for row in self.run_listing("maildir", rows)]
        self.assertEqual(ids, ["a", "c", "d", "e"])

    def test_imap_subprocess_listing_is_untouched(self):
        rows = [
            {"id": "1", "message-id": "<X@outlook.com>"},
            {"id": "2", "message-id": "<X@outlook.com>"},
        ]
        original = hc.imap_body_fetch.list_envelopes

        def unavailable(*args, **kwargs):
            raise hc.imap_body_fetch.ImapBodyFetchUnavailable("test")

        hc.imap_body_fetch.list_envelopes = unavailable
        try:
            ids = [row["id"] for row in self.run_listing("imap", rows)]
        finally:
            hc.imap_body_fetch.list_envelopes = original
        self.assertEqual(ids, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
