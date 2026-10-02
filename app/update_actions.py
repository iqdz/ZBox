"""
The main window's side of updates: the Help > Updates submenu, the
idle-time check, the prompt, installing on exit, and what happens on
the first start after an update. Mixed into ZBoxMainFrame the same way
MailFetchMixin is, so main_frame.py only gains the hooks.

Automatic work is silent, per ZBox's rule for background work: an
automatic check never speaks or writes a status line, and a failure is
only logged. The prompt is the one thing it may show, and only when a
newer version exists. Everything the user starts is announced.
"""

import logging
import os
import time
import webbrowser

import wx

import lang
import single_instance
import updater
from update_dialog import UpdateProgressDialog, UpdatePromptDialog

ID_CHECK_UPDATES = wx.NewIdRef()
ID_UPDATE_SETTINGS = wx.NewIdRef()
ID_RELEASES_PAGE = wx.NewIdRef()

REMIND_LATER_SECONDS = 24 * 60 * 60
_log = logging.getLogger("zbox.update")


class UpdateActionsMixin:
    """Needs from the frame: paths, settings_manager, worker,
    sync_worker, announce(), _app_last_input, _current_message_tab(),
    _on_settings() and _on_exit()."""

    # --- Setup -------------------------------------------------------

    def _init_updates(self, offline_menu_id=None):
        self._update_offline_menu_id = offline_menu_id
        self._update_check_running = False
        self._update_staged_root = None
        self._update_apply_on_exit = False
        self._settings_focus_updates = False
        self.Bind(wx.EVT_MENU, self._on_check_updates, id=ID_CHECK_UPDATES)
        self.Bind(wx.EVT_MENU, self._on_update_settings, id=ID_UPDATE_SETTINGS)
        self.Bind(wx.EVT_MENU, self._on_releases_page, id=ID_RELEASES_PAGE)
        self._update_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self._maybe_check_for_updates(), self._update_timer)
        self._update_timer.Start(5 * 60 * 1000)
        self._adopt_staged_update()

    def _append_update_menu(self, help_menu):
        submenu = wx.Menu()
        submenu.Append(ID_CHECK_UPDATES, lang.menu_label(
            "help_check_updates", "&Check for Updates Now..."))
        submenu.Append(ID_UPDATE_SETTINGS, lang.menu_label(
            "help_update_settings", "Update &Settings..."))
        submenu.Append(ID_RELEASES_PAGE, lang.menu_label(
            "help_releases_page", "Open the &Releases Page"))
        help_menu.AppendSubMenu(submenu, lang.menu_label("help_updates", "&Updates"))

    def _adopt_staged_update(self):
        """An update downloaded earlier (silent mode, or Update now
        followed by Keep editing) is applied at this session's exit
        when it is still newer than this ZBox; anything else left in
        data/update is removed with one quick try that can never stop
        ZBox starting. When the staged update is the version now
        running, this is the first start after that update and the
        apply step may still be running from the staging folder, so
        it is left for the clean up two minutes later."""
        label = updater.staged_label(self.paths.data)
        if not label:
            return
        staged = updater.parse_version(label)
        current = updater.current_version(self.paths)
        if updater.is_frozen() and updater.is_newer(staged, current):
            self._update_staged_root = os.path.join(
                updater.update_dir(self.paths.data), "staging", updater.ZIP_ROOT,
            )
            self._update_apply_on_exit = True
            _log.info("A downloaded update (%s) will be applied at exit.", label)
            return
        data_dir = self.paths.data
        if staged is not None and current is not None and tuple(staged) == tuple(current):
            wx.CallLater(120000, lambda: updater.discard_staged(data_dir, quick=True))
            return
        updater.discard_staged(data_dir, quick=True)

    # --- Automatic check --------------------------------------------

    def _is_working_offline(self):
        if self._update_offline_menu_id is None:
            return False
        try:
            item = self.GetMenuBar().FindItemById(self._update_offline_menu_id)
            return bool(item and item.IsCheckable() and item.IsChecked())
        except Exception:  # noqa: BLE001
            return False

    def _maybe_check_for_updates(self):
        if self._update_check_running or self._update_apply_on_exit:
            return
        settings = self.settings_manager.settings
        now = time.time()
        if not updater.update_check_due(
            now,
            getattr(settings, "update_last_check", 0.0),
            getattr(settings, "update_remind_after", 0.0),
            time.monotonic() - self._app_last_input,
            self._current_message_tab() is not None,
            auto_check=getattr(settings, "update_auto_check", True),
            frozen=updater.is_frozen(),
            offline=self._is_working_offline(),
        ):
            return
        self._update_check_running = True
        paths = self.paths

        def on_success(info):
            self._update_check_running = False
            self.settings_manager.settings.update_last_check = now
            self.settings_manager.save()
            if info is None:
                return
            if getattr(self.settings_manager.settings, "update_mode", "ask") == "silent":
                self._download_silently(info)
            else:
                self._show_update_prompt(info, automatic=True)

        def on_error(exc):
            self._update_check_running = False
            _log.warning("Automatic update check failed: %s", exc)

        self.sync_worker.submit(lambda: updater.check_for_update(paths), on_success, on_error)

    def _download_silently(self, info):
        paths = self.paths
        label = updater.current_label(paths)

        def work():
            zip_path = updater.download(info, paths.data, label=label)
            return updater.stage(zip_path, paths.data, info.version)

        def on_success(root):
            self._update_staged_root = root
            self._update_apply_on_exit = True
            _log.info("Update %s downloaded; it will be applied at exit.", info.label)

        def on_error(exc):
            updater.discard_staged(paths.data)
            _log.warning("Silent update download failed: %s", exc)

        self.sync_worker.submit(work, on_success, on_error)

    # --- Manual check -----------------------------------------------

    def _on_check_updates(self, event):
        if self._update_check_running:
            return
        if self._update_apply_on_exit:
            text = lang.t(
                "main_ui", "upd_ready_at_exit",
                default="An update is already downloaded. It will be installed when you exit ZBox.",
            )
            self.GetStatusBar().SetStatusText(text)
            self.announce(text)
            return
        checking = lang.t("main_ui", "upd_checking", default="Checking for updates...")
        self.GetStatusBar().SetStatusText(checking)
        self.announce(checking)
        self._update_check_running = True
        paths = self.paths

        def on_success(info):
            self._update_check_running = False
            self.settings_manager.settings.update_last_check = time.time()
            self.settings_manager.save()
            if info is None:
                text = lang.t("main_ui", "upd_up_to_date", default="ZBox is up to date.")
                self.GetStatusBar().SetStatusText(text)
                self.announce(text)
                return
            if not updater.is_frozen():
                text = lang.t(
                    "main_ui", "upd_available_source",
                    default="ZBox {version} is available. This copy runs from source, so update it with git.",
                    version=info.label or info.version_text,
                )
                self.GetStatusBar().SetStatusText(text)
                self.announce(text)
                return
            self._show_update_prompt(info, automatic=False)

        def on_error(exc):
            self._update_check_running = False
            text = lang.t(
                "main_ui", "upd_check_failed",
                default="Could not check for updates: {error}", error=exc,
            )
            self.GetStatusBar().SetStatusText(text)
            wx.MessageBox(
                text,
                lang.t("dialogs", "title_update_available", default="ZBox Update"),
                wx.OK | wx.ICON_ERROR, self,
            )

        self.worker.submit(lambda: updater.check_for_update(paths), on_success, on_error)

    # --- Prompt and install -----------------------------------------

    def _modal_dialog_open(self):
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                return True
        return False

    def _show_update_prompt(self, info, automatic):
        if automatic and (self._modal_dialog_open() or not self.IsShown()):
            # Never stack on another dialog, and never pop up while
            # ZBox is hidden in the tray: try again at the next tick.
            self.settings_manager.settings.update_last_check = 0.0
            self.settings_manager.save()
            return
        dialog = UpdatePromptDialog(self, updater.current_label(self.paths), info)
        try:
            result = dialog.ShowModal()
        finally:
            dialog.Destroy()
        if result == wx.ID_YES:
            self._install_update_now(info)
            return
        self.settings_manager.settings.update_remind_after = time.time() + REMIND_LATER_SECONDS
        self.settings_manager.save()

    def _install_update_now(self, info):
        dialog = UpdateProgressDialog(
            self, info, self.paths.data, updater.current_label(self.paths),
        )
        try:
            result = dialog.ShowModal()
            root = dialog.staged_root
        finally:
            dialog.Destroy()
        if result != wx.ID_OK or not root:
            return
        self._update_staged_root = root
        self._update_apply_on_exit = True
        self._on_exit(None)
        try:
            closing = bool(getattr(self, "_shutting_down", False))
        except RuntimeError:
            closing = True
        if not closing:
            # Keep editing was chosen for an unsent message: ZBox stays
            # open, and the update waits for the next full exit.
            self._quitting = False
            text = lang.t(
                "main_ui", "upd_ready_at_exit",
                default="An update is already downloaded. It will be installed when you exit ZBox.",
            )
            self.GetStatusBar().SetStatusText(text)
            self.announce(text)

    def _apply_staged_update_on_exit(self):
        """Called from _on_close once the quit is certain. Starts the
        apply step, which waits for this process to end."""
        if not self._update_apply_on_exit or not self._update_staged_root:
            return
        # The new ZBox is started by the apply step, a background
        # process, so Windows would not let its window take the
        # keyboard. This ZBox was just used, so it may pass that right
        # on, the same way a second launch of ZBox does.
        single_instance.grant_foreground()
        try:
            updater.start_apply(
                self.paths.base, self._update_staged_root, os.getpid(),
                updater.current_label(self.paths),
            )
            _log.info("Update apply step started from %s.", self._update_staged_root)
        except Exception as exc:  # noqa: BLE001 - never stop ZBox closing
            _log.warning("Could not start the update apply step: %s", exc)

    # --- Other menu items -------------------------------------------

    def _on_update_settings(self, event):
        self._settings_focus_updates = True
        try:
            self._on_settings(event)
        finally:
            self._settings_focus_updates = False

    def _on_releases_page(self, event):
        try:
            webbrowser.open(updater.RELEASES_PAGE)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(
                lang.t(
                    "errors", "open_link_failed",
                    default="Could not open your browser for:\n{url}",
                    url=updater.RELEASES_PAGE,
                ),
                lang.t("dialogs", "title_open_link", default="Open Link"),
                wx.OK | wx.ICON_ERROR, self,
            )
            _log.warning("Could not open the releases page: %s", exc)

    # --- First start after an update --------------------------------

    def _focus_after_update(self):
        """The first start after an update, or after a failed one's
        restore: the window to the front and the keyboard on the
        message list, as when a second launch brings ZBox forward."""
        try:
            single_instance.force_foreground(self.GetHandle())
            self.restore_from_tray()
        except Exception as exc:  # noqa: BLE001 - only focus depends on it
            _log.warning("Could not focus the message list after the update: %s", exc)

    def after_update_start(self, updated_from, update_failed):
        if updated_from is not None or update_failed:
            wx.CallAfter(self._focus_after_update)
        if updated_from is not None:
            updater.mark_started(self.paths.data)
            label = updater.current_label(self.paths)
            wx.CallLater(2000, lambda: self.announce(lang.t(
                "actions_announcements", "upd_updated_to",
                default="ZBox updated to {version}.", version=label,
            )))
            wx.CallLater(120000, lambda: updater.cleanup_after_start(self.paths.data))
        if update_failed:
            wx.CallLater(1500, lambda: wx.MessageBox(
                lang.t(
                    "errors", "update_rolled_back",
                    default="The update could not start, so the previous version was restored.",
                ),
                lang.t("dialogs", "title_update_available", default="ZBox Update"),
                wx.OK | wx.ICON_WARNING, self,
            ))
