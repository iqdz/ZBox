"""
The envelope dict built from an IMAP FETCH must be indistinguishable
from the one himalaya's 'envelope list --json' produces.

This is a wider contract than the message-body one: envelope_format
reads the flags, sender and date, thread_grouping reads message-id
and in-reply-to, the sort reads the date, and the collapsed-thread
aggregates read the flags of every member. A field in the wrong shape
here does not raise -- it silently reports every message as read, or
puts every message in its own thread.

The fixture shapes below mirror what imapclient returns: bytes for
IMAP strings, an ENVELOPE object with .subject/.date/.from_/.message_id,
and Address objects with .mailbox/.host/.name.
"""

import datetime
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import envelope_format as ef
import imap_body_fetch as ibf
import thread_grouping


class _Address:
    def __init__(self, name, mailbox, host):
        self.name = name
        self.mailbox = mailbox
        self.host = host


class _Envelope:
    def __init__(self, subject=b"", date=None, from_=(), to=(), cc=(),
                 message_id=b"", in_reply_to=b""):
        self.subject = subject
        self.date = date
        self.from_ = from_
        self.to = to
        self.cc = cc
        self.message_id = message_id
        self.in_reply_to = in_reply_to


TZ = datetime.timezone(datetime.timedelta(hours=-4))
SENT = datetime.datetime(2026, 9, 7, 18, 55, 26, tzinfo=TZ)

FULL = {
    b"ENVELOPE": _Envelope(
        subject=b"Quarterly numbers",
        date=SENT,
        from_=[_Address(b"Alice Example", b"alice", b"example.com")],
        to=[_Address(None, b"bob", b"example.org"),
            _Address(b"Carol", b"carol", b"example.org")],
        cc=[_Address(b"Dan", b"dan", b"example.org")],
        message_id=b"<root@example.com>",
        in_reply_to=b"<parent@example.com>",
    ),
    b"FLAGS": (b"\\Seen", b"\\Flagged", b"NonJunk"),
    b"INTERNALDATE": SENT,
}


class EnvelopeShape(unittest.TestCase):
    def setUp(self):
        self.envelope = ibf._envelope_dict(378994, FULL)

    def test_the_id_is_the_uid_as_a_string(self):
        # Every caller passes it straight back to a fetch by id.
        self.assertEqual(self.envelope["id"], "378994")

    def test_the_sender_reads_through_envelope_format(self):
        self.assertEqual(ef._envelope_from(self.envelope), "Alice Example")

    def test_addresses_use_the_email_key_not_address(self):
        # Envelope addresses and message-header addresses use
        # different keys for the same thing. This is the envelope
        # side; getting it wrong silently blanks every sender.
        self.assertEqual(
            self.envelope["from"],
            [{"name": "Alice Example", "email": "alice@example.com"}],
        )
        self.assertEqual(
            [a["email"] for a in self.envelope["to"]],
            ["bob@example.org", "carol@example.org"],
        )
        self.assertEqual(self.envelope["to"][0]["name"], "")

    def test_the_date_round_trips_through_the_sort_parser(self):
        parsed = ef._parse_envelope_date(self.envelope)
        self.assertEqual(parsed, SENT)
        # The offset has to survive: sorting the raw string across
        # mixed timezones is exactly the bug _parse_envelope_date
        # was written for.
        self.assertEqual(parsed.utcoffset(), datetime.timedelta(hours=-4))

    def test_flags_are_read_by_the_existing_flag_helpers(self):
        self.assertFalse(ef._envelope_is_unread(self.envelope))
        self.assertTrue(ef._envelope_is_flagged(self.envelope))

    def test_a_non_standard_keyword_keeps_its_name_and_claims_no_iana(self):
        # A provider keyword must not accidentally satisfy a check
        # that matches on iana.
        keyword = [f for f in self.envelope["flags"] if f["raw"] == "NonJunk"]
        self.assertEqual(keyword, [{"raw": "NonJunk", "iana": None}])

    def test_threading_headers_are_where_thread_grouping_looks(self):
        self.assertEqual(
            thread_grouping.envelope_message_id(self.envelope), "root@example.com"
        )
        self.assertEqual(
            thread_grouping.envelope_parent_id(self.envelope), "parent@example.com"
        )

    def test_in_reply_to_is_a_list(self):
        # The shape himalaya reports, and the one envelope_parent_id
        # handles first.
        self.assertEqual(self.envelope["in-reply-to"], ["<parent@example.com>"])


class SparseEnvelopes(unittest.TestCase):
    def test_no_flags_means_unread(self):
        envelope = ibf._envelope_dict(1, {b"ENVELOPE": _Envelope(date=SENT), b"FLAGS": ()})
        self.assertTrue(ef._envelope_is_unread(envelope))
        self.assertFalse(ef._envelope_is_flagged(envelope))

    def test_a_message_with_no_threading_headers_is_its_own_thread(self):
        envelope = ibf._envelope_dict(1, {b"ENVELOPE": _Envelope(date=SENT)})
        self.assertEqual(thread_grouping.envelope_message_id(envelope), "")
        self.assertEqual(thread_grouping.envelope_parent_id(envelope), "")
        self.assertEqual(envelope["in-reply-to"], [])

    def test_a_missing_subject_falls_back_the_same_way(self):
        envelope = ibf._envelope_dict(1, {b"ENVELOPE": _Envelope(date=SENT)})
        self.assertEqual(ef._envelope_subject(envelope), "(no subject)")

    def test_an_unparseable_header_date_falls_back_to_internaldate(self):
        envelope = ibf._envelope_dict(
            1, {b"ENVELOPE": _Envelope(date=None), b"INTERNALDATE": SENT},
        )
        self.assertEqual(ef._parse_envelope_date(envelope), SENT)

    def test_no_date_at_all_does_not_raise(self):
        envelope = ibf._envelope_dict(1, {b"ENVELOPE": _Envelope()})
        self.assertEqual(envelope["date"], "")
        ef._parse_envelope_date(envelope)  # sorts first rather than raising

    def test_no_envelope_at_all_still_produces_a_usable_row(self):
        envelope = ibf._envelope_dict(7, {b"FLAGS": (b"\\Seen",)})
        self.assertEqual(envelope["id"], "7")
        self.assertEqual(ef._envelope_from(envelope), "(unknown sender)")
        self.assertFalse(ef._envelope_is_unread(envelope))

    def test_an_encoded_subject_is_decoded(self):
        envelope = ibf._envelope_dict(
            1, {b"ENVELOPE": _Envelope(subject=b"=?utf-8?B?U3Ryw6ZuZ2U=?=", date=SENT)},
        )
        self.assertEqual(envelope["subject"], "Strænge")

    def test_an_address_missing_its_host_is_skipped_not_mangled(self):
        envelope = ibf._envelope_dict(1, {b"ENVELOPE": _Envelope(
            date=SENT, from_=[_Address(b"Broken", b"nobody", None)],
        )})
        self.assertEqual(envelope["from"], [])


if __name__ == "__main__":
    unittest.main()
