"""
dpapi_secret_store's unlock path -- the one place a bug locks someone
out of their own mail.

Everything here runs against a real temp config folder with real
DPAPI and real AES-GCM. There is no useful stub for either: DPAPI's
whole behaviour is "this machine and this Windows account, nothing
else", and faking it would test the fake. The file skips itself
whole when pywin32 or bcrypt.dll is not reachable.

The second-machine case is simulated honestly rather than avoided.
A folder carried to another computer arrives with DPAPI wraps that
cannot be opened there and a machines list that does not name it, so
_other_machine() rewrites secrets.dat into exactly that state: wraps
emptied, machines set to a fingerprint that is not this one. That is
the state the master password exists for, and it is reachable here
without a second computer.

Nothing in this file touches the real data folder; every test gets
its own tempfile.mkdtemp and removes it afterwards.

Not covered, deliberately: machine_id() itself, which reads
MachineGuid out of the registry, same accepted gap as every other
real-OS edge in this suite.
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

import dpapi_secret_store as store
import win_crypto


RUNNABLE = store.DPAPI_AVAILABLE and win_crypto.is_available()
NOT_RUNNABLE = "needs pywin32 and bcrypt.dll, i.e. real Windows"

MASTER = "correct horse battery staple"
WRONG = "correct horse battery stapler"


@unittest.skipUnless(RUNNABLE, NOT_RUNNABLE)
class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.config_dir = tempfile.mkdtemp(prefix="zbox_secrets_test_")
        self.addCleanup(shutil.rmtree, self.config_dir, True)

    # --- helpers ------------------------------------------------------

    def path(self):
        return os.path.join(self.config_dir, "secrets.dat")

    def read_store(self):
        with open(self.path(), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def write_store(self, data):
        with open(self.path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def other_machine(self):
        """Puts the folder in the state it would arrive in on a second
        computer: no DPAPI wrap this machine can open, and a machines
        list that does not include this one."""
        data = self.read_store()
        for keyset in data.get("keysets", []):
            keyset.setdefault("wraps", {})["dpapi"] = []
            keyset["machines"] = ["0" * 32]
        self.write_store(data)


# --- round trip and binding -------------------------------------------

class RoundTrip(_StoreCase):
    def test_a_stored_password_comes_back(self):
        store.set_password(self.config_dir, "acct-1", "hunter2")
        self.assertEqual(
            store.get_password(self.config_dir, "acct-1"), "hunter2"
        )

    def test_unicode_survives_the_round_trip(self):
        store.set_password(self.config_dir, "acct-1", "påsswörd–ünïcode")
        self.assertEqual(
            store.get_password(self.config_dir, "acct-1"), "påsswörd–ünïcode"
        )

    def test_an_unknown_account_is_none_not_an_error(self):
        store.set_password(self.config_dir, "acct-1", "hunter2")
        self.assertIsNone(store.get_password(self.config_dir, "acct-2"))

    def test_a_secret_moved_between_accounts_does_not_open(self):
        # The account id is the AAD. A blob copied from one entry to
        # another has to fail its tag rather than hand back the wrong
        # account's password.
        store.set_password(self.config_dir, "acct-1", "first-password")
        store.set_password(self.config_dir, "acct-2", "second-password")
        data = self.read_store()
        keyset = data["keysets"][0]
        keyset["accounts"]["acct-2"] = keyset["accounts"]["acct-1"]
        self.write_store(data)
        self.assertIsNone(store.get_password(self.config_dir, "acct-2"))
        self.assertEqual(
            store.get_password(self.config_dir, "acct-1"), "first-password"
        )

    def test_delete_removes_it_from_every_keyset(self):
        store.set_password(self.config_dir, "acct-1", "hunter2")
        store.delete_password(self.config_dir, "acct-1")
        self.assertIsNone(store.get_password(self.config_dir, "acct-1"))
        for keyset in self.read_store()["keysets"]:
            self.assertNotIn("acct-1", keyset.get("accounts", {}))


# --- the master password ----------------------------------------------

class MasterPassword(_StoreCase):
    def test_an_empty_master_password_is_refused(self):
        store.set_password(self.config_dir, "acct-1", "hunter2")
        with self.assertRaises(ValueError):
            store.set_master_password(self.config_dir, "")
        self.assertFalse(store.has_master_password(self.config_dir))

    def test_verify_accepts_the_right_one_and_refuses_the_wrong_one(self):
        store.set_password(self.config_dir, "acct-1", "hunter2")
        store.set_master_password(self.config_dir, MASTER)
        self.assertTrue(
            store.verify_master_password(self.config_dir, MASTER)
        )
        self.assertFalse(
            store.verify_master_password(self.config_dir, WRONG)
        )

    def test_set_before_any_account_covers_the_first_one_added(self):
        # The first-run order the EULA gate builds: master password
        # first, account afterwards.
        store.set_master_password(self.config_dir, MASTER)
        self.assertTrue(store.has_master_password(self.config_dir))
        store.set_password(self.config_dir, "acct-1", "hunter2")
        self.assertEqual(
            store.get_password(self.config_dir, "acct-1"), "hunter2"
        )
        self.assertTrue(store.status(self.config_dir)["master_set"])

    def test_clearing_it_removes_it(self):
        store.set_password(self.config_dir, "acct-1", "hunter2")
        store.set_master_password(self.config_dir, MASTER)
        self.assertTrue(
            store.clear_master_password(self.config_dir, MASTER)
        )
        self.assertFalse(store.has_master_password(self.config_dir))
        # The folder still works here, which is the whole trade.
        self.assertEqual(
            store.get_password(self.config_dir, "acct-1"), "hunter2"
        )


# --- arriving on another computer -------------------------------------

class SecondMachine(_StoreCase):
    def setUp(self):
        super().setUp()
        store.set_password(self.config_dir, "acct-1", "hunter2")
        store.set_master_password(self.config_dir, MASTER)

    def test_locked_reads_as_none_not_as_a_crash(self):
        self.other_machine()
        self.assertIsNone(store.get_password(self.config_dir, "acct-1"))

    def test_status_says_locked_and_offers_the_master_password(self):
        self.other_machine()
        state = store.status(self.config_dir)
        self.assertTrue(state["secrets_present"])
        self.assertGreaterEqual(state["locked"], 1)
        self.assertTrue(state["master_available"])
        self.assertTrue(state["new_machine"])

    def test_the_right_master_password_opens_it_and_keeps_it_open(self):
        self.other_machine()
        self.assertTrue(store.unlock_with_master(self.config_dir, MASTER))
        self.assertEqual(
            store.get_password(self.config_dir, "acct-1"), "hunter2"
        )
        # A DPAPI wrap for this machine was added, so nothing prompts
        # again here.
        self.assertEqual(store.status(self.config_dir)["locked"], 0)

    def test_the_wrong_master_password_raises_and_changes_nothing(self):
        self.other_machine()
        with self.assertRaises(store.WrongMasterPassword):
            store.unlock_with_master(self.config_dir, WRONG)
        self.assertGreaterEqual(store.status(self.config_dir)["locked"], 1)
        self.assertIsNone(store.get_password(self.config_dir, "acct-1"))


class SecondMachineWithNoMaster(_StoreCase):
    def test_no_master_wrap_means_no_prompt_is_offered(self):
        # Offering the master password dialog here would be a dead
        # end: there is nothing for it to open.
        store.set_password(self.config_dir, "acct-1", "hunter2")
        self.other_machine()
        state = store.status(self.config_dir)
        self.assertGreaterEqual(state["locked"], 1)
        self.assertFalse(state["master_available"])
        self.assertFalse(state["master_set"])


# --- the store file itself --------------------------------------------

class CorruptFile(_StoreCase):
    def test_an_unreadable_store_is_treated_as_empty(self):
        os.makedirs(self.config_dir, exist_ok=True)
        with open(self.path(), "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        state = store.status(self.config_dir)
        self.assertFalse(state["secrets_present"])
        self.assertEqual(state["locked"], 0)
        # And a fresh password can still be written over it.
        store.set_password(self.config_dir, "acct-1", "hunter2")
        self.assertEqual(
            store.get_password(self.config_dir, "acct-1"), "hunter2"
        )


class FileKeys(_StoreCase):
    def test_the_same_keyset_gives_the_same_key_back(self):
        keyset_id, key = store.file_key(self.config_dir)
        self.assertIsInstance(keyset_id, str)
        self.assertTrue(keyset_id)
        self.assertEqual(len(key), win_crypto.KEY_SIZE)
        again_id, again_key = store.file_key(self.config_dir, keyset_id)
        self.assertEqual(again_id, keyset_id)
        self.assertEqual(again_key, key)

    def test_an_unknown_keyset_is_missing_not_locked(self):
        # Telling the user their key is gone is the honest answer;
        # reporting it as a lock invites retyping a password forever.
        store.file_key(self.config_dir)
        with self.assertRaises(store.KeysetMissing):
            store.file_key(self.config_dir, "0123456789abcdef")

    def test_a_locked_keyset_raises_secrets_locked(self):
        keyset_id, _ = store.file_key(self.config_dir)
        self.other_machine()
        with self.assertRaises(store.SecretsLocked):
            store.file_key(self.config_dir, keyset_id)


if __name__ == "__main__":
    unittest.main()
