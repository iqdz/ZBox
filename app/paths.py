"""
Holds the resolved portable folder paths so every module reaches
them through one object instead of recomputing relative paths.
"""

import os


class Paths:
    """The portable layout, resolved once in main.py and passed
    down. Root holds the executable, apps_files (PyInstaller runtime
    and the bundled WebView2), himalaya (himalaya.exe) and data
    (everything the user owns: config, userdata, cache, logs,
    sounds).

    userdata and cache are deliberately separate roots. userdata
    holds what cannot be re-created -- contacts.json, blocklists, and
    each account's thread_state.json. cache holds only what a
    re-download rebuilds: the offline Maildir copies and the
    per-message JSON bodies. Anything under cache may be deleted at
    any time by anyone, which is what makes Clear Offline Message
    Cache safe to offer as one keystroke; before the split it had to
    enumerate the same directory that holds the address book."""

    def __init__(self, base, app, apps_files, himalaya, data,
                 userdata, cache, config, logs):
        self.base = base
        self.app = app
        self.apps_files = apps_files
        self.himalaya = himalaya
        self.data = data
        self.userdata = userdata
        self.cache = cache
        self.config = config
        self.logs = logs

    @property
    def accounts_file(self):
        return os.path.join(self.config, "accounts.json")

    @property
    def settings_file(self):
        return os.path.join(self.config, "settings.json")

    @property
    def settings_default_file(self):
        """Template of the shipped defaults, tracked in git. The
        live settings.json is deliberately untracked (it holds one
        user's personal preferences), so this is what a fresh
        checkout seeds from on first run -- without it, a missing
        settings.json would silently fall back to the constructor
        defaults in Settings.__init__, which are not the values ZBox
        actually ships with."""
        return os.path.join(self.config, "settings.default.json")

    @property
    def himalaya_binary(self):
        return os.path.join(self.himalaya, "himalaya.exe")

    @property
    def webview2_runtime_dir(self):
        """Folder a bundled WebView2 Fixed Version runtime is
        expanded into. Kept inside apps_files, with the app's other
        dependencies, so the whole ZBox folder stays portable and the
        runtime travels with it; see docs/webview2_fixed_runtime.md.
        Deliberately not under data/, which is the user's own folder
        and would otherwise gain 250 MB of vendored runtime."""
        return os.path.join(self.apps_files, "webview2")

    @property
    def himalaya_config_file(self):
        return os.path.join(self.config, "himalaya.toml")

    @property
    def main_script(self):
        """ZBox's entry point, used to build himalaya.toml's
        password.command when running from source. Frozen builds
        invoke the executable itself with --get-secret instead."""
        return os.path.join(self.base, "main.py")

    @property
    def sounds_dir(self):
        """Root of the sound-theme system (audio notifications
        settings): data/sounds/default/ ships the built-in theme; a
        user can drop a sibling folder here (e.g.
        data/sounds/custom-theme/) with the same file names to make a
        custom theme. Under data/ rather than apps_files/ so adding a
        theme never means opening the dependency folder. See
        sound_manager.py for how a theme and its fallback to default
        are resolved."""
        return os.path.join(self.data, "sounds")

    @property
    def lang_dir(self):
        """Where the interface translations live: data/lang/en/en.lang
        ships the English strings, each language its own folder named
        for its code (data/lang/fr/fr.lang, data/lang/ar/ar.lang),
        holding that language's .lang file and its about.txt/
        shortcuts.txt if it has them. Same reasoning as sounds_dir --
        under data/ because it belongs to the user, so adding a
        language never means opening the dependency folder, and a
        folder put there survives an update. See lang.py for how a
        chosen language is laid over English."""
        return os.path.join(self.data, "lang")

    @property
    def documentation_file(self):
        """The manual shown by Help > Documentation.

        Two locations, because the source tree and a built app are
        not shaped the same: docs/ sits at the root when running from
        source, and PyInstaller bundles the same file into
        apps_files/docs/ for the build (zbox.spec), where the root is
        kept to the executable plus apps_files, himalaya and data.
        Source wins when both exist, so an edited manual is what a
        developer sees.
        """
        from_source = os.path.join(self.base, "docs", "zbox_documentation.txt")
        if os.path.isfile(from_source):
            return from_source
        return os.path.join(self.apps_files, "docs", "zbox_documentation.txt")

    @property
    def license_file(self):
        """ZBox's license and third-party attributions, shown by the
        first-run EULA gate and reachable afterward from Help > About
        ZBox's View License button.

        Same two-location resolution as documentation_file: docs/ at
        the root when running from source, apps_files/docs/ once
        PyInstaller has bundled it for a build (zbox.spec). Source
        wins when both exist.
        """
        from_source = os.path.join(self.base, "docs", "LICENSE.txt")
        if os.path.isfile(from_source):
            return from_source
        return os.path.join(self.apps_files, "docs", "LICENSE.txt")

    def profile_dir(self, account_id):
        """An account's own directory under userdata, for the things
        that are not re-creatable -- today just thread_state.json.
        Cache belongs in cache_dir below, never here."""
        path = os.path.join(self.userdata, account_id)
        os.makedirs(path, exist_ok=True)
        return path

    def cache_dir(self, account_id):
        """An account's own directory under cache, holding
        offline_mail (the Maildir copies) and message_cache (the
        per-message JSON bodies). Everything in here is re-fetchable
        by definition, so it is safe for anything to delete it."""
        path = os.path.join(self.cache, account_id)
        os.makedirs(path, exist_ok=True)
        return path

    @property
    def junk_rules_file(self):
        """The junk rules' blocked senders, allowed senders and exempt
        messages (see junk_rules.py), one file for every account.
        Under userdata, not cache: these are the user's own decisions
        and cannot be re-fetched. Unencrypted, same reasoning as
        thread_state.json and junk_origin.json: addresses and
        Message-IDs, no secrets and no message content.

        The learned filter this replaced kept spam_filter.sqlite3 in
        the same folder. Nothing reads it any more; it is left for the
        user to delete rather than removed behind their back.
        """
        return os.path.join(self.userdata, "junk_rules.json")
