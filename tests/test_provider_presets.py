"""
Audit finding 32: outlook.com, hotmail.com and live.com presets fill
in server settings that cannot actually sign in once Microsoft
withdrew basic auth, and the wizard used to hand those settings over
with only a soft note. Covers the data (provider_presets.py) and the
pure preset_is_unsupported() helper that ServerPage.apply_preset and
the "NOT SUPPORTED" gate in AccountWizard._on_page_changing key off
of -- kept wx-free deliberately so it is testable without a real
ServerPage (the wxstub's Stub objects don't support the numeric
comparisons wrap_text needs).
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from provider_presets import PROVIDER_PRESETS, preset_for_domain, preset_is_unsupported


class UnsupportedPresetsAreMarked(unittest.TestCase):
    def test_microsoft_domains_are_marked_unsupported(self):
        for domain in ("outlook.com", "hotmail.com", "live.com"):
            with self.subTest(domain=domain):
                self.assertTrue(PROVIDER_PRESETS[domain].get("unsupported"))
                self.assertIn("OAuth2", PROVIDER_PRESETS[domain]["note"])

    def test_working_domains_are_not_marked_unsupported(self):
        for domain in ("gmail.com", "yahoo.com", "icloud.com", "disroot.org",
                       "fastmail.com", "zoho.com", "gmx.com", "protonmail.com"):
            with self.subTest(domain=domain):
                self.assertFalse(PROVIDER_PRESETS[domain].get("unsupported", False))

    def test_lookup_by_domain_carries_the_flag_through(self):
        preset = preset_for_domain("someone@outlook.com")
        self.assertTrue(preset["unsupported"])


class PresetIsUnsupportedHelper(unittest.TestCase):
    def test_unsupported_preset_is_flagged(self):
        self.assertTrue(preset_is_unsupported(preset_for_domain("someone@hotmail.com")))

    def test_supported_preset_is_not_flagged(self):
        self.assertFalse(preset_is_unsupported(preset_for_domain("someone@gmail.com")))

    def test_no_preset_at_all_is_not_flagged(self):
        self.assertFalse(preset_is_unsupported(preset_for_domain("someone@example.net")))

    def test_none_is_not_flagged(self):
        self.assertFalse(preset_is_unsupported(None))


if __name__ == "__main__":
    unittest.main()
