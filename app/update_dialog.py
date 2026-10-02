"""
The update prompt and the download progress dialog.

Both are built for a screen reader first: every control has a real
label, focus lands on the one button that matters, and nothing is
disabled into invisibility. The work itself (download, verify, unpack)
is in updater.py and runs on a background thread here, so the window
never freezes while the zip arrives.
"""

import threading

import wx

import lang
import updater


def _say(parent, text, title="ZBox"):
    """Speaks through the main window's announce(), which reaches the
    running screen reader. Silent when there is none to reach."""
    frame = parent
    while frame is not None and not hasattr(frame, "announce"):
        frame = frame.GetParent()
    if frame is not None:
        try:
            frame.announce(text, title=title)
        except Exception:  # noqa: BLE001 - speech must never break an update
            pass


class UpdatePromptDialog(wx.Dialog):
    """"ZBox 26.10.03 is available." Yes, update now / Remind me later.
    Returns wx.ID_YES or wx.ID_CANCEL from ShowModal; Escape is Remind
    me later."""

    def __init__(self, parent, current_label, info):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_update_available", default="ZBox Update"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        new_label = info.label or info.version_text
        message = wx.StaticText(self, label=lang.t(
            "dialogs", "update_available",
            default="ZBox {new} is available. You have {current}.",
            new=new_label, current=current_label or "an unknown version",
        ))
        notes_label = wx.StaticText(self, label=lang.control_label(
            "upd_notes_label", "Release &notes:",
        ))
        self.notes = wx.TextCtrl(
            self,
            value=info.notes or lang.t("dialogs", "upd_no_notes", default="No release notes."),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            size=(560, 260),
        )
        self.notes.SetName(lang.t("dialogs", "upd_notes_name", default="Release notes"))

        self.yes_button = wx.Button(self, wx.ID_YES, lang.control_label(
            "upd_update_now", "&Yes, update now",
        ))
        later_button = wx.Button(self, wx.ID_CANCEL, lang.control_label(
            "upd_remind_later", "&Remind me later",
        ))
        self.yes_button.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_YES))
        self.SetAffirmativeId(wx.ID_YES)
        self.SetEscapeId(wx.ID_CANCEL)
        self.yes_button.SetDefault()

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.yes_button, 0, wx.RIGHT, 8)
        buttons.Add(later_button, 0)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(message, 0, wx.ALL, 12)
        sizer.Add(notes_label, 0, wx.LEFT | wx.RIGHT, 12)
        sizer.Add(self.notes, 1, wx.EXPAND | wx.ALL, 12)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()
        wx.CallAfter(self.yes_button.SetFocus)


class UpdateProgressDialog(wx.Dialog):
    """Downloads, verifies and unpacks one update. ShowModal returns
    wx.ID_OK with staged_root set when the update is ready to apply,
    or wx.ID_CANCEL after a cancel or a failure (a failure has already
    been shown by then)."""

    def __init__(self, parent, info, data_dir, current_label):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_update_download", default="Downloading ZBox Update"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.info = info
        self.data_dir = data_dir
        self.current_label = current_label
        self.staged_root = None
        self._cancel = threading.Event()
        self._finished = False
        self._spoken_quarter = 0

        self.status = wx.StaticText(self, label=lang.t(
            "dialogs", "upd_downloading",
            default="Downloading ZBox {version}...",
            version=info.label or info.version_text,
        ), size=(420, -1))
        self.gauge = wx.Gauge(self, range=1000, size=(420, -1))
        self.gauge.SetName(lang.t("dialogs", "upd_progress_name", default="Download progress"))
        self.cancel_button = wx.Button(self, wx.ID_CANCEL, lang.control_label(
            "upd_cancel", "&Cancel",
        ))
        self.cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
        self.Bind(wx.EVT_CLOSE, self._on_cancel)
        self.SetEscapeId(wx.ID_CANCEL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.status, 0, wx.ALL, 12)
        sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        sizer.Add(self.cancel_button, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()
        wx.CallAfter(self.cancel_button.SetFocus)
        threading.Thread(target=self._work, name="ZBoxUpdateDownload", daemon=True).start()

    # --- Background thread -----------------------------------------

    def _work(self):
        try:
            zip_path = updater.download(
                self.info, self.data_dir, label=self.current_label,
                progress=lambda done, total: wx.CallAfter(self._on_progress, done, total),
                cancelled=self._cancel.is_set,
            )
            wx.CallAfter(self._on_stage_start)
            root = updater.stage(zip_path, self.data_dir, self.info.version)
        except updater.UpdateCancelled:
            wx.CallAfter(self._finish, None, None)
            return
        except updater.UpdateError as exc:
            updater.discard_staged(self.data_dir)
            wx.CallAfter(self._finish, None, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported, never raised into wx
            updater.discard_staged(self.data_dir)
            wx.CallAfter(self._finish, None, str(exc))
            return
        wx.CallAfter(self._finish, root, None)

    # --- Main thread -----------------------------------------------

    def _alive(self):
        return not self._finished and bool(self)

    def _on_progress(self, done, total):
        if not self._alive() or not total:
            return
        self.gauge.SetValue(min(1000, int(done * 1000 / total)))
        quarter = int(done * 4 / total)
        if quarter > self._spoken_quarter and quarter < 4:
            self._spoken_quarter = quarter
            _say(self.GetParent(), lang.t(
                "actions_announcements", "upd_percent",
                default="{percent} percent", percent=quarter * 25,
            ))

    def _on_stage_start(self):
        if not self._alive():
            return
        self.gauge.SetValue(1000)
        text = lang.t("actions_announcements", "upd_download_done",
                      default="Download complete. Preparing the update...")
        self.status.SetLabel(text)
        _say(self.GetParent(), text)

    def _on_cancel(self, _event):
        if self._finished:
            return
        self._cancel.set()
        self.status.SetLabel(lang.t("dialogs", "upd_cancelling", default="Cancelling..."))

    def _finish(self, root, error):
        if self._finished or not bool(self):
            return
        self._finished = True
        if root:
            self.staged_root = root
            self.EndModal(wx.ID_OK)
            return
        if error:
            wx.MessageBox(
                lang.t(
                    "errors", "update_failed",
                    default="The update could not be downloaded, so nothing was changed.\n\n{error}",
                    error=error,
                ),
                lang.t("dialogs", "title_update_available", default="ZBox Update"),
                wx.OK | wx.ICON_ERROR, self,
            )
        self.EndModal(wx.ID_CANCEL)
