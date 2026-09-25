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

    def focus_targets(self):
        return [self.account_panel.tree, self.envelope_panel.list_ctrl, self.reader_panel.reader]

    def focus_default(self):
        """Back to the message list, selection untouched -- what a
        compose tab closing after Send, Reply or Forward lands on, the
        same place main_frame._close_tab_at puts a closed message."""
        self.envelope_panel.list_ctrl.SetFocus()
