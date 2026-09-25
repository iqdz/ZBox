"""
Audit finding 52: window size/position/maximized state now persists
across launches. window_geometry.py holds the pure decision logic
(kept wx-free, same pattern as envelope_format.py/filter_rules.py/
undo_manager.py) -- whether a saved position is still reachable on
the monitors currently attached, and what geometry to actually apply
given saved settings plus those monitors. main_frame.py's
_restore_window_state/_on_frame_geometry_changed/_save_window_state
are thin wx wrappers around these and are not unit-tested here, same
accepted gap as this project's other wx wiring (tree/menu wiring in
findings 32/42, dialog wiring in findings 45/46/49/50).

Settings' window_x/window_y/window_width/window_height/
window_maximized fields get the same to_dict/from_dict round-trip
coverage every other Settings field gets (see test_draft_settings.py).
"""

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

from window_geometry import rect_is_visible, resolve_window_geometry
from settings_manager import Settings


SINGLE_1080P = [(0, 0, 1920, 1080)]
DUAL_MONITOR = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]


class RectIsVisible(unittest.TestCase):
    def test_rect_fully_on_screen_is_visible(self):
        self.assertTrue(rect_is_visible(100, 100, 800, 600, SINGLE_1080P))

    def test_rect_fully_off_every_display_is_not_visible(self):
        self.assertFalse(rect_is_visible(5000, 5000, 800, 600, SINGLE_1080P))

    def test_rect_mostly_off_screen_by_a_sliver_is_not_visible(self):
        # Only a 10px sliver of the window would be reachable --
        # below the min_visible_px floor, so this doesn't count.
        self.assertFalse(rect_is_visible(1910, 0, 800, 600, SINGLE_1080P, min_visible_px=50))

    def test_rect_with_enough_overlap_is_visible(self):
        self.assertTrue(rect_is_visible(1860, 0, 800, 600, SINGLE_1080P, min_visible_px=50))

    def test_second_monitor_counts_too(self):
        self.assertTrue(rect_is_visible(2500, 100, 800, 600, DUAL_MONITOR))

    def test_no_displays_means_never_visible(self):
        self.assertFalse(rect_is_visible(100, 100, 800, 600, []))

    def test_zero_or_negative_size_is_never_visible(self):
        self.assertFalse(rect_is_visible(100, 100, 0, 600, SINGLE_1080P))
        self.assertFalse(rect_is_visible(100, 100, 800, -1, SINGLE_1080P))


class ResolveWindowGeometry(unittest.TestCase):
    def test_nothing_saved_falls_back_to_defaults_and_no_position(self):
        width, height, x, y, maximized = resolve_window_geometry(
            None, None, 1100, 700, False, SINGLE_1080P,
        )
        self.assertEqual((width, height), (1100, 700))
        self.assertIsNone(x)
        self.assertIsNone(y)
        self.assertFalse(maximized)

    def test_valid_saved_position_is_kept(self):
        width, height, x, y, maximized = resolve_window_geometry(
            200, 150, 1000, 650, False, SINGLE_1080P,
        )
        self.assertEqual((width, height, x, y), (1000, 650, 200, 150))

    def test_saved_position_off_every_display_is_dropped(self):
        # Monitor arrangement changed since this was saved (e.g. an
        # external monitor unplugged) -- fall back to centering
        # rather than reopening off-screen.
        width, height, x, y, maximized = resolve_window_geometry(
            3000, 3000, 1000, 650, False, SINGLE_1080P,
        )
        self.assertEqual((width, height), (1000, 650))
        self.assertIsNone(x)
        self.assertIsNone(y)

    def test_maximized_flag_passes_through(self):
        _, _, _, _, maximized = resolve_window_geometry(
            None, None, 1100, 700, True, SINGLE_1080P,
        )
        self.assertTrue(maximized)

    def test_invalid_saved_size_falls_back_to_default(self):
        width, height, _, _, _ = resolve_window_geometry(
            None, None, 0, -50, False, SINGLE_1080P,
        )
        self.assertEqual((width, height), (1100, 700))

    def test_custom_defaults_are_honored(self):
        width, height, _, _, _ = resolve_window_geometry(
            None, None, None, None, False, SINGLE_1080P,
            default_width=900, default_height=500,
        )
        self.assertEqual((width, height), (900, 500))


class WindowSettingsRoundTrip(unittest.TestCase):
    def test_defaults(self):
        settings = Settings()
        self.assertIsNone(settings.window_x)
        self.assertIsNone(settings.window_y)
        self.assertEqual(settings.window_width, 1100)
        self.assertEqual(settings.window_height, 700)
        self.assertFalse(settings.window_maximized)

    def test_to_dict_and_from_dict_round_trip(self):
        settings = Settings(
            window_x=42, window_y=17, window_width=1280, window_height=800,
            window_maximized=True,
        )
        restored = Settings.from_dict(settings.to_dict())
        self.assertEqual(restored.window_x, 42)
        self.assertEqual(restored.window_y, 17)
        self.assertEqual(restored.window_width, 1280)
        self.assertEqual(restored.window_height, 800)
        self.assertTrue(restored.window_maximized)

    def test_from_dict_with_no_window_keys_uses_defaults(self):
        # A settings.json written before this finding has none of
        # these keys at all.
        restored = Settings.from_dict({})
        self.assertIsNone(restored.window_x)
        self.assertEqual(restored.window_width, 1100)
        self.assertFalse(restored.window_maximized)


if __name__ == "__main__":
    unittest.main()
