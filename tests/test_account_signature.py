"""
Account.signature (audit finding 41) -- per-account signature text,
edited in Account Settings, persisted alongside every other account
field. This only covers Account.to_dict/from_dict; the actual append
into a composed body is envelope_format._append_signature, tested in
test_envelope_format.py, and the dialog widget is real wx so it isn't
unit-tested here (same limitation already hit by ServerPage in
test_provider_presets.py).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from account_manager import Account


class AccountSignature(unittest.TestCase):
    def test_defaults_to_empty(self):
        account = Account(display_name="Harith", login_email="h@example.com")
        self.assertEqual(account.signature, "")

    def test_round_trips_through_to_dict_and_from_dict(self):
        account = Account(
            display_name="Harith",
            login_email="h@example.com",
            signature="Harith\nSent from ZBox",
        )
        restored = Account.from_dict(account.to_dict())
        self.assertEqual(restored.signature, "Harith\nSent from ZBox")

    def test_from_dict_defaults_missing_signature_to_empty(self):
        # An accounts.json entry saved before this feature existed has
        # no "signature" key at all.
        account = Account.from_dict({
            "account_id": "acct_old",
            "display_name": "Harith",
            "login_email": "h@example.com",
        })
        self.assertEqual(account.signature, "")

    def test_to_dict_never_includes_the_password(self):
        # Not new behaviour, but worth pinning down alongside the new
        # field: signature is plain text saved to accounts.json, the
        # password never is (see AccountManager.save_accounts).
        account = Account(login_email="h@example.com", password="hunter2", signature="hi")
        self.assertNotIn("password", account.to_dict())


class AccountSignatureHtml(unittest.TestCase):
    """The formatted signature written by app/signature_dialog.py,
    stored beside the plain one rather than replacing it. Empty means
    there is no formatted version and everything falls back to
    converting the plain text, which is what makes this field need no
    migration."""

    def test_defaults_to_empty(self):
        account = Account(display_name="Harith", login_email="h@example.com")
        self.assertEqual(account.signature_html, "")

    def test_round_trips_through_to_dict_and_from_dict(self):
        account = Account(
            display_name="Harith",
            login_email="h@example.com",
            signature="Harith\nSent from ZBox",
            signature_html="<p><b>Harith</b><br>Sent from ZBox</p>",
        )
        restored = Account.from_dict(account.to_dict())
        self.assertEqual(restored.signature, "Harith\nSent from ZBox")
        self.assertEqual(
            restored.signature_html, "<p><b>Harith</b><br>Sent from ZBox</p>"
        )

    def test_from_dict_defaults_missing_signature_html_to_empty(self):
        # An accounts.json entry saved before the signature editor
        # existed has the plain signature and no HTML one.
        account = Account.from_dict({
            "account_id": "acct_old",
            "display_name": "Harith",
            "login_email": "h@example.com",
            "signature": "Harith",
        })
        self.assertEqual(account.signature, "Harith")
        self.assertEqual(account.signature_html, "")

    def test_none_is_stored_as_empty(self):
        account = Account(login_email="h@example.com", signature_html=None)
        self.assertEqual(account.signature_html, "")


if __name__ == "__main__":
    unittest.main()
