"""
Standalone secret-fetch script. Himalaya's config points password.command
at this script rather than embedding a password, so the raw secret only
ever exists encrypted on disk (data/config/secrets.dat) or in memory,
never inside himalaya.toml itself.

Usage: python get_secret.py <account_id>
Prints the stored password to stdout, nothing else, and exits
non-zero with a message on stderr if nothing is stored.

The lookup itself lives in lookup_password() so main.py's
--get-secret flag can share it. That flag is what himalaya.toml
actually points at now: under PyInstaller sys.executable is
ZBox.exe, so a command line of [sys.executable, get_secret.py, id]
launched a second copy of the GUI and returned no password, and
every account failed to authenticate in the built app while working
perfectly from source. Running this file directly still works and is
useful for checking a single account by hand.
"""

import os
import sys

import dpapi_secret_store

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(APP_DIR)
CONFIG_DIR = os.path.join(BASE_DIR, "data", "config")


def lookup_password(config_dir, account_id):
    """The stored password for an account, or None.

    Raises dpapi_secret_store.SecretStoreUnavailable if the store
    itself cannot be read; returning None means "nothing stored for
    this account", which is a different problem and gets a different
    message.
    """
    password = dpapi_secret_store.get_password(config_dir, account_id)

    if password is None:
        # Not found in the current store: check the pre-migration
        # OS credential store. If it's there, this is an account set
        # up before ZBox switched to its own encrypted file, or one
        # whose entry only exists in a different keyring backend
        # (e.g. saved from a Windows Python but looked up from a WSL
        # one) than the current process is using -- either way, once
        # we're actually able to read it, that's the version going
        # forward: copy it into the new store so this migration only
        # ever has to happen once per account.
        import legacy_keyring_store

        password = legacy_keyring_store.get_password(account_id)
        if password is not None:
            try:
                dpapi_secret_store.set_password(config_dir, account_id, password)
            except dpapi_secret_store.SecretStoreUnavailable:
                pass  # still return the password below even if the migration write failed

    return password


def main():
    if len(sys.argv) != 2:
        print("Usage: get_secret.py <account_id>", file=sys.stderr)
        sys.exit(1)

    account_id = sys.argv[1]

    try:
        password = lookup_password(CONFIG_DIR, account_id)
    except dpapi_secret_store.SecretStoreUnavailable as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    if password is None:
        print(f"No stored password for account {account_id}", file=sys.stderr)
        sys.exit(1)

    # No trailing newline, Himalaya reads stdout as the raw secret.
    sys.stdout.write(password)


if __name__ == "__main__":
    main()
