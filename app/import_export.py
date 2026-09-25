"""
Import/export of ZBox accounts and settings as one portable JSON file
(audit finding 54), so moving to a new machine -- or recovering from a
wipe -- does not mean redoing the account wizard for every account.

Passwords are never included in an export. Account.to_dict() (see
account_manager.py) already omits the password field entirely, and
the export file is plain JSON the user is expected to carry around,
which is precisely where a password should not be. Moving the
secrets is the other feature's job: copy the whole ZBox folder and
unlock it with the master password (dpapi_secret_store), which keeps
them encrypted the entire way across.
Every imported account therefore needs its password entered once via
Accounts > Account Settings before it can connect: the wizard is
skipped, authentication is not.

Import is purely additive. Each imported account is given a brand
new account_id (the exported one is discarded), so importing the same
file twice just adds duplicates rather than risking a silent
overwrite of an account already configured on the target machine --
there is no merge/collision logic to get wrong.

Only the "preference" half of Settings travels. Window position,
splitter sash positions, and the last-selected account/folder (audit
finding 52) are specific to the machine and screen layout they were
saved on; PORTABLE_SETTINGS_FIELDS below is the explicit allowlist of
what does travel, so a future settings field defaults to NOT being
exported until someone decides it belongs on this list.
"""

import json


FORMAT_VERSION = 1

PORTABLE_SETTINGS_FIELDS = [
    "debug_logging",
    "unified_folder_types",
    "default_message_view",
    "html_silence_hold_ms",
    "folder_pane_mode",
    "webview_focus_delay_level",
    "webview_backend",
    "webview2_runtime",
    "block_remote_content",
    "blocklist_enabled",
    "blocklist_auto_update",
    "remote_content_allowed_senders",
    "sort_key",
    "sort_ascending",
    "announcement_hold_ms",
    "show_message_headers",
    "attachment_save_dir",
    "bare_key_shortcuts_enabled",
    "draft_autosave_seconds",
    "sounds_enabled",
    "sound_theme",
    "reading_font_size",
    "reading_font_family",
]


def build_export_data(accounts, settings):
    """Returns the plain-dict structure written to the export file:
    every account (to_dict() already omits the password) plus the
    portable subset of Settings."""
    settings_dict = settings.to_dict()
    portable_settings = {
        key: settings_dict[key]
        for key in PORTABLE_SETTINGS_FIELDS
        if key in settings_dict
    }
    return {
        "zbox_export_version": FORMAT_VERSION,
        "accounts": [account.to_dict() for account in accounts],
        "settings": portable_settings,
    }


def write_export_file(path, accounts, settings):
    data = build_export_data(accounts, settings)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    return data


def read_export_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def accounts_from_export_data(data):
    """Builds fresh Account objects from export data, always with a
    new account_id (see module docstring) -- the caller is
    responsible for calling AccountManager.add_account for each one,
    which is what actually assigns the (blank) password placeholder
    and regenerates himalaya.toml."""
    from account_manager import Account

    if not isinstance(data, dict):
        raise ValueError("Not a ZBox export file (expected a JSON object).")

    accounts = []
    for entry in data.get("accounts") or []:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        entry.pop("account_id", None)  # force a fresh id, see module docstring
        accounts.append(Account.from_dict(entry))
    return accounts


def apply_portable_settings(settings, data):
    """Merges the portable settings fields present in data into the
    given live Settings object in place. Fields absent from the
    import (an older export, or a hand-edited file) are left
    untouched; fields not in PORTABLE_SETTINGS_FIELDS -- window/sash/
    last-selected-folder state -- are never touched even if present,
    since re-exporting from a future version might still include
    them under an old habit."""
    if not isinstance(data, dict):
        return
    imported = data.get("settings") or {}
    if not isinstance(imported, dict):
        return
    for key in PORTABLE_SETTINGS_FIELDS:
        if key in imported:
            setattr(settings, key, imported[key])
