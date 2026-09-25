"""
Envelope and message formatting, the pure half of what used to live
in main_frame.py.

Every fixture here is the shape Himalaya 2.1.0 actually returns.
That matters more than it sounds: the two address shapes below are
different on purpose. A list envelope's addresses use the key
"email", a parsed message header's use "address", and assuming
either one universally has produced real bugs.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import envelope_format as ef
import message_body as mb


# Flags are objects, not strings. A read message carries an entry
# with iana == "seen"; an unread one simply has no such entry, which
# is not the same as an empty list -- an unread message often still
# carries other flags. Confirmed against live capture; guessing this
# shape has caused bugs before.
SEEN = {"iana": "seen", "raw": "\\Seen"}
FLAGGED = {"iana": "flagged", "raw": "\\Flagged"}
NON_JUNK = {"iana": None, "raw": "NonJunk"}


def envelope(**overrides):
    """A list envelope, as Himalaya returns it. Addresses use
    "email"."""
    base = {
        "id": "42",
        "subject": "Quarterly report",
        "date": "2026-09-03 15:33:00",
        "flags": [SEEN],
        "from": [{"name": "Sam Sender", "email": "sender@example.com"}],
        "to": [{"name": "Harith", "email": "account@example.com"}],
    }
    base.update(overrides)
    return base


def message(headers=None, body="Hello there."):
    """A parsed message. Header addresses use "address"."""
    return {
        "parts": [{
            "headers": headers or [],
            "body": {"Text": body},
        }],
        "text_body": [0],
        "html_body": [],
    }


class SubjectLines(unittest.TestCase):
    def test_reply_prefixes_once(self):
        self.assertEqual(ef._reply_subject("Report"), "Re: Report")
        self.assertEqual(ef._reply_subject("Re: Report"), "Re: Report")
        self.assertEqual(ef._reply_subject("RE: Report"), "RE: Report")

    def test_forward_prefixes_once(self):
        self.assertEqual(ef._forward_subject("Report"), "Fwd: Report")
        self.assertEqual(ef._forward_subject("Fwd: Report"), "Fwd: Report")

    def test_missing_subject_is_named_not_blank(self):
        # A blank row is unreadable; the reader needs to hear
        # something.
        self.assertEqual(ef._envelope_subject({}), "(no subject)")
        self.assertEqual(ef._reply_subject(None), "Re: (no subject)")


class SenderReading(unittest.TestCase):
    def test_prefers_display_name(self):
        self.assertEqual(ef._envelope_from(envelope()), "Sam Sender")

    def test_falls_back_to_address(self):
        env = envelope(**{"from": [{"email": "nobody@example.com"}]})
        self.assertEqual(ef._envelope_from(env), "nobody@example.com")

    def test_survives_a_missing_from(self):
        self.assertEqual(ef._envelope_from({}), "(unknown sender)")
        self.assertEqual(ef._envelope_from({"from": []}), "(unknown sender)")
        self.assertEqual(ef._envelope_from({"from": "not a list"}), "(unknown sender)")

    def test_sender_address_is_formatted_for_a_to_field(self):
        self.assertEqual(
            ef._envelope_sender_address(envelope()),
            "Sam Sender <sender@example.com>",
        )

    def test_sender_email_only_is_lowercased(self):
        env = envelope(**{"from": [{"name": "X", "email": "MiXeD@Example.COM"}]})
        self.assertEqual(ef._envelope_sender_email_only(env), "mixed@example.com")


class SpokenSender(unittest.TestCase):
    """Ctrl+U reads this aloud, so it is written for speech: no angle
    brackets, which a screen reader pronounces and which carry
    nothing here."""

    def test_name_and_address(self):
        self.assertEqual(
            ef._envelope_sender_spoken(envelope()),
            ("Sam Sender, sender@example.com", "sender@example.com"),
        )

    def test_address_alone_when_there_is_no_name(self):
        env = envelope(**{"from": [{"email": "no-reply@list.example"}]})
        spoken, address = ef._envelope_sender_spoken(env)
        self.assertEqual(spoken, "no-reply@list.example")
        self.assertEqual(address, "no-reply@list.example")

    def test_a_name_with_no_address_says_so(self):
        env = envelope(**{"from": [{"name": "Mystery"}]})
        spoken, address = ef._envelope_sender_spoken(env)
        self.assertEqual(spoken, "Mystery, no address")
        self.assertEqual(address, "", "nothing to copy, so nothing is copied")

    def test_no_sender_is_a_sentence_not_an_empty_string(self):
        for env in ({}, {"from": []}, {"from": "nonsense"}):
            spoken, address = ef._envelope_sender_spoken(env)
            self.assertEqual(spoken, "No sender on this message.")
            self.assertEqual(address, "")

    def test_the_copied_address_never_carries_the_name(self):
        # Ctrl+Shift+U pastes into a To field or a filter rule, so it
        # must be the bare address.
        _spoken, address = ef._envelope_sender_spoken(envelope())
        self.assertEqual(address, "sender@example.com")
        self.assertNotIn("Sam Sender", address)
        self.assertNotIn("<", address)


class ReplyRecipient(unittest.TestCase):
    """Reply-To exists so mailing lists and no-reply senders can point
    replies somewhere a person reads. Reading From regardless was a
    real bug."""

    def test_uses_reply_to_when_present(self):
        msg = message([
            {"name": "reply_to", "value": {"Address": {"List": [
                {"name": "The List", "address": "list@example.org"}]}}},
        ])
        to_field, covered = ef._reply_recipient(envelope(), msg)
        self.assertEqual(to_field, "The List <list@example.org>")
        self.assertEqual(covered, {"list@example.org"})

    def test_falls_back_to_from(self):
        to_field, covered = ef._reply_recipient(envelope(), message())
        self.assertEqual(to_field, "Sam Sender <sender@example.com>")
        self.assertEqual(covered, {"sender@example.com"})

    def test_reply_all_excludes_everyone_already_in_to(self):
        class Account:
            identity_email = "account@example.com"
            login_email = "account@example.com"

        env = envelope(
            to=[{"name": "Harith", "email": "account@example.com"},
                {"name": "Other", "email": "other@example.com"}],
            cc=[{"name": "Third", "email": "third@example.com"}],
        )
        cc = ef._reply_all_cc_field(Account, env, {"sender@example.com"})
        self.assertIn("other@example.com", cc)
        self.assertIn("third@example.com", cc)
        # Never Cc yourself, and never Cc whoever is already in To.
        self.assertNotIn("account@example.com", cc)
        self.assertNotIn("sender@example.com", cc)


class Flags(unittest.TestCase):
    def test_unread_detection(self):
        self.assertFalse(ef._envelope_is_unread(envelope(flags=[SEEN])))
        self.assertTrue(ef._envelope_is_unread(envelope(flags=[])))

    def test_unread_is_the_absence_of_seen_not_an_empty_list(self):
        self.assertTrue(ef._envelope_is_unread(envelope(flags=[NON_JUNK])))
        self.assertFalse(ef._envelope_is_unread(envelope(flags=[NON_JUNK, SEEN])))

    def test_flagged_detection(self):
        self.assertTrue(ef._envelope_is_flagged(envelope(flags=[SEEN, FLAGGED])))
        self.assertFalse(ef._envelope_is_flagged(envelope(flags=[SEEN])))

    def test_status_text_is_words_not_colour(self):
        # A screen reader cannot hear bold or a colour; the row has to
        # say it. "Starred", not "Flagged" -- changed 2026-09-06 at
        # the user's request to match what they actually call the
        # state; this test kept asserting the old word.
        self.assertIn("Unread", ef._envelope_status_text(True, False))
        self.assertIn("Starred", ef._envelope_status_text(False, True))
        both = ef._envelope_status_text(True, True)
        self.assertIn("Unread", both)
        self.assertIn("Starred", both)

    def test_setting_a_flag_round_trips(self):
        env = envelope(flags=[SEEN])
        ef._set_envelope_flag(env, "flagged", True)
        self.assertTrue(ef._envelope_is_flagged(env))
        self.assertFalse(ef._envelope_is_unread(env), "toggling a flag must not lose Seen")
        ef._set_envelope_flag(env, "flagged", False)
        self.assertFalse(ef._envelope_is_flagged(env))


class Signature(unittest.TestCase):
    """The auto-refresh compares this to decide whether to repaint.
    Comparing it as an ordered sequence is what threw away the
    reader's chosen sort order every twenty seconds."""

    def test_order_does_not_count_as_a_change(self):
        rows = [envelope(id="1", subject="A"), envelope(id="2", subject="B")]
        self.assertEqual(
            ef._envelopes_signature(rows),
            ef._envelopes_signature(list(reversed(rows))),
        )

    def test_new_mail_does_count_as_a_change(self):
        rows = [envelope(id="1")]
        self.assertNotEqual(
            ef._envelopes_signature(rows),
            ef._envelopes_signature(rows + [envelope(id="2")]),
        )

    def test_read_state_counts_as_a_change(self):
        self.assertNotEqual(
            ef._envelopes_signature([envelope(flags=[SEEN])]),
            ef._envelopes_signature([envelope(flags=[])]),
        )

    def test_missing_account_id_does_not_break_the_sort(self):
        # None mixed with a real id used to raise TypeError here.
        rows = [envelope(id="1", _zbox_account_id=None),
                envelope(id="2", _zbox_account_id="acct_a")]
        self.assertEqual(len(ef._envelopes_signature(rows)), 2)


class Sorting(unittest.TestCase):
    def test_every_advertised_sort_key_exists(self):
        self.assertEqual(set(ef._SORT_KEY_FUNCS), {"date", "subject", "from"})

    def test_subject_and_sender_sort_case_insensitively(self):
        rows = [envelope(id="1", subject="apple"), envelope(id="2", subject="Banana")]
        ordered = sorted(rows, key=ef._SORT_KEY_FUNCS["subject"])
        self.assertEqual([r["subject"] for r in ordered], ["apple", "Banana"])

    def test_date_sort_uses_actual_time_not_the_string(self):
        # Real shape from a live debug log: a Gmail message at
        # 18:55:26-04:00 (22:55:26 UTC) actually happened AFTER a
        # message at 21:02:55+01:00 (20:02:55 UTC) the same day, but
        # "18:55:26-04:00" < "21:02:55+01:00" as plain strings -- '1'
        # sorts before '2' -- so the old string sort got this backwards.
        later = envelope(id="later", date="2026-09-03T18:55:26-04:00")
        earlier = envelope(id="earlier", date="2026-09-03T21:02:55+01:00")
        ordered = sorted(
            [earlier, later], key=ef._SORT_KEY_FUNCS["date"], reverse=True
        )
        self.assertEqual([e["id"] for e in ordered], ["later", "earlier"])

    def test_date_sort_handles_z_suffix(self):
        # fromisoformat only accepts a bare 'Z' from Python 3.11; ZBox
        # runs on 3.10, and Himalaya does send 'Z' for UTC senders.
        z = envelope(id="z", date="2026-09-01T17:35:02Z")
        self.assertEqual(
            ef._parse_envelope_date(z),
            ef._parse_envelope_date(envelope(id="z2", date="2026-09-01T17:35:02+00:00")),
        )

    def test_date_sort_never_raises_on_missing_or_bad_dates(self):
        missing = envelope(id="missing", date=None)
        bad = envelope(id="bad", date="not-a-date")
        good = envelope(id="good", date="2026-09-03T21:02:55+01:00")
        ordered = sorted(
            [missing, bad, good], key=ef._SORT_KEY_FUNCS["date"], reverse=True
        )
        self.assertEqual(ordered[0]["id"], "good")


class FolderNames(unittest.TestCase):
    def test_inbox_is_always_the_real_inbox(self):
        self.assertEqual(ef._folder_display_to_himalaya("Inbox"), "INBOX")

    def test_gmail_special_folders_resolve(self):
        class GmailAccount:
            imap_host = "imap.gmail.com"

        self.assertEqual(
            ef._folder_display_to_himalaya("Inbox", GmailAccount), "INBOX"
        )
        # Gmail has no real Archive folder; its archive is All Mail.
        self.assertEqual(
            ef._folder_display_to_himalaya("Archive", GmailAccount),
            "[Gmail]/All Mail",
        )

    def test_gmail_trash_is_not_called_trash(self):
        """Delete moves a message to this folder. Passing the literal
        "Trash" is what made Delete misbehave on Gmail, where no such
        folder exists."""
        class GmailAccount:
            imap_host = "imap.gmail.com"

        self.assertEqual(
            ef._folder_display_to_himalaya("Trash", GmailAccount),
            "[Gmail]/Trash",
        )
        self.assertNotEqual(
            ef._folder_display_to_himalaya("Trash", GmailAccount), "Trash"
        )

    def test_other_providers_pass_through(self):
        class PlainAccount:
            imap_host = "imap.disroot.org"

        self.assertEqual(
            ef._folder_display_to_himalaya("Sent", PlainAccount), "Sent"
        )


class JunkFolderForAccount(unittest.TestCase):
    """Audit finding 46: Mark as Junk's target folder."""

    def test_falls_back_to_the_generic_gmail_mapping(self):
        class GmailAccount:
            imap_host = "imap.gmail.com"
            junk_folder = ""

        self.assertEqual(
            ef._junk_folder_for_account(GmailAccount), "[Gmail]/Spam",
        )

    def test_falls_back_to_the_generic_plain_mapping(self):
        class PlainAccount:
            imap_host = "imap.disroot.org"
            junk_folder = ""

        self.assertEqual(ef._junk_folder_for_account(PlainAccount), "Junk")

    def test_an_explicit_override_wins(self):
        class OverriddenAccount:
            imap_host = "imap.gmail.com"
            junk_folder = "Custom Spam Folder"

        self.assertEqual(
            ef._junk_folder_for_account(OverriddenAccount), "Custom Spam Folder",
        )

    def test_a_blank_or_whitespace_override_is_treated_as_unset(self):
        class BlankOverrideAccount:
            imap_host = "imap.disroot.org"
            junk_folder = "   "

        self.assertEqual(ef._junk_folder_for_account(BlankOverrideAccount), "Junk")

    def test_missing_attribute_does_not_error(self):
        # Older Account instances loaded before this field existed --
        # getattr's default covers them without a migration.
        class NoJunkAttributeAccount:
            imap_host = "imap.disroot.org"

        self.assertEqual(ef._junk_folder_for_account(NoJunkAttributeAccount), "Junk")


class ResolveSearchFolders(unittest.TestCase):
    """search_panel's "All Folders" scope bug: main_frame.
    _folders_for_account returns generic display names ("Sent",
    "Archive", ...) until that account's real folder list has been
    fetched at least once, and those names must be resolved through
    _folder_display_to_himalaya before being handed to Himalaya --
    exactly like main_frame._build_move_copy_menus already does --
    or a Gmail-style account's search silently fails in every folder
    but Inbox. Confirmed live against the pinned himalaya v2.1.0
    build: `envelope search -m Sent ...` against a folder that isn't
    really called Sent fails outright rather than fuzzy-matching."""

    def test_generic_names_resolve_per_provider(self):
        class GmailAccount:
            imap_host = "imap.gmail.com"

        generic = ["Inbox", "Sent", "Drafts", "Trash", "Junk", "Archive"]
        resolved = ef._resolve_search_folders(generic, GmailAccount)
        self.assertEqual(resolved[0], "INBOX")
        self.assertEqual(resolved[1], "[Gmail]/Sent Mail")
        self.assertEqual(resolved[5], "[Gmail]/All Mail")
        # The whole point: none of Gmail's real folders are literally
        # spelled like the generic names that used to be sent as-is.
        self.assertNotIn("Sent", resolved)
        self.assertNotIn("Archive", resolved)

    def test_plain_provider_names_pass_through_unchanged(self):
        class PlainAccount:
            imap_host = "imap.disroot.org"

        generic = ["Inbox", "Sent", "Drafts", "Trash", "Junk", "Archive"]
        self.assertEqual(
            ef._resolve_search_folders(generic, PlainAccount),
            ["INBOX", "Sent", "Drafts", "Trash", "Junk", "Archive"],
        )

    def test_already_real_server_names_pass_through(self):
        # Once main_frame._folders_for_account's background fetch has
        # returned once, it hands back real server folder names, not
        # the generic six -- resolving those again must be a no-op.
        class GmailAccount:
            imap_host = "imap.gmail.com"

        real_names = ["INBOX", "[Gmail]/Sent Mail", "Some Custom Label"]
        self.assertEqual(
            ef._resolve_search_folders(real_names, GmailAccount), real_names,
        )


class ThreadingHeaders(unittest.TestCase):
    """Without these a reply starts a new conversation in every
    client, however the subject reads."""

    def test_builds_the_chain_from_the_original(self):
        msg = message([
            {"name": "message_id", "value": {"Text": "current@example.com"}},
            {"name": "references", "value": {"TextList": ["<root@example.com>"]}},
        ])
        in_reply_to, references = mb.threading_headers(msg)
        self.assertEqual(in_reply_to, "<current@example.com>")
        self.assertEqual(references, "<root@example.com> <current@example.com>")

    def test_angle_brackets_are_added_not_doubled(self):
        self.assertEqual(mb.angle_wrap("a@b"), "<a@b>")
        self.assertEqual(mb.angle_wrap("<a@b>"), "<a@b>")
        self.assertEqual(mb.angle_wrap(""), "")

    def test_a_message_with_no_headers_threads_nothing(self):
        self.assertEqual(mb.threading_headers(message()), ("", ""))
        self.assertEqual(mb.threading_headers(None), ("", ""))

    def test_long_chains_keep_the_root_and_the_recent(self):
        chain = ["<m%d@x>" % n for n in range(40)]
        msg = message([
            {"name": "message_id", "value": {"Text": "last@x"}},
            {"name": "references", "value": {"TextList": chain}},
        ])
        _in_reply_to, references = mb.threading_headers(msg)
        tokens = references.split()
        self.assertLessEqual(len(tokens), 20)
        self.assertEqual(tokens[0], "<m0@x>")
        self.assertEqual(tokens[-1], "<last@x>")


class RelatedMessages(unittest.TestCase):
    """message_body.normalized_subject and thread_ids -- audit
    finding 44's "show related messages" action."""

    def test_strips_one_reply_prefix(self):
        self.assertEqual(mb.normalized_subject("Re: Lunch?"), "lunch?")

    def test_strips_repeated_mixed_prefixes(self):
        self.assertEqual(
            mb.normalized_subject("Re: Re: Fwd: Fw: Aw: Q3 report"), "q3 report",
        )

    def test_case_insensitive_and_whitespace_tolerant(self):
        self.assertEqual(mb.normalized_subject("  RE:   Lunch?  "), "lunch?")
        self.assertEqual(mb.normalized_subject("FWD:Lunch?"), "lunch?")

    def test_no_prefix_is_just_lowercased_and_trimmed(self):
        self.assertEqual(mb.normalized_subject("  Lunch?  "), "lunch?")

    def test_none_and_empty_are_empty(self):
        self.assertEqual(mb.normalized_subject(None), "")
        self.assertEqual(mb.normalized_subject(""), "")

    def test_a_word_that_merely_starts_with_re_is_not_stripped(self):
        # "Reminder:" must not be mistaken for a "Re:" prefix.
        self.assertEqual(mb.normalized_subject("Reminder: pay rent"), "reminder: pay rent")

    def test_thread_ids_includes_own_id_references_and_in_reply_to(self):
        msg = message([
            {"name": "message_id", "value": {"Text": "child@x"}},
            {"name": "references", "value": {"TextList": ["<root@x>", "<parent@x>"]}},
            {"name": "in_reply_to", "value": {"Text": "parent@x"}},
        ])
        self.assertEqual(
            mb.thread_ids(msg), {"<child@x>", "<root@x>", "<parent@x>"},
        )

    def test_a_thread_root_is_just_its_own_id(self):
        msg = message([{"name": "message_id", "value": {"Text": "root@x"}}])
        self.assertEqual(mb.thread_ids(msg), {"<root@x>"})

    def test_a_message_with_no_threading_headers_has_no_thread_ids(self):
        self.assertEqual(mb.thread_ids(message()), set())
        self.assertEqual(mb.thread_ids(None), set())

    def test_parent_and_child_intersect(self):
        parent = message([{"name": "message_id", "value": {"Text": "p@x"}}])
        child = message([
            {"name": "message_id", "value": {"Text": "c@x"}},
            {"name": "references", "value": {"TextList": ["<p@x>"]}},
        ])
        self.assertTrue(mb.thread_ids(parent) & mb.thread_ids(child))

    def test_siblings_sharing_an_ancestor_intersect(self):
        sibling_a = message([
            {"name": "message_id", "value": {"Text": "a@x"}},
            {"name": "references", "value": {"TextList": ["<root@x>"]}},
        ])
        sibling_b = message([
            {"name": "message_id", "value": {"Text": "b@x"}},
            {"name": "references", "value": {"TextList": ["<root@x>"]}},
        ])
        self.assertTrue(mb.thread_ids(sibling_a) & mb.thread_ids(sibling_b))

    def test_unrelated_messages_do_not_intersect(self):
        one = message([{"name": "message_id", "value": {"Text": "one@x"}}])
        other = message([{"name": "message_id", "value": {"Text": "other@x"}}])
        self.assertFalse(mb.thread_ids(one) & mb.thread_ids(other))


class MessageHeaders(unittest.TestCase):
    def test_unknown_headers_are_reachable(self):
        msg = message([{"name": {"other": "X-Mailer"}, "value": {"Text": "ZBox"}}])
        self.assertEqual(mb.header_text(msg, "x_mailer"), "ZBox")

    def test_absent_header_is_empty_not_an_error(self):
        self.assertEqual(mb.header_text(message(), "message_id"), "")
        self.assertEqual(mb.header_addresses(message(), "cc"), [])

    def test_body_extraction(self):
        self.assertEqual(mb.extract_message_body(message(body="Hi.")), "Hi.")


class MessageHeaderBlock(unittest.TestCase):
    """Shown above an open message. Read top to bottom by a screen
    reader, so each line has to say which field it is."""

    def test_every_line_is_labelled(self):
        block = ef._message_header_block(envelope(
            cc=[{"name": "Third", "email": "third@example.com"}]
        ))
        lines = block.split("\n")
        self.assertTrue(all(":" in line for line in lines))
        self.assertTrue(lines[0].startswith("From: "))

    def test_includes_the_fields_the_list_does_not_show(self):
        block = ef._message_header_block(envelope(
            cc=[{"name": "Third", "email": "third@example.com"}]
        ))
        self.assertIn("To: ", block)
        self.assertIn("Cc: Third <third@example.com>", block)
        self.assertIn("Date: ", block)
        self.assertIn("Subject: Quarterly report", block)

    def test_empty_fields_are_omitted_not_announced_empty(self):
        # "Cc:" with nothing after it is a line the reader has to
        # listen to for no reason.
        block = ef._message_header_block(envelope(cc=[]))
        self.assertNotIn("Cc:", block)

    def test_subject_is_always_present_even_when_missing(self):
        block = ef._message_header_block({})
        self.assertIn("Subject: (no subject)", block)

    def test_survives_a_junk_envelope(self):
        for junk in ({}, {"from": "nonsense"}, {"to": None}):
            self.assertIn("Subject:", ef._message_header_block(junk))


class AppendSignature(unittest.TestCase):
    """Audit finding 41: signatures appended on new, reply and
    forward, via the conventional "-- " delimiter line."""

    def test_no_signature_is_a_no_op(self):
        self.assertEqual(ef._append_signature("Hello", ""), "Hello")
        self.assertEqual(ef._append_signature("Hello", None), "Hello")
        self.assertEqual(ef._append_signature("Hello", "   \n  "), "Hello")

    def test_appends_below_the_conventional_delimiter(self):
        result = ef._append_signature("Hello", "Harith\nZBox")
        self.assertEqual(result, "Hello\n\n-- \nHarith\nZBox\n")

    def test_signature_whitespace_is_trimmed(self):
        result = ef._append_signature("Hello", "\n\nHarith\n\n")
        self.assertEqual(result, "Hello\n\n-- \nHarith\n")

    def test_appends_after_an_already_quoted_reply_body(self):
        # _quoted_reply_body's own output already ends in a newline --
        # the signature should land after it, not merge into it.
        quoted = "\n\n\nOn today, X wrote:\n> hi\n"
        result = ef._append_signature(quoted, "Harith")
        self.assertTrue(result.startswith(quoted))
        self.assertTrue(result.endswith("\n\n-- \nHarith\n"))

    def test_blank_body_still_gets_a_signature(self):
        self.assertEqual(ef._append_signature("", "Harith"), "\n\n-- \nHarith\n")


class AccountsForSearchSelection(unittest.TestCase):
    """search_panel's Account choice has a synthetic "All Accounts"
    entry at index 0, with real accounts following at index N + 1."""

    def test_index_zero_is_all_accounts(self):
        self.assertEqual(
            ef._accounts_for_search_selection(["a", "b"], 0), ["a", "b"],
        )

    def test_a_real_selection_returns_just_that_one_account(self):
        self.assertEqual(
            ef._accounts_for_search_selection(["a", "b"], 2), ["b"],
        )

    def test_not_found_falls_back_to_every_account(self):
        # wx.NOT_FOUND is -1 -- hardcoded here rather than imported so
        # this test doesn't depend on wxstub defining it.
        self.assertEqual(
            ef._accounts_for_search_selection(["a", "b"], -1), ["a", "b"],
        )

    def test_stale_out_of_range_selection_falls_back_to_every_account(self):
        # refresh_accounts() rebuilt a shorter list (an account was
        # removed) out from under an existing selection.
        self.assertEqual(
            ef._accounts_for_search_selection(["a"], 2), ["a"],
        )

    def test_no_accounts_at_all_returns_an_empty_list_not_none(self):
        self.assertEqual(ef._accounts_for_search_selection([], 0), [])


class AnnouncementHold(unittest.TestCase):
    """The hold time is what stops an announcement closing mid-word,
    so out-of-range or junk values must land somewhere usable rather
    than at zero."""

    def setUp(self):
        from settings_manager import Settings
        self.Settings = Settings

    def test_default(self):
        self.assertEqual(self.Settings.from_dict({}).announcement_hold_ms, 1800)

    def test_clamped_to_a_sane_range(self):
        self.assertEqual(self.Settings(announcement_hold_ms=-500).announcement_hold_ms, 0)
        self.assertEqual(self.Settings(announcement_hold_ms=99999).announcement_hold_ms, 6000)

    def test_junk_falls_back_rather_than_silencing(self):
        for junk in (None, "soon", object()):
            self.assertEqual(
                self.Settings(announcement_hold_ms=junk).announcement_hold_ms, 1800
            )

    def test_zero_is_allowed_and_means_wait_for_a_key(self):
        self.assertEqual(self.Settings(announcement_hold_ms=0).announcement_hold_ms, 0)

    def test_roundtrip(self):
        saved = self.Settings(announcement_hold_ms=2500).to_dict()
        self.assertEqual(self.Settings.from_dict(saved).announcement_hold_ms, 2500)


class ReplyFromIdentity(unittest.TestCase):
    """Audit finding 49: which of the replying account's identities
    (primary or an alias) a reply should send from."""

    class _FakeAccount:
        def __init__(self, identity_email, extra=None):
            self.identity_email = identity_email
            self._extra = extra or []

        def identities(self):
            return [("Primary", self.identity_email)] + [
                (entry.get("display_name", ""), entry["email"]) for entry in self._extra
            ]

    def _to_entry(self, email):
        return {"name": "Someone", "email": email}

    def test_defaults_to_primary_when_nothing_matches(self):
        account = self._FakeAccount("primary@example.com")
        envelope = {"to": [self._to_entry("stranger@example.com")], "cc": []}
        self.assertEqual(ef._reply_from_identity(account, envelope), "primary@example.com")

    def test_matches_an_alias_addressed_in_to(self):
        account = self._FakeAccount(
            "primary@example.com",
            extra=[{"display_name": "Alias", "email": "alias@example.com"}],
        )
        envelope = {"to": [self._to_entry("alias@example.com")], "cc": []}
        self.assertEqual(ef._reply_from_identity(account, envelope), "alias@example.com")

    def test_matches_an_alias_addressed_only_in_cc(self):
        account = self._FakeAccount(
            "primary@example.com",
            extra=[{"display_name": "Alias", "email": "alias@example.com"}],
        )
        envelope = {"to": [self._to_entry("stranger@example.com")], "cc": [self._to_entry("alias@example.com")]}
        self.assertEqual(ef._reply_from_identity(account, envelope), "alias@example.com")

    def test_to_wins_over_cc_when_both_match_different_identities(self):
        account = self._FakeAccount(
            "primary@example.com",
            extra=[{"display_name": "Alias", "email": "alias@example.com"}],
        )
        envelope = {
            "to": [self._to_entry("alias@example.com")],
            "cc": [self._to_entry("primary@example.com")],
        }
        self.assertEqual(ef._reply_from_identity(account, envelope), "alias@example.com")

    def test_matching_is_case_insensitive_and_returns_stored_casing(self):
        account = self._FakeAccount(
            "primary@example.com",
            extra=[{"display_name": "Alias", "email": "Alias@Example.com"}],
        )
        envelope = {"to": [self._to_entry("ALIAS@EXAMPLE.COM")], "cc": []}
        self.assertEqual(ef._reply_from_identity(account, envelope), "Alias@Example.com")

    def test_missing_to_and_cc_falls_back_to_primary(self):
        account = self._FakeAccount("primary@example.com")
        self.assertEqual(ef._reply_from_identity(account, {}), "primary@example.com")

    def test_malformed_entries_are_skipped_without_erroring(self):
        account = self._FakeAccount("primary@example.com")
        envelope = {"to": ["not-a-dict", {"name": "No email"}], "cc": None}
        self.assertEqual(ef._reply_from_identity(account, envelope), "primary@example.com")


class TotalUnreadCount(unittest.TestCase):
    """_total_unread_count sums each account's Inbox unread count for
    the tray icon (see tray_icon.ZBoxTaskBarIcon.refresh and
    main_frame._refresh_tray_icon), from the same
    [(display_label, real_name, unread_count), ...] triples
    _ordered_folder_list builds."""

    def test_sums_inbox_across_accounts(self):
        folders_by_account = {
            "acct_a": [("Inbox", "INBOX", 3), ("Sent", "Sent", 0)],
            "acct_b": [("Inbox", "INBOX", 5)],
        }
        self.assertEqual(ef._total_unread_count(folders_by_account), 8)

    def test_none_unread_count_counts_as_zero_not_unknown(self):
        folders_by_account = {"acct_a": [("Inbox", "INBOX", None)]}
        self.assertEqual(ef._total_unread_count(folders_by_account), 0)

    def test_account_with_no_inbox_entry_contributes_zero(self):
        folders_by_account = {"acct_a": [("Sent", "Sent", 7)]}
        self.assertEqual(ef._total_unread_count(folders_by_account), 0)

    def test_non_inbox_folders_never_counted(self):
        folders_by_account = {
            "acct_a": [("Inbox", "INBOX", 1), ("Archive", "Archive", 99)],
        }
        self.assertEqual(ef._total_unread_count(folders_by_account), 1)

    def test_empty_or_missing_dict_is_zero(self):
        self.assertEqual(ef._total_unread_count({}), 0)
        self.assertEqual(ef._total_unread_count(None), 0)


class OrderedFolderList(unittest.TestCase):
    """The account tree's folder order. Gmail reports its inbox as
    "Inbox" while _folder_display_to_himalaya returns the universal
    "INBOX", and an exact match dropped it out of the special-folder
    loop into the alphabetical remainder -- below Sent, Drafts, Trash,
    Junk and Archive. The mapping returns "INBOX" for every provider,
    so this was never a Gmail-only bug."""

    class _Gmail:
        imap_host = "imap.gmail.com"

    class _Plain:
        imap_host = "imap.disroot.org"

    # The real folder names from a live 'mailbox list --counts', not
    # invented: debug log 2026-09-10 12:43:43, Gmail account.
    GMAIL_RAW = [
        "Inbox", "Personal", "Receipts",
        "[Gmail]/All Mail", "[Gmail]/Drafts", "[Gmail]/Important",
        "[Gmail]/Sent Mail", "[Gmail]/Spam", "[Gmail]/Starred",
        "[Gmail]/Trash",
    ]

    def _labels(self, pairs):
        return [label for label, _real, _unread in pairs]

    def test_gmail_inbox_comes_first(self):
        pairs = ef._ordered_folder_list(self._Gmail, self.GMAIL_RAW)
        self.assertEqual(pairs[0][0], "Inbox")

    def test_gmail_specials_are_in_folder_types_order(self):
        pairs = ef._ordered_folder_list(self._Gmail, self.GMAIL_RAW)
        self.assertEqual(
            self._labels(pairs)[:6],
            ["Inbox", "Sent", "Drafts", "Trash", "Junk", "Archive"],
        )

    def test_the_triple_carries_the_servers_own_spelling(self):
        # Not the mapped "INBOX" constant: every other call in the log
        # uses the name the server itself reported.
        pairs = ef._ordered_folder_list(self._Gmail, self.GMAIL_RAW)
        self.assertEqual(pairs[0][1], "Inbox")

    def test_custom_folders_follow_alphabetically(self):
        pairs = ef._ordered_folder_list(self._Gmail, self.GMAIL_RAW)
        self.assertEqual(
            self._labels(pairs)[6:],
            ["Personal", "Receipts", "[Gmail]/Important", "[Gmail]/Starred"],
        )

    def test_unread_counts_key_off_the_real_name(self):
        counts = {"Inbox": 333, "[Gmail]/Trash": 4}
        pairs = ef._ordered_folder_list(
            self._Gmail, self.GMAIL_RAW, unread_counts=counts
        )
        by_label = {label: unread for label, _real, unread in pairs}
        self.assertEqual(by_label["Inbox"], 333)
        self.assertEqual(by_label["Trash"], 4)
        self.assertIsNone(by_label["Sent"], "absent means unknown, not zero")

    def test_a_plain_providers_inbox_also_comes_first(self):
        raw = ["Archive", "Drafts", "Inbox", "Junk", "Sent", "Trash"]
        self.assertEqual(
            self._labels(ef._ordered_folder_list(self._Plain, raw)),
            ["Inbox", "Sent", "Drafts", "Trash", "Junk", "Archive"],
        )

    def test_an_uppercase_inbox_still_matches(self):
        pairs = ef._ordered_folder_list(self._Plain, ["INBOX", "Sent"])
        self.assertEqual(pairs[0], ("Inbox", "INBOX", None))

    def test_a_special_the_server_does_not_have_is_absent(self):
        pairs = ef._ordered_folder_list(self._Plain, ["Inbox", "Sent"])
        self.assertEqual(self._labels(pairs), ["Inbox", "Sent"])

    def test_an_empty_folder_list_is_empty_not_an_error(self):
        self.assertEqual(ef._ordered_folder_list(self._Plain, []), [])


if __name__ == "__main__":
    unittest.main()
