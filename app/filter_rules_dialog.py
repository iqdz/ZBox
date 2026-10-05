"""
Tools > Message Filters (audit finding 45). Edits one account's
ordered filter_rules.FilterRule list -- New/Edit/Delete/Move Up/Move
Down over a wx.ListBox, the same shape as PrivacyDialog's allowed-
senders list -- plus a Run Now button that saves the list and
immediately applies it to every message already in a chosen folder,
matching Thunderbird's own Filter Rules dialog (its own "Run Now"
lets you pick a folder and run the current rule set against it on
the spot -- this is finding 45's explicitly named second half, "a
manual run filters on this folder").

Scoped to one account per open, the same as AccountSettingsDialog:
whichever account is selected in the tree when Tools > Message
Filters is chosen. Switching accounts means closing this dialog and
reopening it for the other one, not a second account picker inside
it.
"""

import wx

import lang
import notice_toast

from accessible import fit_dialog, wrap_text
from filter_rules import FIELDS, FilterCondition, FilterRule

FIELD_LABELS = {"from": "From", "to": "To", "subject": "Subject"}
NO_MOVE_LABEL = "(do not move)"
CONDITION_ROWS = 3


def _rule_summary(rule):
    """
    One-line description for the rules list -- name, enabled state,
    and a plain-language gist of what the rule does -- so arrowing
    through the list alone tells a screen reader user what Edit would
    otherwise make them open a second dialog to see.
    """
    status = "enabled" if rule.enabled else "disabled"
    active_conditions = [c for c in rule.conditions if (c.value or "").strip()]
    joiner = " and " if rule.match_all else " or "
    condition_text = joiner.join(
        '%s contains "%s"' % (FIELD_LABELS.get(c.field, c.field), c.value.strip())
        for c in active_conditions
    ) or "no conditions set"
    actions = []
    if rule.mark_read:
        actions.append("mark as read")
    if rule.flag:
        actions.append("flag")
    if rule.move_to:
        actions.append('move to "%s"' % rule.move_to)
    action_text = ", ".join(actions) or "no actions set"
    name = rule.name or "(unnamed rule)"
    return "%s (%s): if %s, then %s" % (name, status, condition_text, action_text)


class FilterRuleEditDialog(wx.Dialog):
    def __init__(self, parent, rule, folder_choices):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_filter_rule", default="Filter Rule"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.folder_choices = list(folder_choices)

        sizer = wx.BoxSizer(wx.VERTICAL)

        name_label = wx.StaticText(
            self, label=lang.t("dialogs", "filt_rule_name", default="Rule name")
        )
        self.name_field = wx.TextCtrl(self, value=rule.name)
        self.name_field.SetName(
            lang.t("dialogs", "filt_rule_name", default="Rule name")
        )

        name_row = wx.BoxSizer(wx.HORIZONTAL)
        name_row.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        name_row.Add(self.name_field, 1)

        self.enabled_checkbox = wx.CheckBox(
            self, label=lang.control_label("filt_enabled", "&Enabled")
        )
        self.enabled_checkbox.SetValue(rule.enabled)

        match_label = wx.StaticText(
            self, label=lang.t("dialogs", "filt_match_label", default="Match:")
        )
        match_label.SetFont(match_label.GetFont().Bold())
        self.match_all_radio = wx.RadioButton(
            self,
            label=lang.control_label(
                "filt_match_all", "Match &all of the following (AND)",
            ),
            style=wx.RB_GROUP,
        )
        self.match_any_radio = wx.RadioButton(
            self,
            label=lang.control_label(
                "filt_match_any", "Match an&y of the following (OR)",
            ),
        )
        (self.match_all_radio if rule.match_all else self.match_any_radio).SetValue(True)

        conditions_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "filt_conditions_label",
                default="Conditions (leave a value blank to skip it):",
            ),
        )

        existing = list(rule.conditions)
        self.field_choices = []
        self.value_fields = []
        conditions_grid = wx.FlexGridSizer(CONDITION_ROWS, 2, 6, 8)
        conditions_grid.AddGrowableCol(1)
        field_display = [FIELD_LABELS[f] for f in FIELDS]
        for index in range(CONDITION_ROWS):
            condition = existing[index] if index < len(existing) else FilterCondition()
            field_choice = wx.Choice(self, choices=field_display)
            field_choice.SetName(
                lang.t(
                    "dialogs", "filt_condition_field",
                    default="Condition %d field" % (index + 1),
                    number=index + 1,
                )
            )
            field_choice.SetSelection(
                FIELDS.index(condition.field) if condition.field in FIELDS else 0
            )
            value_field = wx.TextCtrl(self, value=condition.value)
            value_field.SetName(
                lang.t(
                    "dialogs", "filt_condition_contains",
                    default="Condition %d contains" % (index + 1),
                    number=index + 1,
                )
            )
            self.field_choices.append(field_choice)
            self.value_fields.append(value_field)
            conditions_grid.Add(field_choice, 0)
            conditions_grid.Add(value_field, 1, wx.EXPAND)

        actions_label = wx.StaticText(
            self, label=lang.t("dialogs", "filt_actions_label", default="Actions:")
        )
        actions_label.SetFont(actions_label.GetFont().Bold())
        self.mark_read_checkbox = wx.CheckBox(
            self, label=lang.control_label("filt_mark_read", "Mark as &read")
        )
        self.mark_read_checkbox.SetValue(rule.mark_read)
        self.flag_checkbox = wx.CheckBox(
            self, label=lang.control_label("filt_flag", "&Flag the message")
        )
        self.flag_checkbox.SetValue(rule.flag)

        move_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "filt_move_label", default="Move to folder:",
            ),
        )
        self.move_choice = wx.Choice(self, choices=[NO_MOVE_LABEL] + self.folder_choices)
        self.move_choice.SetName(
            lang.t("dialogs", "filt_move_name", default="Move to folder")
        )
        if rule.move_to and rule.move_to in self.folder_choices:
            self.move_choice.SetStringSelection(rule.move_to)
        else:
            self.move_choice.SetSelection(0)

        move_row = wx.BoxSizer(wx.HORIZONTAL)
        move_row.Add(move_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        move_row.Add(self.move_choice, 1)

        sizer.Add(name_row, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.enabled_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(match_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.match_all_radio, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.match_any_radio, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(conditions_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(conditions_grid, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(actions_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.mark_read_checkbox, 0, wx.LEFT | wx.RIGHT, 16)
        sizer.Add(self.flag_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        sizer.Add(move_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, event):
        rule = self.result_rule()
        if not rule.name.strip():
            notice_toast.notify(
                self,
                lang.t(
                    "dialogs", "filter_rule_needs_name",
                    default="Give this rule a name.",
                ),
                lang.t("dialogs", "title_filter_rule", default="Filter Rule"),
            )
            self.name_field.SetFocus()
            return
        if not rule.has_condition():
            notice_toast.notify(
                self,
                lang.t(
                    "dialogs", "filter_rule_needs_condition",
                    default="Set at least one condition (From, To or Subject).",
                ),
                lang.t("dialogs", "title_filter_rule", default="Filter Rule"),
            )
            return
        if not rule.has_action():
            notice_toast.notify(
                self,
                lang.t(
                    "dialogs", "filter_rule_needs_action",
                    default="Choose at least one action: mark as read, flag, or move to a folder.",
                ),
                lang.t("dialogs", "title_filter_rule", default="Filter Rule"),
            )
            return
        event.Skip()

    def result_rule(self):
        conditions = [
            FilterCondition(field=FIELDS[choice.GetSelection()], value=value.GetValue())
            for choice, value in zip(self.field_choices, self.value_fields)
            if value.GetValue().strip()
        ]
        move_selection = self.move_choice.GetStringSelection()
        return FilterRule(
            name=self.name_field.GetValue().strip(),
            enabled=self.enabled_checkbox.GetValue(),
            match_all=self.match_all_radio.GetValue(),
            conditions=conditions,
            mark_read=self.mark_read_checkbox.GetValue(),
            flag=self.flag_checkbox.GetValue(),
            move_to="" if move_selection in ("", NO_MOVE_LABEL) else move_selection,
        )


class FilterRulesDialog(wx.Dialog):
    """
    rules: the account's current FilterRule list, deep-copied on
    construction so edits here never touch the caller's list until
    it explicitly reads .rules back. Returns wx.ID_OK ("save") or
    RUN_NOW ("save, then run .run_folder now") from ShowModal();
    Cancel/close means discard every edit.
    """

    # A plain literal, not wx.ID_HIGHEST + 1: wx's own stock ids
    # (ID_OK, ID_CANCEL, ...) all sit under 5999, so anything
    # comfortably above that is safe from colliding with them.
    RUN_NOW = 6000

    def __init__(self, parent, account_label, rules, folder_choices):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "title_message_filters",
                default="Message Filters: %s" % account_label,
                account=account_label,
            ),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.rules = [FilterRule.from_dict(rule.to_dict()) for rule in rules]
        self.folder_choices = list(folder_choices)
        self.run_folder = None

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "filt_note_intro",
                default=(
                    "Rules run in order, top to bottom, testing From, To and "
                    "Subject. Applied automatically to new mail as it arrives "
                    "in this account's Inbox; use Run Now below to apply them "
                    "to messages already sitting in a folder."
                ),
            ),
        )
        wrap_text(intro)
        sizer.Add(intro, 0, wx.ALL, 10)

        self.rules_list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.rules_list.SetName(
            lang.t("dialogs", "filt_rules_name", default="Filter rules")
        )
        row_height = self.rules_list.GetCharHeight() or 16
        self.rules_list.SetMinSize((-1, row_height * 6))
        self.rules_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_edit)
        self.rules_list.Bind(wx.EVT_LISTBOX, lambda event: self._sync_buttons())

        sizer.Add(self.rules_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self.new_button = wx.Button(
            self, label=lang.control_label("filt_new", "&New Rule...")
        )
        self.edit_button = wx.Button(
            self, label=lang.control_label("filt_edit", "&Edit Rule...")
        )
        self.delete_button = wx.Button(
            self, label=lang.control_label("filt_delete", "&Delete Rule")
        )
        self.up_button = wx.Button(
            self, label=lang.control_label("filt_up", "Move &Up")
        )
        self.down_button = wx.Button(
            self, label=lang.control_label("filt_down", "Move Do&wn")
        )
        for button, handler in (
            (self.new_button, self._on_new),
            (self.edit_button, self._on_edit),
            (self.delete_button, self._on_delete),
            (self.up_button, self._on_move_up),
            (self.down_button, self._on_move_down),
        ):
            button.Bind(wx.EVT_BUTTON, handler)
            button_row.Add(button, 0, wx.RIGHT, 8)
        sizer.Add(button_row, 0, wx.ALL, 10)

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        run_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "filt_run_label", default="Run Now on folder:",
            ),
        )
        self.run_folder_choice = wx.Choice(self, choices=self.folder_choices)
        self.run_folder_choice.SetName(
            lang.t("dialogs", "filt_run_name", default="Run Now folder")
        )
        if self.folder_choices:
            self.run_folder_choice.SetSelection(0)
        self.run_now_button = wx.Button(
            self, label=lang.control_label("filt_run_now", "&Run Now")
        )
        self.run_now_button.Bind(wx.EVT_BUTTON, self._on_run_now)
        self.run_now_button.Enable(bool(self.folder_choices))

        run_row = wx.BoxSizer(wx.HORIZONTAL)
        run_row.Add(run_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        run_row.Add(self.run_folder_choice, 1, wx.RIGHT, 8)
        run_row.Add(self.run_now_button, 0)

        run_note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "filt_note_run_now",
                default=(
                    "Applies every enabled rule above, in order, to every "
                    "message already in the chosen folder -- saves your "
                    "changes first, the same as OK."
                ),
            ),
        )
        wrap_text(run_note)

        sizer.Add(run_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(run_note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        button_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        fit_dialog(self, sizer)
        self._refresh_list()

    def _refresh_list(self):
        previous = self.rules_list.GetSelection()
        self.rules_list.Set([_rule_summary(rule) for rule in self.rules])
        if self.rules:
            target = previous if previous != wx.NOT_FOUND else 0
            self.rules_list.SetSelection(min(target, len(self.rules) - 1))
        self._sync_buttons()

    def _sync_buttons(self):
        index = self.rules_list.GetSelection()
        has_selection = index != wx.NOT_FOUND
        self.edit_button.Enable(has_selection)
        self.delete_button.Enable(has_selection)
        self.up_button.Enable(has_selection and index > 0)
        self.down_button.Enable(has_selection and index < len(self.rules) - 1)

    def _on_new(self, event):
        dialog = FilterRuleEditDialog(self, FilterRule(), self.folder_choices)
        if dialog.ShowModal() == wx.ID_OK:
            self.rules.append(dialog.result_rule())
            self._refresh_list()
            self.rules_list.SetSelection(len(self.rules) - 1)
            self._sync_buttons()
        dialog.Destroy()
        self.rules_list.SetFocus()

    def _on_edit(self, event):
        index = self.rules_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        dialog = FilterRuleEditDialog(self, self.rules[index], self.folder_choices)
        if dialog.ShowModal() == wx.ID_OK:
            self.rules[index] = dialog.result_rule()
            self._refresh_list()
            self.rules_list.SetSelection(index)
        dialog.Destroy()
        self.rules_list.SetFocus()

    def _on_delete(self, event):
        index = self.rules_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        del self.rules[index]
        self._refresh_list()
        self.rules_list.SetFocus()

    def _on_move_up(self, event):
        index = self.rules_list.GetSelection()
        if index == wx.NOT_FOUND or index == 0:
            return
        self.rules[index - 1], self.rules[index] = self.rules[index], self.rules[index - 1]
        self._refresh_list()
        self.rules_list.SetSelection(index - 1)
        self._sync_buttons()
        self.rules_list.SetFocus()

    def _on_move_down(self, event):
        index = self.rules_list.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self.rules) - 1:
            return
        self.rules[index + 1], self.rules[index] = self.rules[index], self.rules[index + 1]
        self._refresh_list()
        self.rules_list.SetSelection(index + 1)
        self._sync_buttons()
        self.rules_list.SetFocus()

    def _on_run_now(self, event):
        selection = self.run_folder_choice.GetStringSelection()
        if not selection:
            return
        self.run_folder = selection
        self.EndModal(self.RUN_NOW)
