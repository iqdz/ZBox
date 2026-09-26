"""
Session aw: ZBox's own spelling marks in the Trix body, and the
signature fixes.

Spelling marks: Chromium's spell check in the editor stops once a
second WebView exists, so ZBox marks unknown words itself as a Trix
text attribute drawn as a <zbox-miss> tag. These tests pin the page
side, the marking logic in TrixBody, and that the tag never leaves
ZBox.

Signatures: a stale formatted signature no longer overrides the plain
box, Save before the editor loads keeps the stored signature, and
changing From rebuilds an untouched body with the new account's
signature.

No display: the WebView and the dialogs are replaced by small fakes.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import account_settings_dialog
import compose_body
import compose_panel
import signature_dialog
import spellcheck
import trix_html
import trix_page
from account_manager import Account


class FakeTimer:
    def __init__(self, func):
        self.func = func
        self.stopped = False

    def Stop(self):
        self.stopped = True


class FakeWeb:
    using_webview = True

    def __init__(self, answer=(True, "marked")):
        self.answer = answer
        self.scripts = []
        self.timers = []

    def later(self, delay_ms, func, *args):
        timer = FakeTimer(func)
        self.timers.append(timer)
        return timer

    def run_script_result(self, script, why):
        self.scripts.append((why, script))
        return self.answer


def make_trix_body(web):
    body = object.__new__(compose_body.TrixBody)
    body.web = web
    body._ready = True
    body._html = ""
    body._text = ""
    body._document = ""
    body._flush_waiting = []
    body._flush_timer = None
    body._loaded_text = None
    body._marked_document = None
    body._mark_timer = None
    body._mark_waits = 0
    return body


def small_dictionary(*words):
    dictionary = spellcheck.Dictionary()
    for rank, word in enumerate(words):
        dictionary.ranks[word] = rank
    dictionary.loaded = True
    return dictionary


class PageSide(unittest.TestCase):
    def test_chromium_spell_check_is_off_in_the_editor(self):
        self.assertIn('spellcheck="false"', trix_page.BODY)

    def test_setup_defines_the_mark_tag_and_attribute(self):
        self.assertIn("zbox-miss", trix_page.SETUP)
        self.assertIn("aria-invalid", trix_page.SETUP)
        self.assertIn("textAttributes.misspelled", trix_page.SETUP)

    def test_the_marks_are_visible(self):
        self.assertIn("zbox-miss", trix_page.PAGE_STYLE)

    def test_mark_markers_appear_once_each(self):
        self.assertEqual(trix_page.MARK_APPLY.count(trix_page.MARK_EXPECTED), 1)
        self.assertEqual(trix_page.MARK_APPLY.count(trix_page.MARK_RANGES), 1)

    def test_set_html_reports_its_load_separately_from_typing(self):
        self.assertIn("zbox_mirror('loaded')", trix_page.SET_HTML)

    def test_the_mirror_carries_the_document_string(self):
        self.assertIn("document: doc", trix_page.SETUP)


class MarksNeverLeave(unittest.TestCase):
    def test_the_tag_is_dropped_and_the_word_kept(self):
        html = trix_html.to_email_html(
            "<div>This <zbox-miss>sentense</zbox-miss> is fine.</div>"
        )
        self.assertNotIn("zbox-miss", html)
        self.assertIn("This sentense is fine.", html)


class Marking(unittest.TestCase):
    def setUp(self):
        self._saved = spellcheck.loaded_dictionary

    def tearDown(self):
        spellcheck.loaded_dictionary = self._saved

    def test_a_change_schedules_a_mark_run(self):
        web = FakeWeb()
        body = make_trix_body(web)
        body._content_arrived({"html": "", "text": "x", "document": "x\n"})
        self.assertEqual(len(web.timers), 1)

    def test_the_same_document_does_not_reschedule(self):
        web = FakeWeb()
        body = make_trix_body(web)
        body._marked_document = "x\n"
        body._content_arrived({"html": "", "text": "x", "document": "x\n"})
        self.assertEqual(web.timers, [])

    def test_unknown_words_are_marked_by_range(self):
        spellcheck.loaded_dictionary = lambda: small_dictionary("hello")
        web = FakeWeb()
        body = make_trix_body(web)
        body._document = "hello wrold\n"
        body._apply_marks()
        why, script = web.scripts[-1]
        self.assertEqual(why, "spelling_marks")
        self.assertIn("[[6, 11]]", script)
        self.assertIn('"hello wrold\\n"', script)
        self.assertEqual(body._marked_document, "hello wrold\n")

    def test_stale_text_is_left_for_the_next_pause(self):
        spellcheck.loaded_dictionary = lambda: small_dictionary("hello")
        web = FakeWeb(answer=(True, "stale"))
        body = make_trix_body(web)
        body._document = "hello wrold\n"
        body._apply_marks()
        self.assertIsNone(body._marked_document)

    def test_no_dictionary_waits_but_not_for_ever(self):
        spellcheck.loaded_dictionary = lambda: None
        saved_warm = spellcheck.warm
        spellcheck.warm = lambda personal_path=None: None
        try:
            web = FakeWeb()
            body = make_trix_body(web)
            body._document = "abc\n"
            for _ in range(compose_body.MARK_WAIT_LIMIT + 5):
                body._apply_marks()
            self.assertEqual(len(web.timers), compose_body.MARK_WAIT_LIMIT)
            self.assertEqual(web.scripts, [])
        finally:
            spellcheck.warm = saved_warm


class SpellMenu(unittest.TestCase):
    def test_the_marking_run_leaves_its_ranges_for_the_page(self):
        self.assertIn("window.zbox_miss=", trix_page.MARK_APPLY)

    def test_the_page_opens_zbox_suggestions_only_when_switched_on(self):
        self.assertIn("'contextmenu'", trix_page.SETUP)
        self.assertIn("zbox_spell_menu_on", trix_page.SETUP)
        self.assertIn("zbox_spell_menu_on=true", trix_page.SPELL_MENU_ON)

    def test_a_menu_request_is_scheduled_with_its_range(self):
        calls = []
        saved = getattr(compose_body.wx, "CallAfter", None)
        compose_body.wx.CallAfter = lambda func, *args: func(*args)
        try:
            body = make_trix_body(FakeWeb())
            body._on_spell_menu = lambda start, end: calls.append((start, end))
            body._on_message({"kind": "spell_menu", "start": 6, "end": 11})
        finally:
            if saved is None:
                del compose_body.wx.CallAfter
            else:
                compose_body.wx.CallAfter = saved
        self.assertEqual(calls, [(6, 11)])

    def test_a_bad_range_is_ignored(self):
        calls = []
        body = make_trix_body(FakeWeb())
        body._on_spell_menu = lambda start, end: calls.append((start, end))
        body._on_message({"kind": "spell_menu", "start": "x", "end": None})
        self.assertEqual(calls, [])

    def test_refresh_redoes_the_marks(self):
        web = FakeWeb()
        body = make_trix_body(web)
        body._marked_document = "abc\n"
        body.refresh_marks()
        self.assertIsNone(body._marked_document)
        self.assertEqual(len(web.timers), 1)


class LoadedDictionary(unittest.TestCase):
    def setUp(self):
        self._saved = (spellcheck._DICTIONARY, spellcheck._PERSONAL_HINT)

    def tearDown(self):
        spellcheck._DICTIONARY, spellcheck._PERSONAL_HINT = self._saved

    def test_nothing_until_loaded(self):
        spellcheck._DICTIONARY = None
        self.assertIsNone(spellcheck.loaded_dictionary())
        pending = spellcheck.Dictionary()
        spellcheck._DICTIONARY = pending
        self.assertIsNone(spellcheck.loaded_dictionary())

    def test_a_loaded_dictionary_is_returned(self):
        loaded = small_dictionary("word")
        spellcheck._DICTIONARY = loaded
        spellcheck._PERSONAL_HINT = None
        self.assertIs(spellcheck.loaded_dictionary(), loaded)


class Pristine(unittest.TestCase):
    def test_an_empty_body_with_nothing_loaded_is_pristine(self):
        body = make_trix_body(FakeWeb())
        self.assertTrue(body.is_pristine())

    def test_the_loaded_text_is_pristine_until_typing(self):
        body = make_trix_body(FakeWeb())
        body._content_arrived({"html": "", "text": "-- \nSig", "reason": "loaded"})
        self.assertTrue(body.is_pristine())
        body._content_arrived({"html": "", "text": "Hi\n-- \nSig", "reason": "mirror"})
        self.assertFalse(body.is_pristine())


class Field:
    def __init__(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def GetStringSelection(self):
        return self.value

    def SetValue(self, value):
        self.value = value


def make_settings_dialog(account, plain_now):
    dialog = object.__new__(account_settings_dialog.AccountSettingsDialog)
    dialog.account = account
    dialog._previous = {}
    dialog.identities = []
    dialog.signature_html = account.signature_html
    dialog._plain_synced = account.signature
    for name, value in (
        ("name_field", account.display_name),
        ("login_field", account.login_email),
        ("password_field", ""),
        ("identity_field", account.identity_email),
        ("imap_host_field", account.imap_host),
        ("imap_port_field", account.imap_port),
        ("imap_encryption_choice", account.imap_encryption),
        ("smtp_host_field", account.smtp_host),
        ("smtp_port_field", account.smtp_port),
        ("smtp_encryption_choice", account.smtp_encryption),
        ("signature_field", plain_now),
        ("junk_folder_field", ""),
        ("mute_account_checkbox", False),
        ("disable_account_checkbox", False),
        ("auto_check_checkbox", True),
    ):
        setattr(dialog, name, Field(value))
    return dialog


def signed_account():
    return Account(
        account_id="acct0001", display_name="Test", login_email="a@example.com",
        signature="Old sig", signature_html="<div>Old <b>sig</b></div>",
    )


class PlainSignatureBox(unittest.TestCase):
    def test_untouched_box_keeps_the_formatted_signature(self):
        account = signed_account()
        make_settings_dialog(account, "Old sig").apply_to_account()
        self.assertEqual(account.signature_html, "<div>Old <b>sig</b></div>")

    def test_edited_box_drops_the_stale_formatted_signature(self):
        account = signed_account()
        make_settings_dialog(account, "New sig").apply_to_account()
        self.assertEqual(account.signature, "New sig")
        self.assertEqual(account.signature_html, "")

    def test_emptied_box_means_no_signature(self):
        account = signed_account()
        make_settings_dialog(account, "").apply_to_account()
        self.assertEqual(account.signature, "")
        self.assertEqual(account.signature_html, "")


class FakeBody:
    def __init__(self, ready=True, html="", pristine=True):
        self.ready = ready
        self._html = html
        self._pristine = pristine
        self.set_calls = []

    def get_html(self):
        return self._html

    def is_pristine(self):
        return self._pristine

    def set_html(self, html):
        self.set_calls.append(html)


class SignatureEditorSave(unittest.TestCase):
    def make(self, body):
        dialog = object.__new__(signature_dialog.SignatureEditDialog)
        dialog.signature_html = "<div>Stored</div>"
        dialog.signature_text = "Stored"
        dialog._html_edited = False
        dialog.body = body
        dialog.text_field = Field("Stored")
        dialog.EndModal = lambda code: None
        return dialog

    def test_save_before_the_editor_loaded_keeps_the_stored_signature(self):
        dialog = self.make(FakeBody(ready=False, html=""))
        dialog._finish()
        self.assertEqual(dialog.signature_html, "<div>Stored</div>")

    def test_save_after_loading_reads_the_editor(self):
        dialog = self.make(FakeBody(ready=True, html="<div>Edited</div>"))
        dialog._finish()
        self.assertEqual(dialog.signature_html, "<div>Edited</div>")


class FakeEvent:
    def Skip(self):
        pass


def make_panel(body, accounts, selected):
    panel = object.__new__(compose_panel.ComposePanel)
    panel.body = body
    panel._opening_body = ""
    panel._signature_account_id = accounts[0].account_id
    panel._selected_account = lambda: selected
    panel.main_frame = type("MF", (), {})()
    panel.main_frame.account_manager = type("AM", (), {"accounts": accounts})()
    panel.spoken = []
    panel._say = panel.spoken.append
    return panel


class FromChange(unittest.TestCase):
    def accounts(self):
        plain = Account(account_id="acct_a", login_email="a@example.com")
        signed = Account(
            account_id="acct_b", login_email="b@example.com", signature="Bee",
        )
        return plain, signed

    def test_untouched_body_gets_the_new_signature(self):
        plain, signed = self.accounts()
        body = FakeBody(pristine=True)
        panel = make_panel(body, [plain, signed], signed)
        panel._on_from_changed(FakeEvent())
        self.assertEqual(len(body.set_calls), 1)
        self.assertIn("Bee", body.set_calls[0])
        self.assertEqual(panel._signature_account_id, "acct_b")
        self.assertEqual(len(panel.spoken), 1)

    def test_typed_body_is_left_alone_and_says_so(self):
        plain, signed = self.accounts()
        body = FakeBody(pristine=False)
        panel = make_panel(body, [plain, signed], signed)
        panel._on_from_changed(FakeEvent())
        self.assertEqual(body.set_calls, [])
        self.assertIn("Ctrl+Shift+S", panel.spoken[0])

    def test_same_signature_changes_nothing(self):
        plain, signed = self.accounts()
        other = Account(account_id="acct_c", login_email="c@example.com")
        body = FakeBody(pristine=True)
        panel = make_panel(body, [plain, other], other)
        panel._on_from_changed(FakeEvent())
        self.assertEqual(body.set_calls, [])
        self.assertEqual(panel.spoken, [])


if __name__ == "__main__":
    unittest.main()
