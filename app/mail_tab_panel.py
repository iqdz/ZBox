"""
The Mail tab itself: the three panes and the splitters between them.

Split out of main_frame.py (audit item 11, stage 2). Purely
composition -- it builds the panes, wires their callbacks through to
the frame, and owns nothing else.
"""

import wx

from accessible import apply_content_background
from account_tree_panel import AccountTreePanel
from envelope_list_panel import EnvelopeListPanel
from reader_panel import ReaderPanel
import lang


class MailTabPanel(wx.Panel):
    """
    The main three-pane mail view: account tree, message list, reader.
    Lives as one tab inside the notebook. Kept as its own class so
    other tab types (a message-in-its-own-tab, search results,
    calendar, tasks) can be added later as sibling classes.
    """

    def __init__(self, parent, tree_context_handler, list_context_handler, envelope_selection_handler, tree_selection_handler, envelope_activation_handler, list_key_handler=None, selection_announce_handler=None, list_bare_key_shortcuts_enabled=lambda: True, list_thread_flags_provider=None, outer_sash_position=220, inner_sash_position=420, list_hidden_keys_provider=None):
        super().__init__(parent)
        apply_content_background(self)

        outer_splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        inner_splitter = wx.SplitterWindow(outer_splitter, style=wx.SP_LIVE_UPDATE)
        # Exposed so main_frame can bind EVT_SPLITTER_SASH_POS_CHANGED
        # and persist a dragged sash position (audit finding 52) --
        # previously these were local names only, with the sash
        # positions hardcoded below on every launch.
        self.outer_splitter = outer_splitter
        self.inner_splitter = inner_splitter

        self.account_panel = AccountTreePanel(outer_splitter, tree_context_handler)
        # The tree is created after inner_splitter, so without this the
        # Tab order ran list, reader, tree, and Shift+Tab from the list
        # left the Mail tab for the notebook's tab strip before wrapping
        # round to the folders. Siblings under outer_splitter, so
        # MoveBeforeInTabOrder applies here.
        self.account_panel.MoveBeforeInTabOrder(inner_splitter)
        # enable_threading=True only here -- this is the real
        # per-folder Mail tab list. search_panel.py and
        # related_messages_panel.py construct their own
        # EnvelopeListPanel instances directly, with threading left
        # at its default False, since their results span multiple
        # folders/accounts and were never a single folder's thread
        # structure to group in the first place.
        self.envelope_panel = EnvelopeListPanel(inner_splitter, list_context_handler, envelope_selection_handler, envelope_activation_handler, key_handler=list_key_handler, selection_announce_handler=selection_announce_handler, bare_key_shortcuts_enabled=list_bare_key_shortcuts_enabled, enable_threading=True, thread_flags_provider=list_thread_flags_provider, initial_placeholder=lang.t('dialogs', 'envlist_loading', default="Loading messages..."), hidden_keys_provider=list_hidden_keys_provider)
        self.reader_panel = ReaderPanel(inner_splitter)

        self.account_panel.tree.Bind(wx.EVT_TREE_SEL_CHANGED, tree_selection_handler)

        inner_splitter.SplitVertically(self.envelope_panel, self.reader_panel)
        inner_splitter.SetSashPosition(inner_sash_position)
        inner_splitter.SetMinimumPaneSize(200)

        outer_splitter.SplitVertically(self.account_panel, inner_splitter)
        outer_splitter.SetSashPosition(outer_sash_position)
        outer_splitter.SetMinimumPaneSize(150)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(outer_splitter, 1, wx.EXPAND)
        self.SetSizer(sizer)

        # Ctrl+Y, and Tab wrapping inside the three panes. Caught here
        # on the Mail tab, not frame-wide, so compose and every other
        # tab keep their own keys.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def focus_folder_tree(self):
        """Ctrl+Y and View > Folders > Go to Folder List: focus on the
        folder tree, on the current folder, scrolled into view."""
        tree = self.account_panel.tree
        item = tree.GetSelection()
        if item.IsOk():
            tree.EnsureVisible(item)
        tree.SetFocus()

    def _on_char_hook(self, event):
        key = event.GetKeyCode()
        control, alt, shift = event.ControlDown(), event.AltDown(), event.ShiftDown()
        if key == ord("Y") and control and not alt and not shift:
            self.focus_folder_tree()
            return
        if key == wx.WXK_TAB and not control and not alt:
            # Tab from the last pane goes to the first and Shift+Tab
            # from the first to the last, so Tab stays inside the three
            # panes and never stops on the tab strip. Ctrl+Tab still
            # switches tabs (the frame's accelerator). Panes that are
            # hidden, such as the reader after Ctrl+B, are left out.
            shown = [target for target in self.focus_targets() if target.IsShownOnScreen()]
            focused = wx.Window.FindFocus()
            if len(shown) > 1 and focused is not None:
                if shift and focused == shown[0]:
                    shown[-1].SetFocus()
                    return
                if not shift and focused == shown[-1]:
                    shown[0].SetFocus()
                    return
        event.Skip()

    def focus_targets(self):
        return [self.account_panel.tree, self.envelope_panel.list_ctrl, self.reader_panel.reader]

    def focus_default(self):
        """Back to the message list, selection untouched -- what a
        compose tab closing after Send, Reply or Forward lands on, the
        same place main_frame._close_tab_at puts a closed message."""
        self.envelope_panel.list_ctrl.SetFocus()
