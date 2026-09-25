"""
Every ZBox module must import.

Run in a subprocess against the wx stub in tests/wxstub, so the fake
wx never leaks into the other tests and no wxPython install is
needed. This is the cheapest guard that exists against the split of
main_frame.py leaving a name behind or introducing an import cycle
-- both of which show up only at import time, and both of which mean
ZBox does not start.
"""

import os
import subprocess
import sys
import textwrap
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
APP = os.path.join(ROOT, "app")
STUB = os.path.join(TESTS_DIR, "wxstub")

MODULES = [
    "about_dialog", "accessible", "account_lanes", "account_manager",
    "account_settings_dialog", "account_tree_panel", "account_wizard",
    "address_book_dialog", "announce", "blocklist", "bulk_execution",
    "bulk_progress_dialog", "compose_panel", "contacts",
    "content_blocker", "conversation_panel", "data_crypto",
    "desktop_notifications", "documentation_dialog",
    "dpapi_secret_store", "envelope_format", "envelope_list_panel",
    "eula_dialog", "filter_rules", "filter_rules_dialog", "get_secret",
    "himalaya_client", "himalaya_worker", "imap_body_fetch",
    "imap_idle_watcher", "import_export", "junk_origin",
    "legacy_keyring_store", "mail_fetch", "mail_tab_panel",
    "main_frame", "master_password_dialog", "message_body",
    "message_view_panel", "paths", "privacy_dialog", "provider_presets",
    "reader_panel", "related_messages_panel", "search_panel",
    "settings_dialog", "settings_manager", "shortcuts_dialog",
    "sound_manager", "junk_rules", "startup_registration",
    "thread_grouping", "thread_state", "tray_icon", "undo_manager",
    "unified_folders_dialog", "unlock_dialog", "unlock_methods",
    "webauthn_unlock", "webview2_runtime", "win_crypto",
    "window_geometry",
]


class Imports(unittest.TestCase):
    def test_every_module_imports(self):
        script = textwrap.dedent("""
            import importlib, sys
            failures = []
            for name in %r:
                try:
                    importlib.import_module(name)
                except Exception as exc:
                    failures.append("%%s: %%s: %%s" %% (name, type(exc).__name__, exc))
            if failures:
                print("\\n".join(failures))
                sys.exit(1)
        """ % MODULES)

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([STUB, APP])
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, cwd=ROOT,
        )
        self.assertEqual(
            result.returncode, 0,
            "modules failed to import:\n%s%s" % (result.stdout, result.stderr),
        )

    def test_the_panes_do_not_import_the_frame_back(self):
        """main_frame imports the panes. A pane importing main_frame
        would be a cycle, and the tempting shortcut every time a pane
        needs one more helper."""
        for name in ("account_tree_panel", "envelope_list_panel",
                     "reader_panel", "mail_tab_panel", "envelope_format",
                     "mail_fetch", "message_view_panel", "compose_panel"):
            with open(os.path.join(APP, name + ".py"), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn(
                "import main_frame", source,
                "%s imports main_frame, which is a cycle" % name,
            )


if __name__ == "__main__":
    unittest.main()
