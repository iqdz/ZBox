"""
sound_manager (audio notification / sound-theme system): resolve_sound_path
and available_themes are pure filesystem logic and are tested directly
against real temp directories. play() uses the stdlib winsound module
(Windows-only, so not installed here) and is tested against a small fake
of it -- see the Play class for why this module moved off wx.Sound.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import sound_manager as sm


def _touch(path):
    with open(path, "wb") as f:
        f.write(b"\x00")


class ResolveSoundPath(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "default"))
        _touch(os.path.join(self.root, "default", "delete.wav"))

    def test_finds_file_in_requested_theme(self):
        os.makedirs(os.path.join(self.root, "custom"))
        _touch(os.path.join(self.root, "custom", "delete.wav"))
        path = sm.resolve_sound_path(self.root, "custom", "delete")
        self.assertEqual(path, os.path.join(self.root, "custom", "delete.wav"))

    def test_falls_back_to_default_when_custom_theme_missing_file(self):
        os.makedirs(os.path.join(self.root, "custom"))
        # "custom" exists but has no delete.wav of its own.
        path = sm.resolve_sound_path(self.root, "custom", "delete")
        self.assertEqual(path, os.path.join(self.root, "default", "delete.wav"))

    def test_returns_none_when_missing_everywhere(self):
        self.assertIsNone(sm.resolve_sound_path(self.root, "default", "none_found"))

    def test_returns_none_for_falsy_sounds_dir(self):
        self.assertIsNone(sm.resolve_sound_path("", "default", "delete"))
        self.assertIsNone(sm.resolve_sound_path(None, "default", "delete"))

    def test_blank_or_none_theme_means_default(self):
        path_blank = sm.resolve_sound_path(self.root, "", "delete")
        path_none = sm.resolve_sound_path(self.root, None, "delete")
        expected = os.path.join(self.root, "default", "delete.wav")
        self.assertEqual(path_blank, expected)
        self.assertEqual(path_none, expected)

    def test_requesting_default_theme_directly_does_not_double_fall_back(self):
        # Regression guard: default-falls-back-to-default should just
        # resolve once, not loop or return a wrong path.
        path = sm.resolve_sound_path(self.root, "default", "delete")
        self.assertEqual(path, os.path.join(self.root, "default", "delete.wav"))


class AvailableThemes(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_returns_empty_list_when_sounds_dir_does_not_exist(self):
        missing = os.path.join(self.root, "does-not-exist")
        self.assertEqual(sm.available_themes(missing), [])

    def test_returns_empty_list_for_falsy_sounds_dir(self):
        self.assertEqual(sm.available_themes(""), [])
        self.assertEqual(sm.available_themes(None), [])

    def test_default_only_theme(self):
        os.makedirs(os.path.join(self.root, "default"))
        _touch(os.path.join(self.root, "default", "delete.wav"))
        self.assertEqual(sm.available_themes(self.root), ["default"])

    def test_default_sorts_first_regardless_of_name(self):
        os.makedirs(os.path.join(self.root, "aardvark"))
        _touch(os.path.join(self.root, "aardvark", "delete.wav"))
        os.makedirs(os.path.join(self.root, "default"))
        _touch(os.path.join(self.root, "default", "delete.wav"))
        self.assertEqual(sm.available_themes(self.root), ["default", "aardvark"])

    def test_non_default_themes_sort_alphabetically_case_insensitive(self):
        for name in ("Zeta", "alpha", "default"):
            folder = os.path.join(self.root, name)
            os.makedirs(folder)
            _touch(os.path.join(folder, "delete.wav"))
        self.assertEqual(sm.available_themes(self.root), ["default", "alpha", "Zeta"])

    def test_folder_with_no_recognized_sound_files_is_not_a_theme(self):
        empty_folder = os.path.join(self.root, "empty")
        os.makedirs(empty_folder)
        _touch(os.path.join(empty_folder, "readme.txt"))
        self.assertEqual(sm.available_themes(self.root), [])

    def test_files_in_the_sounds_root_are_ignored_not_treated_as_a_theme(self):
        os.makedirs(os.path.join(self.root, "default"))
        _touch(os.path.join(self.root, "default", "delete.wav"))
        _touch(os.path.join(self.root, "stray.wav"))
        self.assertEqual(sm.available_themes(self.root), ["default"])

    def test_a_theme_needs_only_one_recognized_file_to_count(self):
        # Partial custom themes are allowed -- sound_manager.play()
        # falls back to default per missing file, so a theme folder
        # with just one override file is still a valid, selectable
        # theme.
        os.makedirs(os.path.join(self.root, "quiet"))
        _touch(os.path.join(self.root, "quiet", "progress-tick.wav"))
        self.assertEqual(sm.available_themes(self.root), ["quiet"])


class FakeWinsound:
    """
    Stands in for the stdlib winsound module (Windows-only, so it
    can't be relied on to even exist in this project's own Linux-side
    test runs). Exposes just enough of winsound's real surface --
    SND_FILENAME, SND_ASYNC, PlaySound -- for play() to use unchanged.
    """

    SND_FILENAME = 0x00020000
    SND_ASYNC = 0x0001

    def __init__(self):
        self.calls = []
        self.raise_on_play = None

    def PlaySound(self, path, flags):
        if self.raise_on_play is not None:
            raise self.raise_on_play
        self.calls.append((path, flags))


class Play(unittest.TestCase):
    """
    play() went through two real bugs before landing here. First, it
    built a fresh wx.Sound local variable on every call; wx.Sound's
    Windows backend plays asynchronously by handing the sound data
    off to the OS, which needs that data to stay alive for as long as
    playback takes, and a local variable is garbage-collected the
    instant play() returns -- every notification was cut off before
    it was ever audible, silently, since IsOk() was still true and
    Play() still returned normally. Caching an instance per path
    fixed that, but then a real user hit `AttributeError: module 'wx'
    has no attribute 'Sound'` outright -- their installed wxPython
    build simply doesn't expose Sound, even though the same .wav
    files played fine in Windows Media Player. play() now uses the
    stdlib winsound module instead (ZBox only ever runs on Windows),
    which sidesteps both: no object needs to outlive the call, and
    winsound ships with every Windows Python regardless of how
    wxWidgets was compiled. These tests pin down winsound usage
    directly with a fake, since the real module isn't installed here.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "default"))
        _touch(os.path.join(self.root, "default", "delete.wav"))
        self.settings = types.SimpleNamespace(
            sounds_enabled=True, sound_theme="default"
        )
        self.fake_winsound = FakeWinsound()
        module_patcher = unittest.mock.patch.object(
            sm, "winsound", self.fake_winsound
        )
        available_patcher = unittest.mock.patch.object(
            sm, "WINSOUND_AVAILABLE", True
        )
        module_patcher.start()
        available_patcher.start()
        self.addCleanup(module_patcher.stop)
        self.addCleanup(available_patcher.stop)

    def test_plays_the_resolved_file_asynchronously_by_filename(self):
        sm.play(self.root, self.settings, "delete")
        expected_path = os.path.join(self.root, "default", "delete.wav")
        self.assertEqual(
            self.fake_winsound.calls,
            [(expected_path, FakeWinsound.SND_FILENAME | FakeWinsound.SND_ASYNC)],
        )

    def test_a_playsound_failure_is_a_silent_no_op_not_a_raise(self):
        self.fake_winsound.raise_on_play = RuntimeError("no audio device")
        sm.play(self.root, self.settings, "delete")  # must not raise

    def test_does_nothing_when_sounds_are_disabled(self):
        settings = types.SimpleNamespace(sounds_enabled=False, sound_theme="default")
        sm.play(self.root, settings, "delete")
        self.assertEqual(self.fake_winsound.calls, [])

    def test_does_nothing_when_no_file_resolves(self):
        sm.play(self.root, self.settings, "no-such-sound")
        self.assertEqual(self.fake_winsound.calls, [])

    def test_does_nothing_when_winsound_is_unavailable(self):
        # The real condition on any non-Windows Python, and exactly
        # what must NOT crash ZBox if it ever happened on Windows too.
        with unittest.mock.patch.object(sm, "WINSOUND_AVAILABLE", False):
            sm.play(self.root, self.settings, "delete")
        self.assertEqual(self.fake_winsound.calls, [])


if __name__ == "__main__":
    unittest.main()
