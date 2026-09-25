"""
Tools > Privacy and Content Blocking.

Kept out of the main Settings dialog on purpose. Settings is already
a tall single column of screen-reader tuning controls with no
scrolling, and Thunderbird keeps privacy in its own pane too, so
this follows both the existing structure and the parity rule.

Everything here answers one question: what is allowed to load when
an HTML message opens, and what is allowed to phone home.
"""

import wx

import lang
from accessible import fit_dialog, wrap_text


class PrivacyDialog(wx.Dialog):
    def __init__(
        self, parent, settings, blocklist=None, on_update_now=None,
        junk_rules=None, phishing_list=None, on_update_phishing=None,
    ):
        super().__init__(parent,
                         title=lang.t(
                             "dialogs", "title_privacy_dialog",
                             default="Privacy and Content Blocking",
                         ),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.settings = settings
        self.blocklist = blocklist
        self.on_update_now = on_update_now
        # Junk rules (junk_rules.py): the sender lists are edited here
        # and written back on OK; the phishing list is only reported
        # on and refreshed through main_frame, which owns the worker.
        # phishing_list is None until the rules have been on once.
        self.junk_rules = junk_rules
        self.phishing_list = phishing_list
        self.on_update_phishing = on_update_phishing

        sizer = wx.BoxSizer(wx.VERTICAL)

        # --- remote content -------------------------------------------

        remote_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_remote_label",
                default="Remote content in messages:",
            ),
        )
        remote_label.SetFont(remote_label.GetFont().Bold())

        self.block_remote_checkbox = wx.CheckBox(
            self,
            label=lang.control_label(
                "priv_block_remote",
                "&Block remote content until I ask for it (recommended)",
            ),
        )
        self.block_remote_checkbox.SetValue(
            getattr(settings, "block_remote_content", True)
        )

        remote_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_note_remote",
                default=(
                    "Images, stylesheets and fonts that a message loads from "
                    "the internet tell the sender the moment you opened it, "
                    "and from where. With this on, none of them load: a "
                    "notice appears above the message saying what was "
                    "blocked, and Ctrl+Shift+I loads it for that one "
                    "message. Pictures attached to the message itself "
                    "always show. Scripts and embedded frames are removed "
                    "either way and are never loaded."
                ),
            ),
        )
        wrap_text(remote_note)

        sizer.Add(remote_label, 0, wx.ALL, 10)
        sizer.Add(self.block_remote_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(remote_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # --- blocklist -------------------------------------------------

        list_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_blocklist_label",
                default="Ad and tracker blocklist:",
            ),
        )
        list_label.SetFont(list_label.GetFont().Bold())

        self.blocklist_checkbox = wx.CheckBox(
            self,
            label=lang.control_label(
                "priv_block_known", "Block &known ad and tracker domains",
            ),
        )
        self.blocklist_checkbox.SetValue(
            getattr(settings, "blocklist_enabled", True)
        )

        self.auto_update_checkbox = wx.CheckBox(
            self,
            label=lang.control_label(
                "priv_update_daily", "&Update the list daily in the background",
            ),
        )
        self.auto_update_checkbox.SetValue(
            getattr(settings, "blocklist_auto_update", True)
        )

        self.status_label = wx.StaticText(self, label=self._status_text())
        wrap_text(self.status_label)

        self.update_button = wx.Button(
            self, label=lang.control_label("priv_update_now", "Update &Now")
        )
        self.update_button.Bind(wx.EVT_BUTTON, self._on_update_now)

        list_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_note_blocklist",
                default=(
                    "This is the second layer. It matters once you load a "
                    "message's remote content, or allow a sender: the "
                    "pictures load, but anything served from a known "
                    "tracking or advertising domain still does not. The "
                    "list is downloaded from public ad and tracker lists "
                    "and merged; updating takes a second or two and runs "
                    "in the background."
                ),
            ),
        )
        wrap_text(list_note)

        sizer.Add(list_label, 0, wx.ALL, 10)
        sizer.Add(self.blocklist_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.auto_update_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(self.status_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(self.update_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(list_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # --- junk rules ------------------------------------------------

        junk_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_junk_label", default="Junk mail rules:",
            ),
        )
        junk_label.SetFont(junk_label.GetFont().Bold())

        self.spam_filter_checkbox = wx.CheckBox(
            self,
            label=lang.control_label(
                "priv_check_inbox",
                "&Check new Inbox mail against blocked senders and known "
                "phishing links",
            ),
        )
        self.spam_filter_checkbox.SetValue(
            getattr(settings, "spam_filter_enabled", False)
        )
        self.spam_filter_checkbox.Bind(wx.EVT_CHECKBOX, self._on_spam_filter_checkbox)

        junk_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_note_junk",
                default=(
                    "No guessing. Mark as Junk adds the sender to the "
                    "blocked list below, unless the message came through a "
                    "mailing list. Mark as Not Junk adds the sender to the "
                    "never-junk list and that message is never checked "
                    "again. A new message from a blocked sender is marked "
                    "Blocked Sender in the list; one that links to a known "
                    "phishing or malware site is marked Possible Phishing. "
                    "Your mail provider's own spam filtering still runs as "
                    "before."
                ),
            ),
        )
        wrap_text(junk_note)

        self.spam_auto_move_checkbox = wx.CheckBox(
            self,
            label=lang.control_label(
                "priv_move_blocked",
                "Automatically &move mail from blocked senders to Junk",
            ),
        )
        self.spam_auto_move_checkbox.SetValue(
            getattr(settings, "spam_auto_move_to_junk_enabled", False)
        )

        self.phishing_status_label = wx.StaticText(self, label=self._phishing_status_text())
        wrap_text(self.phishing_status_label)
        self.phishing_update_button = wx.Button(
            self,
            label=lang.control_label(
                "priv_update_phishing", "Update &Phishing List Now",
            ),
        )
        self.phishing_update_button.Bind(wx.EVT_BUTTON, self._on_update_phishing)

        sizer.Add(junk_label, 0, wx.ALL, 10)
        sizer.Add(self.spam_filter_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(junk_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(self.spam_auto_move_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(self.phishing_status_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(self.phishing_update_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        blocked = list(self.junk_rules.blocked) if self.junk_rules is not None else []
        allowed = list(self.junk_rules.allowed) if self.junk_rules is not None else []
        # Remembered so OK can send only what was removed here. This
        # dialog's lists are a snapshot taken now; writing them back
        # whole on OK erased any sender Mark as Junk added in the
        # meantime, which looked exactly like Mark as Junk failing.
        self._blocked_at_open = list(blocked)
        self._allowed_at_open = list(allowed)
        row_height = self.GetCharHeight() or 16

        blocked_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_blocked_label", default="Blocked senders:",
            ),
        )
        self.blocked_list = wx.ListBox(self, choices=blocked, style=wx.LB_SINGLE)
        self.blocked_list.SetName(
            lang.t("dialogs", "priv_blocked_name", default="Blocked senders")
        )
        self.blocked_list.SetMinSize((-1, row_height * 4))
        self.remove_blocked_button = wx.Button(
            self,
            label=lang.control_label(
                "priv_remove_blocked", "R&emove Blocked Sender",
            ),
        )
        self.remove_blocked_button.Bind(
            wx.EVT_BUTTON, lambda _e: self._remove_selected(self.blocked_list)
        )

        allowed_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_never_junk_label",
                default="Senders never treated as junk:",
            ),
        )
        self.allowed_junk_list = wx.ListBox(self, choices=allowed, style=wx.LB_SINGLE)
        self.allowed_junk_list.SetName(
            lang.t(
                "dialogs", "priv_never_junk_name",
                default="Senders never treated as junk",
            )
        )
        self.allowed_junk_list.SetMinSize((-1, row_height * 4))
        self.remove_allowed_junk_button = wx.Button(
            self,
            label=lang.control_label(
                "priv_remove_never_junk", "Remove Never-Junk &Sender",
            ),
        )
        self.remove_allowed_junk_button.Bind(
            wx.EVT_BUTTON, lambda _e: self._remove_selected(self.allowed_junk_list)
        )

        sizer.Add(blocked_label, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.blocked_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.remove_blocked_button, 0, wx.ALL, 16)
        sizer.Add(allowed_label, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.allowed_junk_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.remove_allowed_junk_button, 0, wx.ALL, 16)

        self._sync_spam_enabled_state()

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # --- allowed senders -------------------------------------------

        senders_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "priv_allowed_label",
                default="Senders allowed to load remote content:",
            ),
        )
        senders_label.SetFont(senders_label.GetFont().Bold())

        self.senders_list = wx.ListBox(
            self,
            choices=list(getattr(settings, "remote_content_allowed_senders", [])),
            style=wx.LB_SINGLE,
        )
        self.senders_list.SetName(
            lang.t("dialogs", "priv_allowed_name", default="Allowed senders")
        )
        # Fit sizes a dialog to its contents, and an empty ListBox has
        # almost no contents -- so give it a floor measured in rows of
        # the current font rather than pixels, and it keeps that floor
        # at any text scale.
        row_height = self.senders_list.GetCharHeight() or 16
        self.senders_list.SetMinSize((-1, row_height * 5))

        self.remove_button = wx.Button(
            self,
            label=lang.control_label(
                "priv_remove_selected", "&Remove Selected Sender",
            ),
        )
        self.remove_button.Bind(wx.EVT_BUTTON, self._on_remove_sender)

        sizer.Add(senders_label, 0, wx.ALL, 10)
        sizer.Add(self.senders_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.remove_button, 0, wx.ALL, 16)

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)
        self._sync_enabled_state()
        self.blocklist_checkbox.Bind(wx.EVT_CHECKBOX, self._on_blocklist_toggle)

    # -- helpers ---------------------------------------------------------

    def _status_text(self):
        if self.blocklist is None:
            return lang.t('dialogs', 'priv_status_unavailable', default="Blocklist status is unavailable.")
        return self.blocklist.status_text()

    def _sync_enabled_state(self):
        enabled = self.blocklist_checkbox.GetValue()
        self.auto_update_checkbox.Enable(enabled)
        self.update_button.Enable(enabled)

    def _on_blocklist_toggle(self, _event):
        self._sync_enabled_state()

    def _phishing_status_text(self):
        if self.phishing_list is None:
            return (
                lang.t('dialogs', 'priv_phishing_not_loaded', default="Phishing list: not loaded yet. It downloads the first "
                "time these rules are on and ZBox is online.")
            )
        return (lang.t('dialogs', 'priv_phishing_prefix', default='Phishing list:') + ' ') + self.phishing_list.status_text()

    def _sync_spam_enabled_state(self):
        enabled = self.spam_filter_checkbox.GetValue()
        for control in (self.spam_auto_move_checkbox, self.phishing_update_button):
            control.Enable(enabled)

    def _on_spam_filter_checkbox(self, _event):
        self._sync_spam_enabled_state()

    def _on_update_phishing(self, _event):
        if self.on_update_phishing is None:
            return
        self.on_update_phishing()
        self.phishing_status_label.SetLabel(
            lang.t(
                "dialogs", "priv_status_phishing_updating",
                default=(
                    "Phishing list: updating in the background. Reopen this "
                    "dialog to see the result."
                ),
            )
        )
        wrap_text(self.phishing_status_label)
        self.Layout()
        # A StaticText changing is not spoken by any reader, so the
        # button appeared to do nothing. The frame owns the
        # announcement channel and is this dialog's own parent;
        # getattr keeps it a no-op for any other parent rather than
        # giving the dialog a frame reference it otherwise has no use
        # for.
        announce_action = getattr(self.GetParent(), "announce_action", None)
        if announce_action is not None:
            announce_action(
                lang.t(
                    "actions_announcements", "phishing_list_updating",
                    default="Phishing list updating in the background.",
                ),
                title=lang.t("dialogs", "title_privacy", default="Privacy"),
            )

    def _remove_selected(self, list_box):
        index = list_box.GetSelection()
        if index == wx.NOT_FOUND:
            return
        list_box.Delete(index)
        count = list_box.GetCount()
        if count:
            list_box.SetSelection(min(index, count - 1))
        list_box.SetFocus()

    def _on_update_now(self, _event):
        if self.on_update_now is None:
            return
        # The refresh runs on a background worker, so the dialog stays
        # responsive and this label is refreshed on the next open
        # rather than blocking here waiting for a download.
        self.on_update_now()
        self.status_label.SetLabel(
            lang.t(
                "dialogs", "priv_status_updating",
                default=(
                    "Updating in the background. Reopen this dialog to see "
                    "the result."
                ),
            )
        )
        wrap_text(self.status_label)
        self.Layout()

    def _on_remove_sender(self, _event):
        index = self.senders_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        self.senders_list.Delete(index)
        count = self.senders_list.GetCount()
        if count:
            self.senders_list.SetSelection(min(index, count - 1))
        self.senders_list.SetFocus()

    def apply_to_settings(self):
        self.settings.block_remote_content = self.block_remote_checkbox.GetValue()
        self.settings.blocklist_enabled = self.blocklist_checkbox.GetValue()
        self.settings.blocklist_auto_update = self.auto_update_checkbox.GetValue()
        self.settings.remote_content_allowed_senders = sorted(
            self.senders_list.GetString(index)
            for index in range(self.senders_list.GetCount())
        )
        self.settings.spam_filter_enabled = self.spam_filter_checkbox.GetValue()
        self.settings.spam_auto_move_to_junk_enabled = (
            self.spam_auto_move_checkbox.GetValue()
        )
        if self.junk_rules is not None:
            # Only removals are possible here, so only the removals are
            # sent -- never both lists whole. See _blocked_at_open.
            blocked_now = {
                self.blocked_list.GetString(i) for i in range(self.blocked_list.GetCount())
            }
            allowed_now = {
                self.allowed_junk_list.GetString(i)
                for i in range(self.allowed_junk_list.GetCount())
            }
            self.junk_rules.remove_senders(
                [a for a in self._blocked_at_open if a not in blocked_now],
                [a for a in self._allowed_at_open if a not in allowed_now],
            )
        return self.settings
