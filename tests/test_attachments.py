"""
Audit finding 17: the attachments list (name, format, size) and
Save All, plus the configurable save folder behind it. Covers the
pure pieces -- format/size derivation from a real part shape, and
Settings.resolved_attachment_save_dir -- not the wx list widget
itself.
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from message_view_panel import (
    _attachment_filename,
    _attachment_format_label,
    _attachment_size_bytes,
    _format_attachment_size,
)
from settings_manager import Settings


def _jpeg_part(size=100):
    return {
        "headers": [
            {
                "name": "content_type",
                "value": {
                    "ContentType": {
                        "c_type": "image",
                        "c_subtype": "jpeg",
                        "attributes": [{"name": "name", "value": "image001.jpg"}],
                    }
                },
            }
        ],
        "body": {"Binary": [0] * size},
    }


class AttachmentFormatAndSize(unittest.TestCase):
    def test_format_label_from_real_shape(self):
        # Matches a real cached message: image/jpeg -> "JPEG image".
        self.assertEqual(_attachment_format_label(_jpeg_part()), "JPEG image")

    def test_format_label_falls_back_to_raw_mime(self):
        part = {
            "headers": [
                {
                    "name": "content_type",
                    "value": {"ContentType": {"c_type": "font", "c_subtype": "ttf"}},
                }
            ]
        }
        self.assertEqual(_attachment_format_label(part), "font/ttf")

    def test_format_label_unknown_when_no_content_type(self):
        self.assertEqual(_attachment_format_label({}), "Unknown")

    def test_size_bytes_from_binary_body(self):
        self.assertEqual(_attachment_size_bytes(_jpeg_part(size=37000)), 37000)

    def test_size_bytes_from_text_body_counts_utf8_bytes(self):
        part = {"body": {"Text": "h\u00e9llo"}}  # 5 chars, 6 UTF-8 bytes
        self.assertEqual(_attachment_size_bytes(part), 6)

    def test_size_bytes_none_when_body_missing(self):
        self.assertIsNone(_attachment_size_bytes({}))

    def test_format_attachment_size_thresholds(self):
        self.assertEqual(_format_attachment_size(None), "Unknown size")
        self.assertEqual(_format_attachment_size(512), "512 B")
        self.assertEqual(_format_attachment_size(37000), "36 KB")
        self.assertEqual(_format_attachment_size(600000), "586 KB")
        self.assertEqual(_format_attachment_size(5 * 1024 * 1024), "5.0 MB")

    def test_filename_still_works_alongside_new_helpers(self):
        parts = [_jpeg_part()]
        self.assertEqual(_attachment_filename(parts, 0), "image001.jpg")


class ResolvedAttachmentSaveDir(unittest.TestCase):
    def test_blank_setting_uses_downloads_attachments_default(self):
        settings = Settings(attachment_save_dir="")
        resolved = settings.resolved_attachment_save_dir()
        self.assertTrue(resolved.endswith(os.path.join("Downloads", "attachments")))

    def test_custom_setting_is_expanded_and_used_verbatim(self):
        settings = Settings(attachment_save_dir=r"D:\MyAttachments")
        self.assertEqual(settings.resolved_attachment_save_dir(), r"D:\MyAttachments")

    def test_round_trips_through_to_dict_from_dict(self):
        settings = Settings(attachment_save_dir=r"D:\MyAttachments")
        restored = Settings.from_dict(settings.to_dict())
        self.assertEqual(restored.attachment_save_dir, r"D:\MyAttachments")

    def test_missing_key_in_from_dict_defaults_to_blank(self):
        settings = Settings.from_dict({})
        self.assertEqual(settings.attachment_save_dir, "")


if __name__ == "__main__":
    unittest.main()
