"""
data_crypto's fail-closed contract.

Task 3 inverted Task 2 on purpose: the startup lock fails OPEN so a
bug cannot shut someone out of their own mail, and this module fails
CLOSED so a locked folder is never mistaken for an empty one. That
second half is the dangerous one to get wrong, and it is what most
of this file tests. A reader that turns "cannot decrypt" into "no
accounts configured" looks like a fresh install, and the next save
writes an empty file over data that was only locked.

dpapi_secret_store.file_key is stubbed to hand back a fixed 32-byte
key, so these exercise data_crypto's own logic against the real
AES-GCM in win_crypto rather than a pretend cipher. The four tests
that need real AES skip themselves when bcrypt.dll is not reachable
(the Linux wxstub harness); the seven fail-closed tests need no
crypto at all and always run.

Not covered here, same accepted gap as the rest of the suite: the
DPAPI and keyset machinery inside dpapi_secret_store itself, which
needs a real Windows user profile to mean anything.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import data_crypto
import dpapi_secret_store
import win_crypto


TEST_KEY = bytes(range(32))
TEST_KEYSET = "test-keyset"

HAVE_AES = win_crypto.is_available()
NEEDS_AES = unittest.skipUnless(
    HAVE_AES, "bcrypt.dll is not reachable on this machine"
)


class _CryptoCase(unittest.TestCase):
    """Gives every test its own folder and a working file key."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="zbox_crypto_test_")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.config_dir = self.folder
        self.path = os.path.join(self.folder, "accounts.json")
        self._install_key(lambda config_dir, keyset=None:
                          (TEST_KEYSET, TEST_KEY))

    def _install_key(self, replacement):
        original = dpapi_secret_store.file_key
        dpapi_secret_store.file_key = replacement
        self.addCleanup(setattr, dpapi_secret_store, "file_key", original)

    def _lock_the_key(self, error=None):
        """Makes file_key fail the way a locked machine makes it
        fail: no master password entered yet, so no data key."""
        failure = error or dpapi_secret_store.SecretsLocked(
            "The secret store is locked."
        )

        def locked(config_dir, keyset=None):
            raise failure

        self._install_key(locked)

    def write_plain(self, text, path=None):
        target = path or self.path
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
        return target


# --- the fail-closed contract, no AES needed --------------------------

class MissingFile(_CryptoCase):
    def test_default_is_returned_only_when_the_file_is_absent(self):
        self.assertIsNone(data_crypto.read_json(self.config_dir, self.path))
        self.assertEqual(
            data_crypto.read_json(self.config_dir, self.path, default={}),
            {},
        )


class LockedFolder(_CryptoCase):
    """The one that matters. A file that exists and cannot be opened
    must raise, never fall back to the default."""

    def setUp(self):
        super().setUp()
        self.write_plain(json.dumps({
            data_crypto._MAGIC: 1,
            "keyset": TEST_KEYSET,
            "nonce": "AAAAAAAAAAAAAAAA",
            "blob": "AAAAAAAAAAAAAAAAAAAAAAAA",
        }))

    def test_locked_store_raises_instead_of_returning_a_default(self):
        self._lock_the_key()
        with self.assertRaises(data_crypto.EncryptedDataUnavailable):
            data_crypto.read_json(self.config_dir, self.path, default={})

    def test_missing_keyset_raises_too(self):
        self._lock_the_key(dpapi_secret_store.KeysetMissing("no keyset"))
        with self.assertRaises(data_crypto.EncryptedDataUnavailable):
            data_crypto.read_json(self.config_dir, self.path, default=[])

    def test_no_secret_store_at_all_raises_too(self):
        self._lock_the_key(
            dpapi_secret_store.SecretStoreUnavailable("no pywin32")
        )
        with self.assertRaises(data_crypto.EncryptedDataUnavailable):
            data_crypto.read_json(self.config_dir, self.path, default={})


class PlaintextAndCorrupt(_CryptoCase):
    def test_plaintext_json_is_passed_straight_through(self):
        # An install from before encryption existed still has to load.
        self.write_plain(json.dumps({"accounts": [{"id": "a"}]}))
        self.assertEqual(
            data_crypto.read_json(self.config_dir, self.path),
            {"accounts": [{"id": "a"}]},
        )

    def test_not_json_at_all_raises_value_error_not_ours(self):
        # A corrupt file is a different problem with a different
        # answer, and belongs to the caller's own handling.
        self.write_plain("this is not json {")
        with self.assertRaises(ValueError) as caught:
            data_crypto.read_json(self.config_dir, self.path)
        self.assertNotIsInstance(
            caught.exception, data_crypto.EncryptedDataUnavailable
        )


class ContainerShape(_CryptoCase):
    def test_a_newer_format_version_is_refused(self):
        with self.assertRaises(data_crypto.EncryptedDataUnavailable):
            data_crypto.decrypt_bytes(self.config_dir, self.path, {
                data_crypto._MAGIC: 99,
                "keyset": TEST_KEYSET,
                "nonce": "AAAAAAAAAAAAAAAA",
                "blob": "AAAAAAAAAAAAAAAAAAAAAAAA",
            })

    def test_something_that_is_not_a_container_is_refused(self):
        with self.assertRaises(data_crypto.EncryptedDataUnavailable):
            data_crypto.decrypt_bytes(
                self.config_dir, self.path, {"accounts": []}
            )


class EncryptInPlaceRefusals(_CryptoCase):
    def test_missing_file_converts_nothing(self):
        self.assertFalse(
            data_crypto.encrypt_in_place(self.config_dir, self.path)
        )

    def test_unparseable_file_is_left_exactly_as_it_was(self):
        self.write_plain("not json at all")
        self.assertFalse(
            data_crypto.encrypt_in_place(self.config_dir, self.path)
        )
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "not json at all")

    def test_an_already_encrypted_file_is_not_converted_twice(self):
        self.write_plain(json.dumps({
            data_crypto._MAGIC: 1,
            "keyset": TEST_KEYSET,
            "nonce": "AAAAAAAAAAAAAAAA",
            "blob": "AAAAAAAAAAAAAAAAAAAAAAAA",
        }))
        self.assertFalse(
            data_crypto.encrypt_in_place(self.config_dir, self.path)
        )


# --- the real cipher --------------------------------------------------

@NEEDS_AES
class RoundTrip(_CryptoCase):
    def test_what_goes_in_comes_back_out(self):
        value = {
            "accounts": [
                {"id": "a1", "address": "person@example.com",
                 "display": "Zoë Ångström"},
                {"id": "a2", "folders": {"junk": "[Gmail]/Spam"}},
            ],
            "count": 2,
        }
        data_crypto.write_json(self.config_dir, self.path, value)
        self.assertEqual(
            data_crypto.read_json(self.config_dir, self.path), value
        )

    def test_the_file_on_disk_is_a_container_not_readable_json(self):
        data_crypto.write_json(
            self.config_dir, self.path, {"password": "hunter2"}
        )
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("hunter2", raw)
        self.assertTrue(data_crypto.is_encrypted(self.path))
        self.assertTrue(
            data_crypto.is_encrypted_container(json.loads(raw))
        )

    def test_no_temp_file_is_left_behind(self):
        data_crypto.write_json(self.config_dir, self.path, {"a": 1})
        self.assertFalse(os.path.exists(self.path + ".new"))
        self.assertEqual(os.listdir(self.folder), ["accounts.json"])


@NEEDS_AES
class Tamper(_CryptoCase):
    def test_a_ciphertext_moved_to_another_filename_does_not_open(self):
        # The filename is the AAD, so a blob lifted out of
        # accounts.json must not decrypt as contacts.json.
        data_crypto.write_json(self.config_dir, self.path, {"a": 1})
        with open(self.path, "r", encoding="utf-8") as handle:
            container = json.load(handle)
        other = os.path.join(self.folder, "contacts.json")
        with self.assertRaises(data_crypto.EncryptedDataUnavailable):
            data_crypto.decrypt_bytes(self.config_dir, other, container)

    def test_one_flipped_byte_raises_rather_than_returning_garbage(self):
        import base64

        data_crypto.write_json(self.config_dir, self.path, {"a": 1})
        with open(self.path, "r", encoding="utf-8") as handle:
            container = json.load(handle)
        blob = bytearray(base64.b64decode(container["blob"]))
        blob[0] ^= 0x01
        container["blob"] = base64.b64encode(bytes(blob)).decode("ascii")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(container, handle)

        with self.assertRaises(data_crypto.EncryptedDataUnavailable):
            data_crypto.read_json(self.config_dir, self.path, default={})


if __name__ == "__main__":
    unittest.main()
