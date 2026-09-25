"""
unlock_methods -- which passkeys and security keys ZBox will offer,
and what it asks Windows for -- plus get_secret's lookup, the
password.command path Himalaya calls once per connection.

No real Windows Hello here. webauthn_unlock.register and
assert_identity are replaced with recorders, so these tests assert
the two things that can actually be wrong in this module: what ZBox
asks the authenticator for (platform and resident for a passkey,
cross-platform and non-resident for a security key), and what it
does with the answer. Whether Windows then shows a fingerprint
prompt is Windows' business and is not testable from here.

current_machine() is patched per test rather than left real, which
is what makes the carried-folder cases reachable: a passkey belongs
to one computer, and the interesting behaviour is all on the other
one.

Not covered: webauthn_unlock itself, which is ctypes against
webauthn.dll, and the GUI in unlock_dialog. Same accepted
real-OS-edge gap as the rest of the suite.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from base64 import b64encode

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import get_secret
import unlock_methods as um
import webauthn_unlock


THIS_MACHINE = "a" * 32
OTHER_MACHINE = "b" * 32


class _MethodsCase(unittest.TestCase):
    def setUp(self):
        self.config_dir = tempfile.mkdtemp(prefix="zbox_unlock_test_")
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        self.patch(um, "current_machine", lambda: THIS_MACHINE)
        self.patch(um, "master_password_set", lambda config_dir: True)

    def patch(self, module, name, replacement):
        original = getattr(module, name)
        setattr(module, name, replacement)
        self.addCleanup(setattr, module, name, original)

    # --- helpers ------------------------------------------------------

    def path(self):
        return os.path.join(self.config_dir, "unlock_methods.json")

    def file_text(self):
        if not os.path.isfile(self.path()):
            return None
        with open(self.path(), "r", encoding="utf-8") as handle:
            return handle.read()

    def stub_register(self, credential_id=b"cred-1", transport=16):
        """Records what ZBox asked Windows for.

        transport is an int, not a name: 16 is what Windows returned
        for a real Hello passkey on this machine, 2 for a USB
        security key. Nothing reads it back, but the fixtures in this
        suite are the shapes actually seen, not invented ones.
        """
        calls = []

        def fake(hwnd, rp_id, rp_name, user_id, label, display,
                 attachment=None, resident_key=False):
            calls.append({
                "rp_id": rp_id, "user_id": user_id,
                "attachment": attachment, "resident_key": resident_key,
            })
            return credential_id, transport

        self.patch(webauthn_unlock, "register", fake)
        return calls

    def write_methods(self, methods, user_handle="aGFuZGxl"):
        with open(self.path(), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "user_handle": user_handle,
                       "methods": methods}, handle)

    def a_passkey(self, machine=THIS_MACHINE, method_id="p1",
                  credential_id=b"cred-p"):
        return {
            "id": method_id, "kind": um.KIND_PASSKEY, "name": "Hello",
            "credential_id": b64encode(credential_id).decode("ascii"),
            "machine": machine, "registered": "2026-09-12 10:00",
        }

    def a_security_key(self, method_id="s1", credential_id=b"cred-s"):
        return {
            "id": method_id, "kind": um.KIND_SECURITY_KEY, "name": "Key",
            "credential_id": b64encode(credential_id).decode("ascii"),
            "machine": OTHER_MACHINE, "registered": "2026-09-12 10:00",
        }


# --- the gating rule --------------------------------------------------

class MasterPasswordGate(_MethodsCase):
    def test_no_master_password_means_no_registration_and_no_file(self):
        self.patch(um, "master_password_set", lambda config_dir: False)
        self.stub_register()
        with self.assertRaises(um.MasterPasswordRequired):
            um.register(None, self.config_dir, um.KIND_PASSKEY)
        self.assertIsNone(self.file_text())

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            um.register(None, self.config_dir, "retina-scan")


# --- what ZBox asks Windows for ---------------------------------------

class Registration(_MethodsCase):
    def test_a_passkey_is_platform_and_resident(self):
        calls = self.stub_register()
        um.register(None, self.config_dir, um.KIND_PASSKEY)
        self.assertEqual(
            calls[0]["attachment"], webauthn_unlock.ATTACHMENT_PLATFORM
        )
        self.assertTrue(calls[0]["resident_key"])

    def test_a_security_key_is_cross_platform_and_not_resident(self):
        # Non-resident on purpose: most older keys have no room for
        # discoverable credentials.
        calls = self.stub_register(transport=2)
        um.register(None, self.config_dir, um.KIND_SECURITY_KEY)
        self.assertEqual(
            calls[0]["attachment"],
            webauthn_unlock.ATTACHMENT_CROSS_PLATFORM,
        )
        self.assertFalse(calls[0]["resident_key"])

    def test_what_gets_recorded(self):
        self.stub_register(credential_id=b"cred-xyz")
        method = um.register(None, self.config_dir, um.KIND_PASSKEY)
        self.assertEqual(method["kind"], um.KIND_PASSKEY)
        self.assertEqual(method["machine"], THIS_MACHINE)
        self.assertEqual(
            method["credential_id"], b64encode(b"cred-xyz").decode("ascii")
        )
        self.assertTrue(method["registered"])
        self.assertEqual(method["name"], um.default_name(um.KIND_PASSKEY))
        self.assertEqual(len(um.list_methods(self.config_dir)), 1)

    def test_the_user_handle_is_made_once_and_kept(self):
        calls = self.stub_register()
        um.register(None, self.config_dir, um.KIND_PASSKEY, name="one")
        um.register(None, self.config_dir, um.KIND_SECURITY_KEY, name="two")
        self.assertEqual(calls[0]["user_id"], calls[1]["user_id"])


# --- which methods this computer may offer ----------------------------

class UsableHere(_MethodsCase):
    def test_a_security_key_travels(self):
        self.assertTrue(um.is_usable_here(self.a_security_key()))

    def test_a_passkey_from_this_machine_is_usable(self):
        self.assertTrue(um.is_usable_here(self.a_passkey()))

    def test_a_passkey_from_another_machine_is_not(self):
        self.assertFalse(um.is_usable_here(self.a_passkey(OTHER_MACHINE)))

    def test_a_passkey_with_no_machine_recorded_is_adopted(self):
        # Refusing to offer a method that might work is worse than
        # offering one that turns out not to.
        self.assertTrue(um.is_usable_here(self.a_passkey(machine="")))

    def test_usable_methods_filters_by_kind_and_by_machine(self):
        self.write_methods([
            self.a_passkey(),
            self.a_passkey(OTHER_MACHINE, method_id="p2"),
            self.a_security_key(),
        ])
        passkeys = um.usable_methods(self.config_dir, um.KIND_PASSKEY)
        self.assertEqual([m["id"] for m in passkeys], ["p1"])
        self.assertEqual(len(um.usable_methods(self.config_dir)), 2)


class ForeignPasskeys(_MethodsCase):
    def setUp(self):
        super().setUp()
        self.write_methods([
            self.a_passkey(),
            self.a_passkey(OTHER_MACHINE, method_id="p2"),
            self.a_security_key(),
        ])

    def test_only_the_other_machines_passkeys_are_listed(self):
        self.assertEqual(
            [m["id"] for m in um.foreign_passkeys(self.config_dir)], ["p2"]
        )

    def test_forgetting_them_keeps_everything_else(self):
        self.assertEqual(um.forget_foreign_passkeys(self.config_dir), 1)
        left = {m["id"] for m in um.list_methods(self.config_dir)}
        self.assertEqual(left, {"p1", "s1"})

    def test_nothing_to_drop_rewrites_nothing(self):
        um.forget_foreign_passkeys(self.config_dir)
        before = self.file_text()
        self.assertEqual(um.forget_foreign_passkeys(self.config_dir), 0)
        self.assertEqual(self.file_text(), before)


# --- the carried-folder repair ----------------------------------------

class Reassign(_MethodsCase):
    def test_it_registers_here_and_drops_the_stale_ones(self):
        self.write_methods([self.a_passkey(OTHER_MACHINE, method_id="p2")])
        self.stub_register(credential_id=b"cred-new")
        method, dropped = um.reassign_passkey(None, self.config_dir)
        self.assertEqual(dropped, 1)
        self.assertEqual(method["machine"], THIS_MACHINE)
        self.assertEqual(
            [m["id"] for m in um.list_methods(self.config_dir)], [method["id"]]
        )

    def test_a_cancelled_prompt_changes_nothing(self):
        # Registration runs first for exactly this reason: a mistyped
        # PIN must not cost the passkey it was meant to replace.
        self.write_methods([self.a_passkey(OTHER_MACHINE, method_id="p2")])
        before = self.file_text()

        def cancelled(*args, **kwargs):
            raise webauthn_unlock.WebAuthnCancelled("user dismissed it")

        self.patch(webauthn_unlock, "register", cancelled)
        with self.assertRaises(webauthn_unlock.WebAuthnCancelled):
            um.reassign_passkey(None, self.config_dir)
        self.assertEqual(self.file_text(), before)


# --- verify and removal -----------------------------------------------

class Verify(_MethodsCase):
    def stub_assert(self, answer):
        seen = []

        def fake(hwnd, rp_id, credential_ids, attachment=None):
            seen.append(list(credential_ids))
            return answer

        self.patch(webauthn_unlock, "assert_identity", fake)
        return seen

    def test_nothing_registered_raises(self):
        self.stub_assert(b"cred-p")
        with self.assertRaises(um.NoUnlockMethods):
            um.verify(None, self.config_dir)

    def test_only_another_machines_passkey_is_nothing_to_offer(self):
        self.write_methods([self.a_passkey(OTHER_MACHINE, method_id="p2")])
        self.stub_assert(b"cred-p")
        with self.assertRaises(um.NoUnlockMethods):
            um.verify(None, self.config_dir)

    def test_only_usable_credentials_are_offered_and_the_answer_maps_back(self):
        self.write_methods([
            self.a_passkey(),
            self.a_passkey(OTHER_MACHINE, method_id="p2",
                           credential_id=b"cred-far"),
        ])
        seen = self.stub_assert(b"cred-p")
        method = um.verify(None, self.config_dir, um.KIND_PASSKEY)
        self.assertEqual(seen[0], [b"cred-p"])
        self.assertEqual(method["id"], "p1")


class Removal(_MethodsCase):
    def test_removing_a_known_method(self):
        self.write_methods([self.a_passkey(), self.a_security_key()])
        self.assertTrue(um.remove(self.config_dir, "p1"))
        self.assertEqual(
            [m["id"] for m in um.list_methods(self.config_dir)], ["s1"]
        )

    def test_removing_something_that_is_not_there(self):
        self.write_methods([self.a_passkey()])
        self.assertFalse(um.remove(self.config_dir, "nope"))
        self.assertEqual(len(um.list_methods(self.config_dir)), 1)


class UnreadableFile(_MethodsCase):
    def test_a_corrupt_file_reads_as_empty_rather_than_crashing(self):
        with open(self.path(), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(um.list_methods(self.config_dir), [])
        self.assertFalse(um.has_methods(self.config_dir))


# --- get_secret's lookup, the password.command path -------------------

class LookupPassword(unittest.TestCase):
    def setUp(self):
        self.config_dir = tempfile.mkdtemp(prefix="zbox_lookup_test_")
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        import legacy_keyring_store

        self.legacy = legacy_keyring_store
        self.written = []

    def patch(self, module, name, replacement):
        original = getattr(module, name)
        setattr(module, name, replacement)
        self.addCleanup(setattr, module, name, original)

    def stub_store(self, stored=None, set_raises=None):
        def get_password(config_dir, account_id):
            return stored

        def set_password(config_dir, account_id, password):
            if set_raises is not None:
                raise set_raises
            self.written.append((account_id, password))

        self.patch(get_secret.dpapi_secret_store, "get_password", get_password)
        self.patch(get_secret.dpapi_secret_store, "set_password", set_password)

    def test_what_the_store_holds_is_returned(self):
        self.stub_store(stored="hunter2")
        self.assertEqual(
            get_secret.lookup_password(self.config_dir, "acct-1"), "hunter2"
        )

    def test_a_legacy_keyring_entry_is_returned_and_migrated_once(self):
        self.stub_store(stored=None)
        self.patch(self.legacy, "get_password", lambda account_id: "old-pass")
        self.assertEqual(
            get_secret.lookup_password(self.config_dir, "acct-1"), "old-pass"
        )
        self.assertEqual(self.written, [("acct-1", "old-pass")])

    def test_a_failed_migration_still_returns_the_password(self):
        # Himalaya is waiting on stdout; failing to write the new
        # store is not a reason to fail the connection.
        unavailable = get_secret.dpapi_secret_store.SecretStoreUnavailable(
            "no pywin32"
        )
        self.stub_store(stored=None, set_raises=unavailable)
        self.patch(self.legacy, "get_password", lambda account_id: "old-pass")
        self.assertEqual(
            get_secret.lookup_password(self.config_dir, "acct-1"), "old-pass"
        )

    def test_nothing_anywhere_is_none(self):
        self.stub_store(stored=None)
        self.patch(self.legacy, "get_password", lambda account_id: None)
        self.assertIsNone(
            get_secret.lookup_password(self.config_dir, "acct-1")
        )


if __name__ == "__main__":
    unittest.main()
