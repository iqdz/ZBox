"""
ZBox's own settings, separate from account config. Currently holds
the debug logging toggle, the unified-folder preference, the
default message body view, the HTML screen-reader silence hold,
the WebView accessibility focus delay, which WebView2 runtime the
Edge backend is pointed at, and how remote content and known
tracker domains are handled in HTML messages; more app-wide preferences
land here as they're added.
"""

import json
import os
import sys


FOLDER_TYPES = ["Inbox", "Sent", "Drafts", "Trash", "Junk", "Archive"]

# The only ages Tools > Cache Clean Up Configuration offers, in days.
CACHE_AGE_CHOICES = (1, 7, 15, 30)

# Interface themes, in the order Settings lists them. See theme.py.
UI_THEMES = ("system", "light", "dark", "midnight", "warm", "soft")


class Settings:
    def __init__(
        self,
        debug_logging=True,
        language="en",
        unified_folder_types=None,
        default_message_view="html",
        html_silence_hold_ms=100,
        folder_pane_mode="all",
        webview_focus_delay_level=2,
        webview_backend="edge",
        webview2_runtime="auto",
        block_remote_content=True,
        blocklist_enabled=True,
        blocklist_auto_update=True,
        remote_content_allowed_senders=None,
        sort_key="date",
        sort_ascending=False,
        announcement_hold_ms=1800,
        announce_actions=True,
        show_message_headers=True,
        attachment_save_dir="",
        bare_key_shortcuts_enabled=True,
        draft_autosave_seconds=60,
        sounds_enabled=True,
        sound_theme="default",
        desktop_notifications_enabled=True,
        ui_theme="system",
        window_x=None,
        window_y=None,
        window_width=1100,
        window_height=700,
        window_maximized=False,
        last_selected_account_id=None,
        last_selected_folder=None,
        last_selected_unified_type=None,
        outer_sash_position=220,
        inner_sash_position=420,
        reading_font_size=None,
        reading_font_family="",
        minimize_to_tray=False,
        close_to_tray=False,
        stay_maximized=False,
        run_at_windows_startup=False,
        lock_on_full_quit=False,
        auto_purge_days=30,
        cache_max_message_age_days=30,
        cache_cleanup_last_run=0.0,
        spam_filter_enabled=False,
        spam_auto_move_to_junk_enabled=False,
        eula_accepted=False,
    ):
        self.debug_logging = debug_logging
        # Which interface language ZBox speaks, as the code of a file
        # in data/lang -- "en" until a translation is chosen. Read
        # once at startup by ZBoxApp (lang.load); changing it here
        # takes effect on the next launch, because menus, dialogs and
        # open tabs are all built from strings looked up when they
        # were created.
        self.language = (str(language or "en").strip() or "en")
        # Which folder types get combined into one subtree entry each
        # in the account tree, e.g. selecting "Inbox" produces a
        # single Inbox node that combines every account's Inbox.
        self.unified_folder_types = (
            unified_folder_types if unified_folder_types is not None else ["Inbox"]
        )
        # Which of Thunderbird's two folder-pane views the account
        # tree is currently showing: "all" (every account with its
        # own folders, the default) or "unified" (folders grouped by
        # type across accounts, each type expandable into its
        # per-account folders -- see View > Folders in the menu bar).
        self.folder_pane_mode = folder_pane_mode if folder_pane_mode in ("all", "unified") else "all"
        # Body view used when a message opens in its own tab:
        # "html" (rendered wx.html2.WebView, the default since
        # 9 September 2026) or "text" (plain read-only TextCtrl).
        # Ctrl+B inside an open message switches either way.
        #
        # Text was the default up to that date, on the reasoning that
        # the HTML view was the less accessible of the two. That is
        # no longer the case: the document title fix, the silence
        # hold, the focus delay, the content anchor, the blank-element
        # cleanup and the JS hotkey bridge between them made the
        # rendered view readable, so opening every message in the
        # plainer one stopped paying for itself. Changed at the
        # request of the JAWS/NVDA user ZBox is built for.
        #
        # Note this only decides what a NEW install does. An existing
        # settings.json holds this key explicitly (to_dict writes
        # every key on every save), so a machine that has ever saved
        # settings keeps whatever it chose, which is correct -- a
        # stored preference is not something a version bump overrides.
        self.default_message_view = (
            default_message_view if default_message_view in ("text", "html") else "html"
        )
        # How long (ms) ZBox holds a synthetic Ctrl press after an
        # HTML page finishes loading, to suppress the screen
        # reader's browser announcement. 0 disables the emulation
        # entirely; 0-2000.
        #
        # Defaults to on. The document-title fix stops the base64 URL
        # being read, but it does not stop the reader announcing the
        # control and the document type themselves -- "wxWebView" and
        # a generic "Web content page" were still being spoken over
        # the start of every HTML message, reported live. The hold is
        # the only thing that suppresses that window, so it is on by
        # default and can be turned back off in Settings.
        try:
            hold = int(html_silence_hold_ms)
        except (TypeError, ValueError):
            hold = 400
        self.html_silence_hold_ms = max(0, min(2000, hold))
        # How long ZBox waits after an HTML message's page reports
        # itself loaded before actually handing it keyboard focus.
        # WebView2 firing "loaded" and Windows' accessibility layer
        # (UIA) finishing construction of that page's accessible tree
        # are two different moments -- focusing in the gap between
        # them is what produces a generic placeholder announcement
        # ("Web content page") instead of the real message, and for
        # slower/heavier messages can leave the control reporting
        # unavailable for a stretch. A level from 1 to 10, stored
        # here and converted to milliseconds (100-1000ms, 100ms per
        # level) by webview_focus_delay_ms below.
        # Raised from level 3 to level 6 (300ms to 600ms) after the
        # placeholder announcement was still reported live at 300: the
        # gap between WebView2's "loaded" and UIA publishing the
        # document is longer in practice than the original guess.
        try:
            level = int(webview_focus_delay_level)
        except (TypeError, ValueError):
            level = 6
        self.webview_focus_delay_level = max(1, min(10, level))

        # Which engine renders the HTML message view.
        #
        # "edge" is WebView2/Chromium: modern CSS, but it renders in
        # its own process and hands its accessibility tree over only
        # via ICoreWebView2Controller::MoveFocus, which wxPython does
        # not expose -- so a screen reader can end up focused on the
        # control with no document to read.
        #
        # "ie" is MSHTML, the same engine Outlook Express and Windows
        # Mail rendered mail with. It runs in-process and publishes a
        # standard IAccessible/IA2 document tree, which NVDA and JAWS
        # have read in browse mode for two decades. Weaker on modern
        # CSS; email HTML is largely table-based and renders fine.
        backend = str(webview_backend or "edge").lower()
        self.webview_backend = backend if backend in ("edge", "ie") else "edge"

        # Which WebView2 runtime the Edge backend is pointed at.
        #
        # "auto" (the default) uses a Fixed Version runtime bundled
        # under apps_files\webview2 when one is present, and otherwise the
        # system-wide Evergreen runtime. "fixed" asks for the bundled
        # one and still falls back to Evergreen if it is missing, so
        # ZBox always starts. "evergreen" ignores any bundled copy.
        #
        # A bundled runtime pins the exact Chromium build ZBox's
        # screen-reader behaviour was verified against, so a
        # system-wide Edge update cannot silently change it. See
        # docs/webview2_fixed_runtime.md.
        runtime = str(webview2_runtime or "auto").lower()
        self.webview2_runtime = (
            runtime if runtime in ("auto", "fixed", "evergreen") else "auto"
        )

        # Whether an HTML message's remote references (images,
        # stylesheets, fonts) are stripped before it renders. On by
        # default, which is Thunderbird's behaviour and the only
        # thing that stops a tracking pixel reporting that you opened
        # the mail. Inline cid: attachments always show either way.
        self.block_remote_content = bool(block_remote_content)

        # Whether known ad and tracker domains stay blocked even once
        # remote content is allowed -- for a message you chose to
        # show, or a sender on the allow list below.
        self.blocklist_enabled = bool(blocklist_enabled)

        # Whether that domain list refreshes itself daily in the
        # background. Off means it only changes when you press Update
        # Now in Settings.
        self.blocklist_auto_update = bool(blocklist_auto_update)

        # Sender addresses whose messages load remote content without
        # asking, chosen through the message notification bar. Stored
        # lowercase; matched on the From address only.
        senders = remote_content_allowed_senders or []
        self.remote_content_allowed_senders = sorted(
            {str(address).strip().lower() for address in senders if str(address).strip()}
        )

        # Message list sort order (View > Sort By). Persisted because
        # a sort you have to re-pick every launch is not a sort order,
        # it is a chore -- and because a screen reader user cannot
        # glance at a column header to see where the list currently
        # stands.
        self.sort_key = sort_key if sort_key in ("date", "subject", "from") else "date"
        self.sort_ascending = bool(sort_ascending)

        # How long an announcement dialog (Ctrl+U and friends) stays
        # up before dismissing itself. This is a setting rather than a
        # constant because screen reader speech rates vary enormously:
        # at 450 words a minute the sentence is finished in half a
        # second, at 150 it is not, and a dialog that closes mid-word
        # truncates the one thing it existed to say. 0 keeps it up
        # until a key is pressed.
        try:
            hold = int(announcement_hold_ms)
        except (TypeError, ValueError):
            hold = 1800
        self.announcement_hold_ms = max(0, min(6000, hold))

        # Whether an action on a message says what it did out loud
        # (Settings > Screen reader announcements control). On by
        # default: the status bar these actions already write to is
        # only spoken when status-bar reporting is switched on, which
        # is off by default in NVDA and JAWS, so without this an
        # action and a silently failed action sound identical. Off is
        # for anyone who finds a dialog per action more interruption
        # than it is worth.
        self.announce_actions = bool(announce_actions)

        # Whether an open message shows a From/To/Cc/Date/Subject
        # block above the body. On by default: without it there is no
        # way to tell who sent a message from inside it, short of
        # closing the tab and going back to the list.
        self.show_message_headers = bool(show_message_headers)

        # Folder attachments are saved to when a message has more
        # than one and "Save All" is used (a single attachment
        # keeps asking where to save it, as before). Empty means use
        # the default: %USERPROFILE%\Downloads\attachments. Stored
        # as typed (may contain %ENV% references); resolved lazily by
        # resolved_attachment_save_dir() rather than at load time, so
        # a changed USERPROFILE or a moved drive is picked up on next
        # save rather than baked in.
        self.attachment_save_dir = str(attachment_save_dir or "").strip()

        # Bare (unmodified) S, A, J, W and K in the message list: S
        # toggles the flag/star on the selected message(s), A
        # archives them, J/Shift+J marks junk/not junk, W/K toggle
        # Watch Thread/Ignore Thread for whatever thread the
        # selection is part of -- matching Thunderbird. All of them
        # claim the keypress ahead of wx.ListCtrl's own type-ahead
        # jump-to-item-starting-with behavior, and bare A has no
        # confirmation or undo -- audit finding 19. On by default for
        # continuity with prior releases; turning this off returns
        # all five to normal first-letter list navigation.
        self.bare_key_shortcuts_enabled = bool(bare_key_shortcuts_enabled)

        # How often an open compose tab autosaves to Drafts, in
        # seconds. 0 turns autosave off (Save Draft/Ctrl+S and the
        # save-on-close prompt still work either way) -- audit
        # finding 40. Clamped to 0 or 15-600; a value below 15 would
        # mean a Himalaya round trip competing with normal typing.
        try:
            autosave = int(draft_autosave_seconds)
        except (TypeError, ValueError):
            autosave = 60
        self.draft_autosave_seconds = 0 if autosave <= 0 else max(15, min(600, autosave))

        # Master toggle for every notification sound (progress ticks,
        # new mail, send/reply/forward completing, delete, search) --
        # Settings > Sound & Notifications. On by default; a screen
        # reader already speaks most of what these sounds echo, so
        # this exists for anyone who finds the redundancy useful
        # rather than because silence would leave something unheard.
        self.sounds_enabled = bool(sounds_enabled)

        # Master toggle for Windows desktop (toast) notifications on
        # new mail, alongside sounds_enabled above -- Settings >
        # Sound & Notifications. On by default. A muted account (see
        # Account.notifications_muted) never shows one regardless of
        # this setting; this is the other direction, an app-wide kill
        # switch for every account at once. See desktop_notifications.py.
        self.desktop_notifications_enabled = bool(desktop_notifications_enabled)

        # Which subfolder of the sounds/ directory to play from --
        # "default" (bundled, always present) or a custom theme folder
        # the user added alongside it with the same file names.
        # Not validated against what actually exists on disk here
        # (Settings has no filesystem access of its own); an unknown
        # or removed theme name simply falls back to default file by
        # file at playback time -- see sound_manager.resolve_sound_path.
        theme = str(sound_theme or "default").strip()
        self.sound_theme = theme or "default"

        # Interface theme (Settings > Interface theme), applied once
        # at startup by theme.apply_theme because Windows only lets
        # dark mode be switched on before the first window exists.
        # "system" follows Windows' own light or dark app mode; the
        # rest force one look. Windows High Contrast always wins.
        ui = str(ui_theme or "system").strip().lower()
        self.ui_theme = ui if ui in UI_THEMES else "system"

        # Window size/position/maximized state, restored on the next
        # launch (audit finding 52) -- ZBox previously always opened
        # at a fixed 1100x700, centered, discarding anything the user
        # had arranged. window_x/window_y are None until a session
        # actually closes with something to save, and are deliberately
        # NOT part of config/settings.default.json's shipped template
        # -- a fresh install should center like it always has, not
        # aim at a fixed coordinate that means nothing on an unseen
        # monitor layout. See window_geometry.resolve_window_geometry
        # for how a saved position no longer on any attached display
        # (external monitor unplugged, resolution changed) is
        # detected and ignored rather than opening off-screen.
        self.window_x = window_x
        self.window_y = window_y
        self.window_width = window_width
        self.window_height = window_height
        self.window_maximized = bool(window_maximized)

        # Last-selected account/folder (or Unified Folders entry),
        # restored on the next launch (audit finding 52) instead of
        # always landing back on the first account's Inbox. Three
        # flat fields rather than one nested value so plain JSON
        # round-trips it without ambiguity -- see account_tree_panel.
        # target_to_saved_fields/saved_fields_to_target for the
        # conversion to/from the tree's own selected_fetch_target()
        # dict shape.
        self.last_selected_account_id = last_selected_account_id
        self.last_selected_folder = last_selected_folder
        self.last_selected_unified_type = last_selected_unified_type

        # Splitter sash positions for the Mail tab's two splitters
        # (account tree vs. everything else, then message list vs.
        # reader) -- audit finding 52. Defaults match the fixed
        # positions every previous launch hardcoded
        # (mail_tab_panel.MailTabPanel), so a fresh settings.json
        # looks identical to before this finding.
        self.outer_sash_position = outer_sash_position
        self.inner_sash_position = inner_sash_position

        # Message body font size/zoom and an optional reading font
        # face (audit finding 53) -- cheap, high-value for low-vision
        # readers. reading_font_size is None until the reader
        # explicitly overrides it (via Settings or the View > Message
        # Zoom commands/Ctrl+Shift+=/-/0), which is deliberate: an
        # untouched install keeps using each machine's own normal
        # control font exactly as before this finding, rather than
        # baking in one arbitrary point size that means nothing on a
        # different display/DPI setup -- see message_view_panel.
        # clamp_reading_font_size and DEFAULT_READING_FONT_POINT_SIZE
        # for how a set value is bounded and how the HTML view's zoom
        # percentage is derived from it. reading_font_family is a
        # literal font face name (e.g. "Consolas", "OpenDyslexic");
        # empty keeps whatever the control's own default face is.
        try:
            self.reading_font_size = (
                int(reading_font_size) if reading_font_size is not None else None
            )
        except (TypeError, ValueError):
            self.reading_font_size = None
        self.reading_font_family = str(reading_font_family or "").strip()

        # System tray (Settings > System tray & window). All four
        # default off, so an existing settings.json behaves exactly
        # as before ZBox ever had a tray icon -- the icon itself is
        # always shown while ZBox runs (Quit, Settings, and the
        # unread count are meant to stay reachable even with every
        # one of these off), only what minimizing/closing/launching
        # DO is gated by these.
        #
        # minimize_to_tray: the taskbar button disappears and ZBox
        # keeps running behind the tray icon when minimized, instead
        # of sitting in the taskbar as normal.
        #
        # close_to_tray: the X button, Alt+F4, and Ctrl+W pressed on
        # the Mail tab (which has nothing else to close -- see
        # main_frame._close_tab_at) hide ZBox to the tray instead of
        # quitting. File > Exit / Ctrl+Shift+Q and the tray icon's
        # own Quit item always really quit regardless of this
        # setting -- deliberately, so there is always one predictable
        # keyboard path to actually exit ZBox without having to reach
        # the tray icon at all.
        #
        # stay_maximized: ZBox launches maximized and is re-maximized
        # on any attempt to un-maximize it (dragging the title bar,
        # double-clicking it, the Restore button) -- see
        # _restore_window_state and _on_frame_geometry_changed. The
        # menu bar and title bar stay exactly as they are; this is
        # NOT wx's borderless ShowFullScreen, which would hide the
        # menu bar and break every Alt-key menu shortcut.
        #
        # run_at_windows_startup: adds/removes ZBox from the
        # per-user HKCU Run registry key (see startup_registration.py)
        # -- takes effect immediately when toggled in Settings, not
        # only on next launch. A startup-launched ZBox is passed
        # --minimized (main.py) and opens straight to the tray rather
        # than showing its window, per how this was scoped.
        self.minimize_to_tray = bool(minimize_to_tray)
        self.close_to_tray = bool(close_to_tray)
        self.stay_maximized = bool(stay_maximized)
        self.run_at_windows_startup = bool(run_at_windows_startup)

        # App lock (Settings > Privacy and Security). Off by
        # default, and deliberately so: turning it on is a decision
        # about who else uses this computer, not something an
        # existing install should acquire in an update.
        #
        # When on, launching ZBox asks for one of the assigned
        # unlock methods -- a passkey, a security key, or the master
        # password, any one of them -- before any account is opened.
        # "Full quit" is what it says: closing to the tray leaves
        # ZBox running and does not re-lock it, because it was never
        # unlocked twice. See unlock_dialog.unlock_at_startup for the
        # check itself and unlock_methods for what may answer it.
        #
        # With no methods registered and no master password set the
        # lock is skipped rather than enforced -- there would be
        # nothing to unlock it with, and a setting that can brick the
        # app is not a security feature.
        self.lock_on_full_quit = bool(lock_on_full_quit)

        # Protocol logging was removed in session w, along with Tools
        # > Capture Next Message Read. Both existed to put raw message
        # content where it could be read later, and a log a tester
        # sends must never carry their mail. The IMAP library's own
        # loggers are now pinned at WARNING in main.setup_logging with
        # no setting to raise them.

        # Trash and Junk auto-purge: messages older than this many
        # days are permanently removed once a day while ZBox is open
        # -- the same permanent removal Empty Trash/Empty Junk perform
        # on demand (see himalaya_client.purge_folder), just run
        # automatically. 0 disables it. Defaults on at 30 days rather
        # than an opt-in, per explicit request rather than added
        # behind a toggle nobody would find first.
        try:
            purge_days = int(auto_purge_days)
        except (TypeError, ValueError):
            purge_days = 30
        self.auto_purge_days = max(0, min(365, purge_days))

        # Cache clean up by message date (Tools > Cache Clean Up
        # Configuration). Once a day, while ZBox is idle, cached
        # message bodies and offline copies whose own Date is older
        # than this many days are deleted -- ZBox's local copies only,
        # including copies of messages still in the account. Limited
        # to the four ages the dialog offers. 30 matches the file-age
        # prune this replaced, so an existing install behaves the same
        # until someone picks another value.
        try:
            age_days = int(cache_max_message_age_days)
        except (TypeError, ValueError):
            age_days = 30
        self.cache_max_message_age_days = (
            age_days if age_days in CACHE_AGE_CHOICES else 30
        )
        # When that clean up last finished, as a time.time() value. 0
        # means never, so the first idle window after launch runs it.
        # Persisted so a restart does not reset the day. Deliberately
        # not in settings.default.json: it is state, not a preference.
        try:
            self.cache_cleanup_last_run = max(0.0, float(cache_cleanup_last_run or 0.0))
        except (TypeError, ValueError):
            self.cache_cleanup_last_run = 0.0

        # Junk rules (Tools > Privacy and Content Blocking; see
        # junk_rules.py). Off by default. The names are kept from the
        # learned spam filter they replaced on 16 September 2026, so an
        # existing settings.json keeps its choice.
        #
        # spam_filter_enabled checks new Inbox mail against the blocked
        # and allowed senders Mark as Junk and Mark as Not Junk build,
        # and the links in it against the phishing list. A hit only
        # marks the row. spam_auto_move_to_junk_enabled additionally
        # moves mail from a blocked sender to Junk; phishing hits are
        # never moved. The learned filter's other three settings
        # (flag, learn, threshold) are gone and ignored if present.
        self.spam_filter_enabled = bool(spam_filter_enabled)
        self.spam_auto_move_to_junk_enabled = bool(spam_auto_move_to_junk_enabled)

        # Whether the first-run EULA gate (ZBoxApp._show_eula_if_needed
        # in main_frame.py) has ever been accepted on this install.
        # False only until the very first launch's I Agree; once True
        # it is saved here so the gate never shows again. Never set
        # True by anything other than that one explicit choice.
        self.eula_accepted = bool(eula_accepted)

    @property
    def webview_focus_delay_ms(self):
        return self.webview_focus_delay_level * 100

    def resolved_attachment_save_dir(self):
        """The folder "Save All" writes attachments to: the
        configured attachment_save_dir with environment references
        and ~ expanded, or the default (USERPROFILE / Downloads /
        attachments) when nothing is configured."""
        custom = (self.attachment_save_dir or "").strip()
        if custom:
            return os.path.expandvars(os.path.expanduser(custom))
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return os.path.join(home, "Downloads", "attachments")

    def sender_allows_remote_content(self, address):
        address = (address or "").strip().lower()
        return bool(address) and address in self.remote_content_allowed_senders

    def allow_remote_content_from(self, address):
        """Adds a sender to the allow list. Returns True if this
        actually changed anything, so the caller knows whether to
        save."""
        address = (address or "").strip().lower()
        if not address or address in self.remote_content_allowed_senders:
            return False
        self.remote_content_allowed_senders = sorted(
            self.remote_content_allowed_senders + [address]
        )
        return True

    def to_dict(self):
        return {
            "debug_logging": self.debug_logging,
            "language": self.language,
            "unified_folder_types": self.unified_folder_types,
            "default_message_view": self.default_message_view,
            "html_silence_hold_ms": self.html_silence_hold_ms,
            "folder_pane_mode": self.folder_pane_mode,
            "webview_focus_delay_level": self.webview_focus_delay_level,
            "webview_backend": self.webview_backend,
            "webview2_runtime": self.webview2_runtime,
            "block_remote_content": self.block_remote_content,
            "blocklist_enabled": self.blocklist_enabled,
            "blocklist_auto_update": self.blocklist_auto_update,
            "remote_content_allowed_senders": self.remote_content_allowed_senders,
            "sort_key": self.sort_key,
            "sort_ascending": self.sort_ascending,
            "announcement_hold_ms": self.announcement_hold_ms,
            "announce_actions": self.announce_actions,
            "show_message_headers": self.show_message_headers,
            "attachment_save_dir": self.attachment_save_dir,
            "bare_key_shortcuts_enabled": self.bare_key_shortcuts_enabled,
            "draft_autosave_seconds": self.draft_autosave_seconds,
            "sounds_enabled": self.sounds_enabled,
            "sound_theme": self.sound_theme,
            "desktop_notifications_enabled": self.desktop_notifications_enabled,
            "ui_theme": self.ui_theme,
            "window_x": self.window_x,
            "window_y": self.window_y,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "window_maximized": self.window_maximized,
            "last_selected_account_id": self.last_selected_account_id,
            "last_selected_folder": self.last_selected_folder,
            "last_selected_unified_type": self.last_selected_unified_type,
            "outer_sash_position": self.outer_sash_position,
            "inner_sash_position": self.inner_sash_position,
            "reading_font_size": self.reading_font_size,
            "reading_font_family": self.reading_font_family,
            "minimize_to_tray": self.minimize_to_tray,
            "close_to_tray": self.close_to_tray,
            "stay_maximized": self.stay_maximized,
            "run_at_windows_startup": self.run_at_windows_startup,
            "lock_on_full_quit": self.lock_on_full_quit,
            "auto_purge_days": self.auto_purge_days,
            "cache_max_message_age_days": self.cache_max_message_age_days,
            "cache_cleanup_last_run": self.cache_cleanup_last_run,
            "spam_filter_enabled": self.spam_filter_enabled,
            "spam_auto_move_to_junk_enabled": self.spam_auto_move_to_junk_enabled,
            "eula_accepted": self.eula_accepted,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            debug_logging=data.get("debug_logging", True),
            language=data.get("language", "en"),
            unified_folder_types=data.get("unified_folder_types", ["Inbox"]),
            default_message_view=data.get("default_message_view", "html"),
            html_silence_hold_ms=data.get("html_silence_hold_ms", 100),
            folder_pane_mode=data.get("folder_pane_mode", "all"),
            webview_focus_delay_level=data.get("webview_focus_delay_level", 2),
            webview_backend=data.get("webview_backend", "edge"),
            webview2_runtime=data.get("webview2_runtime", "auto"),
            block_remote_content=data.get("block_remote_content", True),
            blocklist_enabled=data.get("blocklist_enabled", True),
            blocklist_auto_update=data.get("blocklist_auto_update", True),
            remote_content_allowed_senders=data.get(
                "remote_content_allowed_senders", []
            ),
            sort_key=data.get("sort_key", "date"),
            sort_ascending=data.get("sort_ascending", False),
            announcement_hold_ms=data.get("announcement_hold_ms", 1800),
            announce_actions=data.get("announce_actions", True),
            show_message_headers=data.get("show_message_headers", True),
            attachment_save_dir=data.get("attachment_save_dir", ""),
            bare_key_shortcuts_enabled=data.get("bare_key_shortcuts_enabled", True),
            draft_autosave_seconds=data.get("draft_autosave_seconds", 60),
            sounds_enabled=data.get("sounds_enabled", True),
            sound_theme=data.get("sound_theme", "default"),
            desktop_notifications_enabled=data.get("desktop_notifications_enabled", True),
            ui_theme=data.get("ui_theme", "system"),
            window_x=data.get("window_x"),
            window_y=data.get("window_y"),
            window_width=data.get("window_width", 1100),
            window_height=data.get("window_height", 700),
            window_maximized=data.get("window_maximized", False),
            last_selected_account_id=data.get("last_selected_account_id"),
            last_selected_folder=data.get("last_selected_folder"),
            last_selected_unified_type=data.get("last_selected_unified_type"),
            outer_sash_position=data.get("outer_sash_position", 220),
            inner_sash_position=data.get("inner_sash_position", 420),
            reading_font_size=data.get("reading_font_size"),
            reading_font_family=data.get("reading_font_family", ""),
            minimize_to_tray=data.get("minimize_to_tray", False),
            close_to_tray=data.get("close_to_tray", False),
            stay_maximized=data.get("stay_maximized", False),
            run_at_windows_startup=data.get("run_at_windows_startup", False),
            lock_on_full_quit=data.get("lock_on_full_quit", False),
            auto_purge_days=data.get("auto_purge_days", 30),
            cache_max_message_age_days=data.get("cache_max_message_age_days", 30),
            cache_cleanup_last_run=data.get("cache_cleanup_last_run", 0.0),
            spam_filter_enabled=data.get("spam_filter_enabled", False),
            spam_auto_move_to_junk_enabled=data.get(
                "spam_auto_move_to_junk_enabled", False
            ),
            eula_accepted=data.get("eula_accepted", False),
        )


class SettingsManager:
    def __init__(self, paths):
        self.paths = paths
        self.settings = self.load()

    def _load_shipped_defaults(self):
        """Settings for a checkout that has no settings.json yet,
        read from the tracked config/settings.default.json template.
        Falls back to Settings()'s own constructor defaults only if
        that template is missing or unreadable.

        A packaged build (sys.frozen) starts with debug logging off,
        regardless of what the template says -- source runs, where
        the log is actively used for diagnosis, keep the template's
        value. This only ever applies to the untouched, first-ever
        run: the moment settings.json exists, load() reads that
        instead and this method is never consulted again."""
        default_file = getattr(self.paths, "settings_default_file", None)
        settings = None
        if default_file and os.path.isfile(default_file):
            try:
                with open(default_file, "r", encoding="utf-8") as handle:
                    settings = Settings.from_dict(json.load(handle))
            except (OSError, ValueError):
                pass
        if settings is None:
            settings = Settings()
        if getattr(sys, "frozen", False):
            settings.debug_logging = False
        return settings

    def load(self):
        if not os.path.isfile(self.paths.settings_file):
            return self._load_shipped_defaults()
        try:
            with open(self.paths.settings_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return Settings.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return Settings()

    def save(self):
        with open(self.paths.settings_file, "w", encoding="utf-8") as handle:
            json.dump(self.settings.to_dict(), handle, indent=2)
