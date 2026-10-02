"""
LEGACY password store: the OS-wide credential store (Windows
Credential Manager) via the keyring package. Superseded by
dpapi_secret_store.py, which encrypts passwords into a file inside
ZBox's own portable config folder instead of a separate per-machine,
per-Windows-user system store.

Kept only so get_secret.py can still find (and migrate) a password
saved by a ZBox install from before that switch. New passwords are
never written here.
"""

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

SERVICE_NAME = "ZBox"


def get_password(account_id):
    if not KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, account_id)
    except Exception:
        # No keyring backend available at all (e.g. run from a
        # Linux/WSL Python with no Secret Service running) -- this
        # is only ever consulted as a migration fallback, so treat
        # "can't check" the same as "nothing there" rather than
        # crashing the whole password lookup.
        return None


def delete_password(account_id):
    if not KEYRING_AVAILABLE:
        return
    try:
        keyring.delete_password(SERVICE_NAME, account_id)
    except Exception:
        pass  # nothing stored for this account (or no backend at all)
