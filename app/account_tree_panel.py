"""
The accounts-and-folders tree, the left pane of the Mail tab.

Split out of main_frame.py (audit item 11, stage 2). The frame keeps
the command handlers; a pane owns its own controls and knows nothing
about fetching, refreshing or the worker threads -- it is handed
callbacks and calls them.
"""

import wx

import lang
from accessible import apply_content_background
from envelope_format import _folder_display_to_himalaya, _folder_tree_label
from settings_manager import FOLDER_TYPES


class AccountTreePanel(wx.Panel):
    def __init__(self, parent, context_menu_handler):
        super().__init__(parent)
        apply_content_background(self)
        self._context_menu_handler = context_menu_handler

        label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "tree_heading", default="Accounts and Folders",
            ),
        )
        label.SetFont(label.GetFont().Bold())

        self.tree = wx.TreeCtrl(
            self,
            style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT | wx.TR_SINGLE,
        )
        self.tree.SetName(
            lang.t(
                "dialogs", "tree_name", default="Account and folder tree",
            )
        )

        self.root = self.tree.AddRoot("Root")
        self.unified_root_item = None
        self.first_unified_child_item = None
        # The item refresh_accounts picked as its default, kept so
        # ensure_selection can fall back to it after a rebuild whose
        # attempt to restore the previous selection failed.
        self.first_selectable_item = None

        # Fires on right-click AND on the keyboard Application/Menu key.
        self.tree.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(label, 0, wx.ALL, 6)
        sizer.Add(self.tree, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(sizer)

    def refresh_accounts(self, accounts, unified_folder_types=None, mode="all", folders_by_account=None):
        """
        Rebuilds the whole tree in one of Thunderbird's two folder
        pane modes (View > Folders in the menu bar), matching how
        Thunderbird itself switches between them rather than showing
        both at once:

        "all" (the default): every account with its own real folders
        underneath -- no Unified Folders node at all.

        "unified": one node per selected folder type (Inbox, Sent,
        etc, in that order), each expandable into one child per
        account for that account's folder of that type, and the type
        node itself selectable for the combined view across every
        account -- matching Thunderbird's "grouped by folder type,
        inboxes from all accounts first" unified layout.

        folders_by_account (audit findings 42 and 43): optional dict
        of account_id -> ordered
        [(display_label, real_name, unread_count), ...] triples (see
        envelope_format._ordered_folder_list), used in "all" mode in
        place of the fixed six-folder list once a real 'mailbox list
        --all --counts' fetch has completed for that account. An
        account missing from the dict (the common case right after
        launch, before that background fetch resolves) falls back to
        the original fixed six, with unread_count always None.
        """
        unified_folder_types = unified_folder_types or []
        folders_by_account = folders_by_account or {}
        self.tree.DeleteChildren(self.root)
        self.first_unified_child_item = None
        first_selectable = None

        if mode == "unified":
            self.unified_root_item = self.tree.AppendItem(self.root, lang.t('dialogs', 'tree_unified_root', default="Unified Folders"))
            for folder_type in unified_folder_types:
                type_item = self.tree.AppendItem(
                    self.unified_root_item,
                    lang.t(
                        "dialogs", "folder_type_" + folder_type.lower().replace(" ", "_"),
                        default=folder_type,
                    ),
                )
                self.tree.SetItemData(type_item, {"unified": folder_type})
                if self.first_unified_child_item is None:
                    self.first_unified_child_item = type_item
                for account in accounts:
                    label = account.display_name or account.identity_email
                    if not getattr(account, "enabled", True):
                        label = "%s (disabled)" % label
                    unread_count = None
                    pairs = folders_by_account.get(account.account_id)
                    if pairs:
                        for pair_label, _real_name, pair_unread in pairs:
                            if pair_label == folder_type:
                                unread_count = pair_unread
                                break
                    account_child = self.tree.AppendItem(
                        type_item, _folder_tree_label(label, unread_count)
                    )
                    self.tree.SetItemData(
                        account_child,
                        {
                            "account_id": account.account_id,
                            "folder": _folder_display_to_himalaya(folder_type, account),
                            "folder_label": folder_type,
                            "is_special_folder": folder_type in FOLDER_TYPES,
                        },
                    )
            # Only the "Unified Folders" root itself opens by
            # default -- each type node (Inbox, Sent, ...) stays
            # collapsed until the person expands it, so launching
            # ZBox doesn't dump every account's folder under every
            # type into the tree at once. Rebuilding the tree (this
            # method deletes and re-adds every node on each call)
            # already resets manual expand/collapse state regardless,
            # same as the "all" branch below always re-expanding each
            # account -- this just picks a different default for the
            # unified type nodes specifically, per user request.
            self.tree.Expand(self.unified_root_item)
            first_selectable = self.first_unified_child_item or self.unified_root_item
        else:
            self.unified_root_item = None
            first_account_inbox = None
            # Fallback when no folder is labelled Inbox (a real folder
            # list that names it differently). Without it nothing was
            # selected at all, and with no selection every hotkey that
            # reads the tree target and every post-delete refresh
            # silently did nothing until restart.
            first_folder_item = None
            for account in accounts:
                label = account.display_name or account.identity_email
                if not getattr(account, "enabled", True):
                    # Said in the label rather than shown as a colour
                    # or an icon, so a screen reader announces it as
                    # part of the row.
                    label = "%s (disabled)" % label
                item = self.tree.AppendItem(self.root, label)
                self.tree.SetItemData(
                    item,
                    {"account_id": account.account_id, "folder": "INBOX", "is_account_root": True},
                )
                pairs = folders_by_account.get(account.account_id)
                if not pairs:
                    pairs = [
                        (folder_name, _folder_display_to_himalaya(folder_name, account), None)
                        for folder_name in ("Inbox", "Sent", "Drafts", "Trash", "Junk", "Archive")
                    ]
                for folder_label, real_folder, unread_count in pairs:
                    folder_item = self.tree.AppendItem(
                        item, _folder_tree_label(folder_label, unread_count)
                    )
                    self.tree.SetItemData(
                        folder_item,
                        {
                            "account_id": account.account_id,
                            "folder": real_folder,
                            "folder_label": folder_label,
                            "is_special_folder": folder_label in FOLDER_TYPES,
                        },
                    )
                    if folder_label == "Inbox" and first_account_inbox is None:
                        first_account_inbox = folder_item
                    if first_folder_item is None:
                        first_folder_item = folder_item
                self.tree.Expand(item)
            first_selectable = first_account_inbox or first_folder_item

        self.first_selectable_item = first_selectable
        if first_selectable is not None:
            self.tree.SelectItem(first_selectable)

    def select_unified_folders(self):
        target = self.first_unified_child_item or self.unified_root_item
        if target is not None:
            self.tree.SelectItem(target)

    def select_by_target(self, target):
        """
        Selects the tree item whose data matches target (the same
        shape selected_fetch_target returns), if one still exists.
        Used to restore the current selection across a rebuild
        triggered by a background event (a real folder list arriving
        for some account -- audit finding 42) instead of the rebuild
        silently snapping the person back to the first Inbox while
        they're mid-read elsewhere. Returns True if a match was
        selected.
        """
        if not target:
            return False
        want_unified = target.get("unified")

        def matches(data):
            if not data:
                return False
            if want_unified is not None:
                return data.get("unified") == want_unified
            return (
                data.get("account_id") == target.get("account_id")
                and data.get("folder") == target.get("folder")
                and not data.get("is_account_root")
            )

        def walk(item):
            child, cookie = self.tree.GetFirstChild(item)
            while child.IsOk():
                if matches(self.tree.GetItemData(child)):
                    self.tree.SelectItem(child)
                    return True
                if walk(child):
                    return True
                child, cookie = self.tree.GetNextChild(item, cookie)
            return False

        return walk(self.root)

    def ensure_selection(self):
        """
        Selects the default item when the tree has no usable
        selection. Returns True if it selected something.

        A rebuild deletes every node, the selection with them. When
        select_by_target then finds no match (the folder was renamed,
        or its real name changed once the server's folder list
        arrived) nothing is selected, and selected_fetch_target()
        stays None. _auto_refresh_current_folder returns early on
        that, so every later refresh -- including the one after a
        delete -- did nothing, and so did every hotkey that needs a
        target, until ZBox was restarted.
        """
        if self._selected_item_data():
            return False
        if self.first_selectable_item is None:
            return False
        self.tree.SelectItem(self.first_selectable_item)
        return True

    def selected_account_id(self):
        target = self._selected_item_data()
        if target is None:
            return None
        return target.get("account_id")

    def selected_fetch_target(self):
        """
        Returns a dict describing what to fetch for the current
        selection: either {"account_id", "folder"} for a single
        account's folder, or {"unified": folder_type} for a Unified
        Folders entry. None if nothing fetchable is selected.
        """
        return self._selected_item_data()

    def _selected_item_data(self):
        item = self.tree.GetSelection()
        if not item.IsOk():
            return None
        data = self.tree.GetItemData(item)
        if data:
            return data
        parent = self.tree.GetItemParent(item)
        if parent.IsOk():
            return self.tree.GetItemData(parent)
        return None

    def _on_context_menu(self, event):
        self._context_menu_handler(self.tree, event)


def target_to_saved_fields(target):
    """Splits a selected_fetch_target()-shaped dict into the three
    flat values Settings persists for the last-selected-folder
    feature (audit finding 52) -- a plain dict doesn't round-trip
    through JSON with "which of the two target shapes this is" kept
    unambiguous once one side is entirely absent, so Settings gets
    three separate fields instead. See saved_fields_to_target for the
    inverse. Returns (account_id, folder, unified_type), each None
    where not applicable; all three None if target itself is falsy.
    """
    if not target:
        return None, None, None
    if "unified" in target:
        return None, None, target.get("unified")
    return target.get("account_id"), target.get("folder"), None


def saved_fields_to_target(account_id, folder, unified_type):
    """Inverse of target_to_saved_fields -- rebuilds a
    selected_fetch_target()-shaped dict from Settings' three flat
    last_selected_* fields, for select_by_target to restore on
    launch. None if nothing usable was saved (a fresh settings.json,
    or only half of an account_id/folder pair present)."""
    if unified_type:
        return {"unified": unified_type}
    if account_id and folder:
        return {"account_id": account_id, "folder": folder}
    return None
