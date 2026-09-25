"""
Privacy and content blocking -- what content_blocker and blocklist
actually do.

Audit B named this one of the two daily user paths with nothing
testing its behaviour. It is a path where a silent failure is
invisible by definition: a filter that stops stripping a tracking
pixel looks exactly like a filter that is working, because the
message still renders and nothing is announced. Only a test notices.

What is covered: content_blocker's three public names (url_host,
BlockReport, filter_message_html) and blocklist's three (parse_hosts,
parse_adblock, Blocklist).

What is deliberately not covered: privacy_dialog. Its only public
name is PrivacyDialog, a wx.Dialog that builds its controls in
__init__ and exposes no helper to call, so there is nothing here to
assert that is not really an assertion about wx. Same accepted gap as
every other dialog in this suite.

No network is ever opened. blocklist._download is patched in every
test that reaches an update, and everything on disk happens inside a
temporary folder -- never the real userdata folder.
"""

import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS_DIR, "wxstub"))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), "app"))

import blocklist as blocklist_module
from blocklist import Blocklist, parse_adblock, parse_hosts
from content_blocker import BlockReport, filter_message_html, url_host


REMOTE_IMG = "https://cdn.example.com/photo.png"


class UrlHostTests(unittest.TestCase):
    """url_host decides which host a reference would be fetched from,
    and everything the blocklist does depends on it being right."""

    def test_ordinary_absolute_url(self):
        self.assertEqual(url_host("https://ads.example.com/a.gif"),
                         "ads.example.com")

    def test_protocol_relative_url_still_has_a_host(self):
        self.assertEqual(url_host("//ads.example.com/a.gif"),
                         "ads.example.com")

    def test_userinfo_is_not_mistaken_for_the_host(self):
        self.assertEqual(url_host("https://user:pw@real.example.com/a"),
                         "real.example.com")

    def test_port_is_stripped(self):
        self.assertEqual(url_host("https://example.com:8443/a"), "example.com")

    def test_ipv6_literal(self):
        self.assertEqual(url_host("https://[2001:db8::1]:443/a"), "2001:db8::1")

    def test_case_is_normalised(self):
        self.assertEqual(url_host("https://ADS.Example.COM/a"),
                         "ads.example.com")

    def test_relative_path_has_no_host(self):
        self.assertEqual(url_host("images/logo.png"), "")

    def test_empty_and_none(self):
        self.assertEqual(url_host(""), "")
        self.assertEqual(url_host(None), "")


class BlockAllRemoteTests(unittest.TestCase):
    """The default mode: nothing remote loads until the reader asks."""

    def filter(self, html):
        return filter_message_html(html)

    def test_remote_image_source_is_stripped(self):
        out, report = self.filter(
            '<img src="%s" alt="A photo" width="600">' % REMOTE_IMG
        )
        self.assertNotIn(REMOTE_IMG, out)
        self.assertEqual(report.blocked, 1)
        self.assertIn("cdn.example.com", report.hosts)

    def test_cid_attachments_always_load(self):
        out, report = self.filter('<img src="cid:part1.abc" alt="Signed">')
        self.assertIn("cid:part1.abc", out)
        self.assertFalse(report.any_blocked)

    def test_data_uris_always_load(self):
        out, report = self.filter('<img src="data:image/gif;base64,R0lGOD" alt="Dot">')
        self.assertIn("data:image", out)
        self.assertFalse(report.any_blocked)

    def test_links_are_not_subresources_and_are_left_alone(self):
        out, report = self.filter('<a href="https://example.com/page">Read</a>')
        self.assertIn('href="https://example.com/page"', out)
        self.assertFalse(report.any_blocked)

    def test_background_attribute_is_a_fetch_too(self):
        out, report = self.filter(
            '<body background="https://bg.example.com/tile.png">hi</body>'
        )
        self.assertNotIn("bg.example.com", out)
        self.assertEqual(report.blocked, 1)

    def test_stylesheet_link_with_a_stripped_href_is_dropped_whole(self):
        out, report = self.filter(
            '<link rel="stylesheet" href="https://css.example.com/mail.css">'
        )
        self.assertNotIn("<link", out)
        self.assertEqual(report.blocked, 1)

    def test_form_action_never_posts_anywhere(self):
        out, report = self.filter(
            '<form action="https://collect.example.com/x"><input name="a"></form>'
        )
        self.assertNotIn("collect.example.com", out)
        self.assertEqual(report.blocked, 1)

    def test_srcset_goes_if_any_candidate_is_remote(self):
        out, report = self.filter(
            '<img alt="Banner" srcset="%s 1x, %s 2x">' % (REMOTE_IMG, REMOTE_IMG)
        )
        self.assertNotIn("srcset", out)
        self.assertNotIn("cdn.example.com", out)
        self.assertTrue(report.any_blocked)


class ImgAndTrackerTests(unittest.TestCase):
    """A blocked image is a nuisance; a blocked beacon is the point.
    The report has to tell them apart, and the reading order has to
    survive either."""

    def filter(self, html):
        return filter_message_html(html)

    def test_one_by_one_image_is_counted_as_a_tracker(self):
        _, report = self.filter(
            '<img src="https://t.example.com/p.gif" width="1" height="1">'
        )
        self.assertEqual(report.trackers, 1)

    def test_a_pixel_hidden_by_inline_style_is_a_tracker(self):
        _, report = self.filter(
            '<img src="https://t.example.com/p.gif" alt="." '
            'style="width:2px;height:2px">'
        )
        self.assertEqual(report.trackers, 1)

    def test_an_open_tracking_url_is_a_tracker_whatever_its_size(self):
        _, report = self.filter(
            '<img src="https://mail.example.com/wf/open?id=9" alt="." '
            'width="400" height="300">'
        )
        self.assertEqual(report.trackers, 1)

    def test_an_ordinary_picture_is_blocked_but_not_called_a_tracker(self):
        _, report = self.filter(
            '<img src="%s" alt="A photo" width="600" height="400">' % REMOTE_IMG
        )
        self.assertEqual(report.blocked, 1)
        self.assertEqual(report.trackers, 0)

    def test_an_image_with_alt_text_stays_in_the_reading_order(self):
        out, _ = self.filter(
            '<img src="%s" alt="Quarterly chart" width="600">' % REMOTE_IMG
        )
        self.assertIn("Quarterly chart", out)
        self.assertIn("data-zbox-blocked", out)

    def test_an_image_with_nothing_to_announce_is_removed_entirely(self):
        out, _ = self.filter('<p>Hello</p><img src="%s" width="600">' % REMOTE_IMG)
        self.assertNotIn("<img", out)
        self.assertIn("Hello", out)


class ScriptsAndModesTests(unittest.TestCase):
    """Scripts and frames go in both modes and are never restored.
    Remote references go only in the mode that asked for it."""

    def filter(self, html, **kwargs):
        return filter_message_html(html, **kwargs)

    def test_scripts_are_removed_with_their_contents(self):
        out, report = self.filter('<p>Hi</p><script>alert(1)</script>')
        self.assertNotIn("alert", out)
        self.assertNotIn("<script", out)
        self.assertEqual(report.scripts, 1)

    def test_scripts_go_even_when_remote_content_is_allowed(self):
        out, report = self.filter(
            '<script>alert(1)</script>', block_all_remote=False
        )
        self.assertNotIn("alert", out)
        self.assertEqual(report.scripts, 1)

    def test_frames_and_objects_are_dropped(self):
        out, _ = self.filter(
            '<iframe src="https://x.example.com/f"></iframe>'
            '<object data="https://x.example.com/o"></object>'
        )
        self.assertNotIn("<iframe", out)
        self.assertNotIn("<object", out)

    def test_base_is_dropped_so_relative_paths_cannot_escape(self):
        out, _ = self.filter('<base href="https://x.example.com/"><p>Hi</p>')
        self.assertNotIn("<base", out)
        self.assertIn("Hi", out)

    def test_inline_event_handlers_are_removed(self):
        out, _ = self.filter('<div onclick="steal()">Text</div>')
        self.assertNotIn("onclick", out)
        self.assertNotIn("steal", out)
        self.assertIn("Text", out)

    def test_meta_refresh_is_dropped(self):
        out, _ = self.filter(
            '<meta http-equiv="refresh" content="0;url=https://x.example.com/">'
        )
        self.assertNotIn("refresh", out)

    def test_blocklist_mode_keeps_first_party_and_strips_a_known_host(self):
        is_blocked = lambda host: host == "tracker.example.net"
        out, report = self.filter(
            '<img src="https://cdn.sender.com/logo.png" alt="Logo">'
            '<img src="https://tracker.example.net/p.gif" alt="Pixel">',
            block_all_remote=False, is_blocked_host=is_blocked,
        )
        self.assertIn("cdn.sender.com/logo.png", out)
        self.assertNotIn("tracker.example.net", out)
        self.assertEqual(report.blocked, 1)

    def test_relative_paths_are_harmless_once_base_is_gone(self):
        out, report = self.filter(
            '<img src="images/logo.png" alt="Logo">', block_all_remote=False
        )
        self.assertIn("images/logo.png", out)
        self.assertFalse(report.any_blocked)

    def test_css_urls_and_imports_are_filtered(self):
        out, report = self.filter(
            "<style>@import url(https://css.example.com/a.css);"
            "body{background-image:url('https://css.example.com/bg.png')}</style>"
        )
        self.assertNotIn("@import", out)
        self.assertNotIn("css.example.com", out)
        self.assertIn("about:blank", out)
        self.assertEqual(report.blocked, 2)

    def test_literal_ampersands_in_the_text_survive(self):
        out, _ = self.filter("<p>Tom &amp; Jerry</p>")
        self.assertIn("&amp;", out)
        self.assertIn("Jerry", out)

    def test_malformed_markup_never_raises(self):
        out, _ = self.filter('<div><img src="https://x.example.com/a" <p>unclosed')
        self.assertIsInstance(out, str)

    def test_empty_input_comes_back_unchanged(self):
        out, report = self.filter("")
        self.assertEqual(out, "")
        self.assertFalse(report.any_blocked)


class BlockReportTests(unittest.TestCase):
    """The summary is read aloud. "Content was blocked" tells the
    reader nothing they can act on, so the wording is a contract."""

    def test_nothing_blocked(self):
        self.assertEqual(BlockReport().summary(),
                         "No remote content in this message.")

    def test_singular_wording(self):
        report = BlockReport()
        report.blocked = 1
        report.trackers = 1
        report.scripts = 1
        report.hosts = {"ads.example.com"}
        text = report.summary()
        self.assertIn("1 remote item blocked", text)
        self.assertIn("1 tracking pixel", text)
        self.assertIn("1 script removed", text)
        self.assertIn("from ads.example.com", text)

    def test_plural_wording(self):
        report = BlockReport()
        report.blocked = 3
        report.trackers = 2
        report.scripts = 2
        text = report.summary()
        self.assertIn("3 remote items blocked", text)
        self.assertIn("2 tracking pixels", text)
        self.assertIn("2 scripts removed", text)

    def test_long_host_lists_are_shortened_for_speech(self):
        report = BlockReport()
        report.blocked = 4
        report.hosts = {"a.example.com", "b.example.com",
                        "c.example.com", "d.example.com"}
        text = report.summary()
        self.assertIn("and 1 more", text)
        self.assertTrue(text.endswith("."))


HOSTS_TEXT = """# a comment
0.0.0.0 ads.example.com
0.0.0.0 localhost
127.0.0.1 beacon.example.org
bare.example.com
"""

ADBLOCK_TEXT = """! Title: test
||tracker.example.net^
@@||good.example.net^
||path.example.net/x^
||wild*.example.net^
||ctx.example.net^$domain=foo.com
cosmetic.example.net##.ad
"""


class BlocklistParseTests(unittest.TestCase):
    def test_hosts_format_keeps_only_real_mappings(self):
        found = parse_hosts(HOSTS_TEXT)
        self.assertIn("ads.example.com", found)
        self.assertIn("beacon.example.org", found)
        self.assertNotIn("localhost", found)
        self.assertNotIn("bare.example.com", found)

    def test_adblock_format_keeps_only_domain_anchored_rules(self):
        found = parse_adblock(ADBLOCK_TEXT)
        self.assertEqual(found, {"tracker.example.net"})

    def test_an_exception_rule_never_becomes_a_block_rule(self):
        self.assertNotIn("good.example.net", parse_adblock(ADBLOCK_TEXT))


class BlocklistStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="zbox_blocklist_")
        self.paths = SimpleNamespace(userdata=self.temp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_a_first_run_with_no_download_still_blocks_something(self):
        store = Blocklist(self.paths)
        self.assertEqual(store.domains, set(blocklist_module.SEED_DOMAINS))
        self.assertTrue(store.count > 0)

    def test_a_parent_domain_blocks_its_subdomains(self):
        store = Blocklist(self.paths)
        store.domains = {"tracker.com"}
        self.assertTrue(store.is_blocked("ads.tracker.com"))
        self.assertTrue(store.is_blocked("a.b.tracker.com"))
        self.assertTrue(store.is_blocked("tracker.com"))

    def test_a_subdomain_does_not_block_its_parent(self):
        store = Blocklist(self.paths)
        store.domains = {"ads.tracker.com"}
        self.assertFalse(store.is_blocked("tracker.com"))

    def test_case_and_trailing_dots_are_normalised(self):
        store = Blocklist(self.paths)
        store.domains = {"tracker.com"}
        self.assertTrue(store.is_blocked("ADS.Tracker.COM."))

    def test_an_empty_host_is_not_blocked(self):
        store = Blocklist(self.paths)
        self.assertFalse(store.is_blocked(""))
        self.assertFalse(store.is_blocked(None))

    def test_needs_update_is_true_until_something_succeeds(self):
        store = Blocklist(self.paths)
        self.assertTrue(store.needs_update())
        store.meta["last_updated"] = time.time()
        self.assertFalse(store.needs_update())

    def test_status_text_says_it_has_never_been_updated(self):
        store = Blocklist(self.paths)
        self.assertIn("Never updated from the internet yet.",
                      store.status_text())

    def test_status_text_after_a_recent_update(self):
        store = Blocklist(self.paths)
        store.meta["last_updated"] = time.time()
        self.assertIn("less than an hour ago", store.status_text())

    def test_status_text_reports_a_partial_failure(self):
        store = Blocklist(self.paths)
        store.meta["last_updated"] = time.time()
        store.meta["last_error"] = "EasyPrivacy third party (timed out)"
        text = store.status_text()
        self.assertIn("Some sources could not be reached:", text)
        self.assertIn("EasyPrivacy third party", text)


class PhishingListTests(unittest.TestCase):
    """The junk rules' phishing list: the same machinery as the ad
    list, with its own folder, sources and never-listed platforms."""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="zbox_phishing_")
        self.paths = SimpleNamespace(userdata=self.temp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_plain_domain_lists_parse(self):
        found = blocklist_module.parse_domains(
            "# comment\nevil.example.com\n\nnot a domain\nBAD.Example.NET\n"
        )
        self.assertEqual(found, {"evil.example.com", "bad.example.net"})

    def test_it_matches_nothing_before_its_first_download(self):
        store = blocklist_module.PhishingList(self.paths)
        self.assertEqual(store.domains, set())
        self.assertFalse(store.is_blocked("evil.example.com"))

    def test_it_keeps_its_own_folder_apart_from_the_ad_list(self):
        self.assertNotEqual(
            blocklist_module.PhishingList(self.paths).domains_file,
            Blocklist(self.paths).domains_file,
        )

    def test_shared_platforms_named_outright_are_dropped(self):
        store = blocklist_module.PhishingList(self.paths)
        feed = "docs.google.com\nsites.google.com\nevil.example.com\nsomeone.github.io\n"
        with mock.patch.object(blocklist_module, "_download", return_value=feed):
            changed, message = store.update(force=True)
        self.assertTrue(changed)
        self.assertIn("Phishing list updated", message)
        self.assertFalse(store.is_blocked("docs.google.com"))
        self.assertFalse(store.is_blocked("sites.google.com"))
        self.assertTrue(store.is_blocked("evil.example.com"))
        self.assertTrue(store.is_blocked("someone.github.io"))


class BlocklistUpdateTests(unittest.TestCase):
    """Every download is patched out. Nothing here touches a socket."""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="zbox_blocklist_update_")
        self.paths = SimpleNamespace(userdata=self.temp)
        self.store = Blocklist(self.paths)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp, ignore_errors=True)

    @staticmethod
    def _fake_download(url):
        if "StevenBlack" in url:
            return HOSTS_TEXT
        return ADBLOCK_TEXT

    def test_a_successful_update_merges_saves_and_reloads(self):
        with mock.patch.object(blocklist_module, "_download",
                               side_effect=self._fake_download):
            changed, message = self.store.update()
        self.assertTrue(changed)
        self.assertIn("Blocklist updated:", message)
        self.assertIn("ads.example.com", self.store.domains)
        self.assertIn("tracker.example.net", self.store.domains)
        # The hand-kept seed list is folded in rather than replaced.
        self.assertTrue(set(blocklist_module.SEED_DOMAINS) <= self.store.domains)
        self.assertTrue(os.path.isfile(self.store.domains_file))
        reloaded = Blocklist(self.paths)
        self.assertIn("ads.example.com", reloaded.domains)
        self.assertTrue(reloaded.is_blocked("img.ads.example.com"))

    def test_every_source_failing_leaves_the_previous_list_alone(self):
        before = set(self.store.domains)

        def boom(url):
            raise OSError("no network")

        with mock.patch.object(blocklist_module, "_download", side_effect=boom):
            changed, message = self.store.update()
        self.assertFalse(changed)
        self.assertIn("Could not update the blocklist", message)
        self.assertEqual(self.store.domains, before)
        self.assertIn("no network", self.store.meta.get("last_error", ""))

    def test_a_partial_failure_still_updates_and_names_what_failed(self):
        def partial(url):
            if "StevenBlack" in url:
                raise OSError("timed out")
            return ADBLOCK_TEXT

        with mock.patch.object(blocklist_module, "_download",
                               side_effect=partial):
            changed, message = self.store.update()
        self.assertTrue(changed)
        self.assertIn("Some sources failed:", message)
        self.assertIn("StevenBlack", message)
        self.assertIn("tracker.example.net", self.store.domains)

    def test_an_up_to_date_list_is_not_downloaded_again(self):
        self.store.meta["last_updated"] = time.time()

        def must_not_run(url):
            raise AssertionError("update() downloaded when it should not have")

        with mock.patch.object(blocklist_module, "_download",
                               side_effect=must_not_run):
            changed, message = self.store.update()
        self.assertFalse(changed)
        self.assertIn("already up to date", message)


if __name__ == "__main__":
    unittest.main()
