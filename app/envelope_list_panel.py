"""
The message list, the middle pane of the Mail tab.

Split out of main_frame.py (audit item 11, stage 2). This is where
multi-select, the spoken selection count, the sort order and the
silent auto-refresh repaint live -- all of it presentation, none of
it fetching. The frame hands it envelopes and it decides what the
rows say.

Two behaviours here exist for screen readers specifically and should
not be "simplified" away: row status is words rather than colour or
weight, because a screen reader cannot hear either; and populate
applies the sort itself, so a background refresh cannot silently
rearrange the list under someone who is arrowing through it.
"""

import logging

import wx

import lang
from accessible import apply_content_background
from envelope_format import (
    _SORT_KEY_FUNCS,
    _friendly_envelope_date,
    _envelope_from,
    _envelope_is_flagged,
    _envelope_is_unread,
    _envelope_status_text,
    _envelope_subject,
    _envelopes_signature,
    _set_envelope_flag,
    _parse_envelope_date,
)
import thread_grouping

logger = logging.getLogger("zbox.envlist")


def _newest_unread(envelopes):
    """
    The message Enter on a collapsed thread should land on: the most
    recent UNREAD one, or the most recent message when the whole
    thread has been read. None when there is nothing to choose from.

    Recency is by parsed date, not list position. A thread's own
    order is depth-first through the reply tree (see
    thread_grouping.all_envelopes), so the last entry is the deepest
    reply, which is not necessarily the newest message -- a late
    reply to the thread's first message sorts before an older reply
    to a later one.
    """
    if not envelopes:
        return None
    unread = [e for e in envelopes if _envelope_is_unread(e)]
    return max(unread or list(envelopes), key=_parse_envelope_date)

# Above how many messages Enter/Space warns before opening a
# collapsed thread (Right-arrow's lighter "reveal the sub-list"
# never warns, whatever the size -- only the fuller "open" gesture
# does).


class _RowThreadInfo:
    """What a single visible row represents when threading is on --
    None (see populate/_render) for an ordinary, non-threaded row,
    which renders exactly as it always has.

    thread_key identifies the whole thread (thread_grouping.thread_key
    of its root) regardless of which row currently represents it;
    is_root marks the thread's own first message; is_collapsed is
    only ever True on the single row standing in for a whole
    collapsed thread; depth is 0 for the root and increases with
    each level of nesting once expanded; message_count is the
    thread's total size, the same value on every row belonging to
    it, used for the >5 warning and the "Collapsed (N)" label."""

    __slots__ = (
        "thread_key", "is_root", "is_collapsed", "depth", "message_count",
        "aggregate_unread", "aggregate_flagged",
    )

    def __init__(
        self, thread_key, is_root, is_collapsed, depth, message_count,
        aggregate_unread=False, aggregate_flagged=False,
    ):
        self.thread_key = thread_key
        self.is_root = is_root
        self.is_collapsed = is_collapsed
        self.depth = depth
        self.message_count = message_count
        # Only meaningful when is_collapsed is True -- whether ANY
        # message in the thread (not just the visible root, which is
        # usually the oldest and so usually already read) is unread
        # or flagged, computed once here from the whole subtree while
        # _build_threaded_rows still has it, rather than the caller
        # trying to re-derive it later from just a thread_key.
        self.aggregate_unread = aggregate_unread
        self.aggregate_flagged = aggregate_flagged


def _thread_expanded(panel, thread_key):
    """
    Whether a thread shows expanded under the panel's current
    Expand All / Collapse All state. A module function rather than
    only a method because the tests' stand-in panels borrow
    _build_threaded_rows alone, without __init__ or other methods;
    the attributes are read through getattr for the same reason.
    """
    if getattr(panel, "_threads_expanded_default", False):
        return thread_key not in getattr(panel, "_collapsed_thread_keys", set())
    return thread_key in panel._expanded_thread_keys


def hide_key_for_header(header):
    """The hide key for a Message-ID header, or None. A deleted row
    is hidden by this as well as by its id, because a row from the
    offline copy and its live twin share the header but not the id."""
    text = str(header or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return "hdr:" + text.casefold() if text else None


class EnvelopeListPanel(wx.Panel):
    def __init__(self, parent, context_menu_handler, selection_handler, activation_handler, key_handler=None, selection_announce_handler=None, bare_key_shortcuts_enabled=lambda: True, enable_threading=False, thread_flags_provider=None, initial_placeholder=None, hidden_keys_provider=None):
        super().__init__(parent)
        apply_content_background(self)
        self._context_menu_handler = context_menu_handler
        self._selection_handler = selection_handler
        self._activation_handler = activation_handler
        self._key_handler = key_handler
        # Zero-arg callable read live on every keypress (audit
        # finding 19), rather than a value snapshotted at
        # construction, so flipping the Settings checkbox takes
        # effect immediately without rebuilding this panel.
        self._bare_key_shortcuts_enabled = bare_key_shortcuts_enabled
        # Called with a plain-text message (e.g. "3 messages selected.")
        # whenever a multi-select sweep (Shift+Up/Down, Ctrl+A) settles,
        # so a screen reader user gets a spoken count instead of silently
        # arrowing past rows with no indication of how many are selected.
        self._selection_announce_handler = selection_announce_handler
        self._announce_timer = None
        self._row_envelopes = []  # index-aligned with list_ctrl rows
        self._signature = ()  # snapshot for silent auto-refresh change detection
        # Sort order lives here, not only in the menu handler, because
        # populate is what has to honour it. It used to be applied
        # only by sort_by, so every silent auto-refresh handed
        # populate the server-ordered list and the reader's chosen
        # order vanished twenty seconds after they picked it.
        self._sort_key = "date"
        self._sort_ascending = False
        # Thread grouping (opt-in -- only the main Mail tab's own
        # instance turns this on; search results and Related Messages
        # keep constructing this panel with the default False and are
        # completely unaffected, since every branch below is skipped
        # entirely when this is False).
        self._enable_threading = bool(enable_threading)
        # The real, full, ungrouped envelope list from the most
        # recent populate() -- what _render() re-derives display rows
        # from on every redraw, INCLUDING a plain expand/collapse
        # toggle that fetched nothing new. self._row_envelopes (above)
        # is the very different "currently visible rows only" list a
        # collapsed thread's hidden replies are excluded from; sort_by
        # and append used to read _row_envelopes as if it were the
        # full set, which would have silently and permanently dropped
        # every reply hidden inside a collapsed thread the moment
        # either ran -- both now read this instead.
        self._all_envelopes = []
        # thread_key set of every thread the user has expanded.
        # Deliberately never cleared on its own: a thread_key is a
        # real Message-ID, so an entry left over after switching
        # folders simply matches nothing there and does nothing --
        # cheaper and safer than trying to detect "this populate() is
        # a different folder than last time" from in here, which this
        # panel has no reliable way to know anyway.
        self._expanded_thread_keys = set()
        # View > Threads > Expand All / Collapse All is a lasting
        # state, not a one-off command. When True every multi-message
        # thread shows expanded, new ones included, except the keys in
        # _collapsed_thread_keys (collapsed by hand with Left or
        # Escape); when False only _expanded_thread_keys show
        # expanded. main_frame seeds it from settings and hears about
        # the "*"/"\" keys through _threads_mode_handler, so the
        # menu check and the saved setting follow the key too.
        self._threads_expanded_default = False
        self._collapsed_thread_keys = set()
        self._threads_mode_handler = None
        # Index-aligned with list_ctrl rows / _row_envelopes: a
        # _RowThreadInfo for a threaded row, None for an ordinary one.
        self._row_thread_info = []
        # thread_key -> every envelope in that thread (root.all_envelopes()),
        # rebuilt on every _build_threaded_rows() call. Only
        # get_all_selected_envelopes() reads this -- it's what lets a
        # bulk action on a selected COLLAPSED thread row reach every
        # message folded into it, not just the one visible root
        # envelope _row_envelopes has a row for.
        self._thread_members_by_key = {}
        # View > Threads > All/Watched Threads/Ignored Threads. A
        # display-only lens over the same folder listing -- switching
        # back to "all" always shows every message again, exactly
        # like muting an account never removes it (see thread_state.
        # py's own docstring): this never drops mail, only rows.
        self._thread_filter = "all"
        # Callable, taking one optional account_id, returning
        # (watched_keys, ignored_keys) -- two sets of thread_key --
        # for that account's currently selected folder, or the
        # CURRENTLY selected account/folder when account_id is None.
        # Read live on every render rather than snapshotted, same
        # reasoning as bare_key_shortcuts_enabled above: this panel
        # has no idea which account/folder it's showing (that's
        # main_frame's job, via the currently selected tree target),
        # so main_frame supplies this instead of this panel trying to
        # track account/folder state of its own that would just have
        # to be kept in sync with the tree. The account_id parameter
        # exists for Unified folders specifically: they mix messages
        # from several accounts in one list, each with its own
        # thread_state.json, so _build_threaded_rows passes each
        # root's own _zbox_account_id rather than relying on "the
        # selected account", which a Unified folder doesn't have.
        self._thread_flags_provider = thread_flags_provider or (
            lambda account_id=None: (set(), set())
        )
        # Zero-arg callable returning a set of (account_id or None,
        # str(id)) keys for rows a Delete or Shift+Delete has taken
        # off the list while the server move may not have landed yet.
        # Read live on every populate, so a silent refresh that races
        # the move cannot put those rows back. None on the search
        # results and Related Messages instances, which hide nothing.
        self._hidden_keys_provider = hidden_keys_provider

        label = wx.StaticText(
            self, label=lang.t("dialogs", "envlist_heading", default="Messages")
        )
        label.SetFont(label.GetFont().Bold())

        # LC_SINGLE_SEL removed: multi-select (Shift+Up/Down range
        # extend, Ctrl+A select all -- both native wx.ListCtrl/MSW
        # behavior once multi-select is on) is enabled here. Every
        # existing single-message action (reply, archive, move,
        # delete, etc.) still reads only the first selected row via
        # selected_envelope(), unchanged -- multi-select is currently
        # just a selection capability, not yet wired to bulk actions.
        self.list_ctrl = wx.ListCtrl(
            self, style=wx.LC_REPORT
        )
        self.list_ctrl.SetName(
            lang.t("dialogs", "envlist_name", default="Message list")
        )
        self.list_ctrl.InsertColumn(0, lang.t('dialogs', 'envlist_col_status', default="Status"), width=70)
        self.list_ctrl.InsertColumn(1, lang.t('dialogs', 'envlist_col_from', default="From"), width=160)
        self.list_ctrl.InsertColumn(2, lang.t('dialogs', 'envlist_col_subject', default="Subject"), width=280)
        self.list_ctrl.InsertColumn(3, lang.t('dialogs', 'envlist_col_date', default="Date"), width=140)

        # What the list says before anything has been fetched -- the
        # first thing a screen reader reads at launch, since the row
        # is there and focusable before any mail is. The Mail tab
        # passes "Loading messages..." because that is what is
        # actually about to happen: the frame restores the last
        # folder and fetches it. Search results and Related Messages
        # keep the default, where nothing loads until the reader asks
        # for something.
        if initial_placeholder is None:
            initial_placeholder = lang.t(
                "dialogs", "envlist_select_folder",
                default="Select an account or folder to load messages.",
            )
        self.show_placeholder(initial_placeholder)

        self.list_ctrl.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_item_selected)
        # Both SELECTED and DESELECTED feed the same debounced announcer:
        # extending a Shift+Down selection fires SELECTED for new rows,
        # shrinking it with Shift+Up fires DESELECTED for rows leaving
        # the selection, and Ctrl+A / Select All fires one SELECTED per
        # row. Either way the announcer settles on the final count once
        # the sweep stops (see _on_selection_count_changed).
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selection_count_changed)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_selection_count_changed)
        # Fires on Enter and on double-click, matching Thunderbird's
        # "open in a new tab" gesture from the message list.
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_activated)
        # Delete / Shift+Delete act on the selected message (move to
        # Trash / permanent delete) straight from the list.
        self.list_ctrl.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(label, 0, wx.ALL, 6)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(sizer)

    def show_placeholder(self, message):
        self.list_ctrl.DeleteAllItems()
        self._row_envelopes = []
        self._row_thread_info = []
        self._thread_members_by_key = {}
        self._all_envelopes = []
        self._signature = ()
        self._listing_loaded = False
        self.list_ctrl.InsertItem(0, "")
        self.list_ctrl.SetItem(0, 1, message)
        self.list_ctrl.SetItem(0, 2, "")
        self.list_ctrl.SetItem(0, 3, "")

    def set_sort(self, key, ascending):
        """Records the order without redrawing. Used when seeding the
        panel from saved settings, before any mail has arrived."""
        if key in _SORT_KEY_FUNCS:
            self._sort_key = key
        self._sort_ascending = bool(ascending)

    def _apply_sort(self, envelopes):
        func = _SORT_KEY_FUNCS.get(self._sort_key)
        if func is None:
            return list(envelopes)
        return sorted(envelopes, key=func, reverse=not self._sort_ascending)

    def _without_hidden(self, envelopes):
        """
        `envelopes` minus every row the hidden_keys_provider names.
        Read through getattr because the tests' _ListPanel stand-in
        borrows populate without ever running __init__.
        """
        provider = getattr(self, "_hidden_keys_provider", None)
        if provider is None:
            return list(envelopes)
        hidden = provider()
        if not hidden:
            return list(envelopes)
        return [
            envelope for envelope in envelopes
            if (envelope.get("_zbox_account_id") or None, str(envelope.get("id"))) not in hidden
            and (
                envelope.get("_zbox_account_id") or None,
                hide_key_for_header(envelope.get("message-id")),
            ) not in hidden
        ]

    def populate(self, envelopes):
        # Every path into the list goes through here -- first load,
        # folder change, offline cache preview, silent auto-refresh --
        # so this is the one place the sort (and, when threading is
        # on, the grouping) has to be applied. Stored as the real
        # full set before anything is derived from it; _render()
        # (below) is what actually redraws, and is also what a plain
        # expand/collapse re-invokes on its own, with nothing new
        # having been fetched.
        #
        # The selection is snapshotted and re-applied around that
        # redraw, by (account, id) key rather than row index, the
        # same way populate_if_changed and append already do it and
        # for the same reason. _render() opens with DeleteAllItems,
        # which destroys the selection AND the focused item, and
        # wx.ListCtrl raises EVT_LIST_ITEM_ACTIVATED only when there
        # is a focused item to activate. Enter pressed in the window
        # between those two therefore raised nothing at all -- no
        # activation, no error, and no log line, since every trace on
        # that path lives inside _on_item_activated. Seen once live:
        # a Unified Inbox fetch finished when its slowest account
        # reported, _finish_unified_fetch called this method, and a
        # keypress 151ms later vanished silently.
        selected_rows = []
        row_index = self.list_ctrl.GetFirstSelected()
        while row_index != -1:
            selected_rows.append(row_index)
            row_index = self.list_ctrl.GetNextSelected(row_index)
        previous_keys = [
            (
                self._row_envelopes[row].get("_zbox_account_id"),
                self._row_envelopes[row].get("id"),
            )
            for row in selected_rows
            if row < len(self._row_envelopes)
        ]

        # append() also lands here, so its new page is filtered too.
        self._all_envelopes = self._without_hidden(envelopes)
        self._render()

        if previous_keys:
            matched = [
                row for row, envelope in enumerate(self._row_envelopes)
                if (envelope.get("_zbox_account_id"), envelope.get("id")) in previous_keys
            ]
            if matched:
                self.list_ctrl.Select(matched[0])
                self.list_ctrl.Focus(matched[0])
                for row in matched[1:]:
                    self.list_ctrl.Select(row)
                self.list_ctrl.EnsureVisible(matched[0])

    def _render(self):
        """
        Redraws list_ctrl from self._all_envelopes, self._sort_key/
        self._sort_ascending and (when threading is on) self.
        _expanded_thread_keys -- the one place that turns "what data
        do we have and what's expanded" into actual rows. Called by
        populate() whenever new data arrived, and directly by the
        expand/collapse handlers when only the expanded set changed.
        """
        self.list_ctrl.DeleteAllItems()
        envelopes = self._apply_sort(self._all_envelopes)
        self._signature = _envelopes_signature(self._all_envelopes)

        if self._enable_threading:
            rows = self._build_threaded_rows(envelopes)
        else:
            rows = [(envelope, None) for envelope in envelopes]

        self._row_envelopes = [envelope for envelope, _info in rows]
        self._row_thread_info = [info for _envelope, info in rows]

        if not rows:
            if self._enable_threading and self._thread_filter != "all" and self._all_envelopes:
                # The folder isn't actually empty -- View > Threads is
                # just filtered to a lens with nothing in it right
                # now. Switching back to All (or watching/ignoring a
                # thread) brings rows back immediately; nothing was
                # ever removed from the folder itself.
                label = (
                    lang.t('dialogs', 'envlist_no_watched', default="No watched threads in this folder.")
                    if self._thread_filter == "watched"
                    else lang.t('dialogs', 'envlist_no_ignored', default="No ignored threads in this folder.")
                )
                self.show_placeholder(label)
            else:
                self.show_placeholder(lang.t('dialogs', 'envlist_empty', default="No messages in this folder."))
            # A real listing that happens to be empty: the silent poll
            # must still run, or new mail here never appears until
            # the folder is reselected (see has_listing).
            self._listing_loaded = True
            return

        for row, (envelope, info) in enumerate(rows):
            if info is not None and info.is_collapsed:
                # One row standing in for every message in the
                # thread: the oldest (root) message's own From/
                # Subject/Date, unmodified, plus a "Collapsed (N)"
                # label in the Status column -- the same column
                # "Unread"/"Starred" already live in, so a screen
                # reader hears it as part of the row's normal status
                # rather than a change buried in the subject text.
                # Unread/Starred themselves are aggregated across
                # every message in the thread (see _build_threaded_
                # rows), not just the root's own (usually-read, since
                # it's the oldest) state -- otherwise collapsing a
                # thread with unread or starred replies would
                # misreport it as read/unstarred.
                status = _envelope_status_text(info.aggregate_unread, info.aggregate_flagged)
                label = lang.t('dialogs', 'envlist_collapsed', default="Collapsed (%d)") % info.message_count
                status = f"{status}, {label}" if status else label
                subject = _envelope_subject(envelope)
            else:
                unread = _envelope_is_unread(envelope)
                flagged = _envelope_is_flagged(envelope)
                status = _envelope_status_text(unread, flagged)
                # Junk rules state (see mail_fetch._apply_spam_filter
                # and mark_ids_spam below) -- text
                # only, same rule as every other row state here, and
                # not aggregated onto a collapsed thread's row: a
                # freshly-flagged arrival is essentially always its
                # own single-message thread at the point this runs.
                spam_label = envelope.get("_zbox_spam_label")
                if spam_label:
                    status = f"{status}, {spam_label}" if status else spam_label
                subject = _envelope_subject(envelope)
                if info is not None and info.depth > 0:
                    # A visible reply inside an expanded thread --
                    # indented under its root so the nesting is at
                    # least visible/readable, since list_ctrl (a
                    # plain report-view list, not a tree control) has
                    # no real indentation or expand/collapse glyph of
                    # its own to draw one with.
                    subject = ("    " * info.depth) + subject
            # Text status rather than relying on color/bold alone,
            # since a screen reader won't announce font weight or
            # color changes but will read plain text every time.
            self.list_ctrl.InsertItem(row, status)
            self.list_ctrl.SetItem(row, 1, _envelope_from(envelope))
            self.list_ctrl.SetItem(row, 2, subject)
            self.list_ctrl.SetItem(row, 3, _friendly_envelope_date(envelope))

    def sort_by(self, key, ascending):
        """
        Changes the order and redraws -- no new fetch, just a
        client-side resort of what is already loaded (View > Sort By).
        The order sticks: populate applies it to every later refresh
        too, so the next background poll no longer undoes it.

        Resorts self._all_envelopes, the real full set -- not
        self._row_envelopes, which is only the currently *visible*
        rows and would silently and permanently drop every reply
        hidden inside a collapsed thread if resorted from instead.
        """
        self.set_sort(key, ascending)
        if self._all_envelopes:
            self.populate(self._all_envelopes)

    def _build_threaded_rows(self, envelopes):
        """
        Groups `envelopes` (already sorted) into threads and returns
        the flat (envelope, info_or_None) row plan _render() draws
        from: one row per single-message thread (info=None, drawn
        exactly like an ordinary row), one row per collapsed
        multi-message thread (info.is_collapsed=True), or one row per
        member of an expanded thread (info.depth increasing with
        nesting).

        self._thread_filter (View > Threads > All/Watched/Ignored)
        drops a whole root -- single-message or not -- when it isn't
        on the matching list, before anything else about it is
        decided. A single message can be watched or ignored exactly
        like a longer thread (thread_key works the same way for a
        thread of one), so the filter check has to happen for every
        root, not only ones that would otherwise get an info object.
        """
        roots = thread_grouping.group_into_threads(envelopes)
        # Sorted by date, a thread sits where its NEWEST message
        # would, so a thread that gets a reply moves to the top
        # (descending) or bottom (ascending), as in Thunderbird. The
        # sort is stable, so threads with the same newest date keep
        # their order. Other sort keys keep the root's own position.
        # Read through getattr because the tests' _Panel stand-in
        # borrows this method without running __init__.
        if getattr(self, "_sort_key", None) == "date":
            roots.sort(
                key=lambda root: max(
                    _parse_envelope_date(e) for e in root.all_envelopes()
                ),
                reverse=not getattr(self, "_sort_ascending", False),
            )
        rows = []
        # Rebuilt from scratch every call, same as _row_thread_info
        # below -- a stale entry from a folder switch would otherwise
        # sit here forever since thread_key never actively expires
        # (see _expanded_thread_keys' own docstring for why).
        self._thread_members_by_key = {}
        for root in roots:
            key = thread_grouping.thread_key(root.envelope)
            if self._thread_filter != "all":
                # Looked up per root, not once for the whole render,
                # and keyed off THIS root's own account -- a Unified
                # folder mixes accounts, each with its own
                # thread_state.json, so one global (watched_keys,
                # ignored_keys) pair for the whole list would silently
                # match nothing there (see _thread_flags_provider's
                # own docstring). Skipped entirely for the common
                # "all" case, which needs no filtering decision at
                # all.
                account_id = root.envelope.get("_zbox_account_id")
                watched_keys, ignored_keys = self._thread_flags_provider(account_id)
                if self._thread_filter == "watched" and key not in watched_keys:
                    continue
                if self._thread_filter == "ignored" and key not in ignored_keys:
                    continue

            count = root.message_count()
            if count == 1:
                rows.append((root.envelope, None))
                continue

            if _thread_expanded(self, key):
                for node, depth in root.walk():
                    info = _RowThreadInfo(
                        thread_key=key, is_root=(depth == 0),
                        is_collapsed=False, depth=depth, message_count=count,
                    )
                    rows.append((node.envelope, info))
            else:
                members = root.all_envelopes()
                self._thread_members_by_key[key] = members
                info = _RowThreadInfo(
                    thread_key=key, is_root=True, is_collapsed=True,
                    depth=0, message_count=count,
                    aggregate_unread=any(_envelope_is_unread(e) for e in members),
                    aggregate_flagged=any(_envelope_is_flagged(e) for e in members),
                )
                rows.append((root.envelope, info))
        return rows

    def _thread_is_expanded(self, thread_key):
        """Whether this thread shows expanded under the current
        Expand All / Collapse All state. See _thread_expanded."""
        return _thread_expanded(self, thread_key)

    def _expand_thread(self, thread_key):
        # Logged for the same reason _open_message_tab is: "it opened
        # by itself while I was arrowing" has to be answerable, and
        # an expand and a tab-open sound similar from the outside.
        logger.debug("Expanding thread %s.", thread_key)
        self._expanded_thread_keys.add(thread_key)
        self.__dict__.setdefault("_collapsed_thread_keys", set()).discard(thread_key)
        self._render()
        self._focus_thread_row(thread_key)

    def _collapse_thread(self, thread_key):
        self._expanded_thread_keys.discard(thread_key)
        self.__dict__.setdefault("_collapsed_thread_keys", set()).add(thread_key)
        self._render()
        self._focus_thread_row(thread_key)

    def _focus_thread_row(self, thread_key):
        """Selects/focuses whichever row now represents this thread
        after a re-render -- the collapsed row if it just collapsed,
        the root's own row (now the first of several) if it just
        expanded -- so the user's position holds steady across the
        toggle instead of landing back at row 0."""
        for row, info in enumerate(self._row_thread_info):
            if info is not None and info.thread_key == thread_key:
                self.list_ctrl.Select(row)
                self.list_ctrl.Focus(row)
                self.list_ctrl.EnsureVisible(row)
                return

    def collapse_focused_thread(self):
        """
        Escape's belt-and-suspenders collapse: if the currently
        focused row belongs to a thread that is currently expanded,
        collapses it and returns True. Returns False when there is
        nothing thread-related to collapse (threading is off, nothing
        is selected, or the selected row isn't part of an expanded
        thread) -- main_frame's frame-wide Escape handler falls
        through to its own normal behaviour (closing the current tab)
        in that case, so Escape's existing meaning everywhere else is
        unaffected. main_frame delegates to this rather than
        envelope_list_panel handling Escape itself in _on_key_down,
        because EVT_CHAR_HOOK at the frame level sees Escape first and
        (per its own docstring) never lets it reach a child control's
        own key handler at all once it decides to act on it.
        """
        if not self._enable_threading:
            return False
        _index, info = self._selected_row_and_info()
        if info is None or info.is_collapsed or not self._thread_is_expanded(info.thread_key):
            return False
        self._collapse_thread(info.thread_key)
        return True

    def set_thread_filter(self, mode):
        """
        View > Threads > All / Watched Threads / Ignored Threads.
        mode is "all", "watched" or "ignored"; anything else is
        ignored rather than raising, since this is reached from a
        menu handler with no other validation in front of it.

        Purely a display lens (see _build_threaded_rows and thread_
        state.py's own docstring): switching to "watched" or
        "ignored" hides every thread not on that list, and switching
        back to "all" brings every one of them back, without ever
        touching the account, the folder, or the messages themselves
        -- the same guarantee muting an account already makes for
        notifications.
        """
        if mode not in ("all", "watched", "ignored") or mode == self._thread_filter:
            return
        self._thread_filter = mode
        self._render()

    def threads_expanded(self):
        """The current Expand All (True) / Collapse All (False) state."""
        return self._threads_expanded_default

    def set_threads_mode_handler(self, handler):
        """main_frame's callable taking one bool, run for the bare
        "*"/"\" keys so the menu check, the saved setting and the
        announcement follow the key exactly as they follow the menu."""
        self._threads_mode_handler = handler

    def set_threads_expanded(self, expanded, redraw=True):
        """
        View > Threads > Expand All Threads (True) or Collapse All
        Threads (False). A lasting state: every thread in every
        folder follows it, including threads that arrive later, until
        the other one is chosen. Choosing either one also forgets
        every thread expanded or collapsed by hand.

        The redraw keeps the reader's place: populate re-selects the
        selected message by key, and when that message has just been
        folded into a collapsed thread, the thread's own row takes the
        selection instead of the list landing on nothing.
        """
        expanded = bool(expanded)
        logger.debug(
            "Threads state set to %s (threading %s, redraw %s).",
            "expanded" if expanded else "collapsed",
            "on" if self._enable_threading else "off", redraw,
        )
        self._threads_expanded_default = expanded
        self._expanded_thread_keys.clear()
        self._collapsed_thread_keys.clear()
        if not redraw or not self._enable_threading or not self._all_envelopes:
            return
        thread_key = self.selected_thread_key()
        self.populate(self._all_envelopes)
        if self.list_ctrl.GetFirstSelected() == -1 and thread_key is not None:
            self._focus_thread_row(thread_key)

    def expand_all_threads(self):
        """View > Threads > Expand All Threads (bare "*" in the
        message list, matching Thunderbird)."""
        self.set_threads_expanded(True)

    def collapse_all_threads(self):
        """View > Threads > Collapse All Threads (bare "\" in the
        message list, matching Thunderbird)."""
        self.set_threads_expanded(False)

    def has_content(self):
        """True when real envelopes are showing (as opposed to a
        placeholder row). Background auto-refreshes skip until a
        folder has actually been loaded once."""
        return bool(self._row_envelopes)

    def has_listing(self):
        """True once a real folder listing is on screen, even an empty
        one ("No messages in this folder."), and False for every other
        placeholder, such as "Loading...". The silent auto-refresh
        uses this rather than has_content, which skipped an empty
        folder until it was reselected."""
        return bool(self._row_envelopes) or getattr(self, "_listing_loaded", False)

    def _holding_top(self):
        """
        True while the startup focus is held on the top row: set by
        main_frame._maybe_apply_initial_focus (hold_top_until), ended by
        the reader's first key press in the list (reader_moved) or two
        minutes at most, and only while the list itself has keyboard
        focus, so it never pulls the reader out of the folder tree.
        """
        import time

        if getattr(self, "reader_moved", False):
            return False
        if time.monotonic() > getattr(self, "hold_top_until", 0.0):
            return False
        try:
            return bool(self.list_ctrl.HasFocus())
        except Exception:  # noqa: BLE001 - no list, no hold
            return False

    def populate_if_changed(self, envelopes):
        """
        Silent auto-refresh variant of populate: repopulates only
        when the envelope content actually changed, so an idle poll
        never resets the user's selection or scroll position on every
        tick. Returns True when the list was updated.

        When content did change, the previous selection (single row or
        a Shift/Ctrl multi-select sweep) is re-applied to the matching
        rows afterwards: a silent refresh that wipes the selection is
        exactly what made the list feel jumpy under a screen reader
        (focus silently lands nowhere and the next arrow press has to
        re-establish where the user was). Rows that disappeared (e.g.
        the message the user just deleted) simply don't match, and the
        first surviving previously-selected row takes the focus.
        """
        # Filtered before the comparison: _signature is taken from the
        # filtered set in _render, so comparing the raw listing would
        # count every pending removal as a change on each silent tick.
        envelopes = self._without_hidden(envelopes)
        new_signature = _envelopes_signature(envelopes)
        if new_signature == self._signature:
            return False

        # Snapshot the selection (by (account, id) key, not row index,
        # so rows that shift when new mail sorts in still match).
        selected_rows = []
        row_index = self.list_ctrl.GetFirstSelected()
        while row_index != -1:
            selected_rows.append(row_index)
            row_index = self.list_ctrl.GetNextSelected(row_index)
        previous_keys = [
            (
                self._row_envelopes[row].get("_zbox_account_id"),
                self._row_envelopes[row].get("id"),
            )
            for row in selected_rows
            if row < len(self._row_envelopes)
        ]

        self.populate(envelopes)

        if self._holding_top():
            # Startup: newer rows sorted in above the selected one. The
            # reader has not moved yet, so focus stays on the top row
            # instead of following that message down the list.
            self.focus_top_message()
            return True

        if previous_keys:
            matched = [
                row for row, envelope in enumerate(self._row_envelopes)
                if (envelope.get("_zbox_account_id"), envelope.get("id")) in previous_keys
            ]
            if matched:
                self.list_ctrl.Select(matched[0])
                self.list_ctrl.Focus(matched[0])
                for row in matched[1:]:
                    self.list_ctrl.Select(row)
                self.list_ctrl.EnsureVisible(matched[0])
        return True

    def replace_account_rows(self, account_id, envelopes):
        """
        A unified view's rows for one account swapped for `envelopes`,
        every other account's rows kept as they are, then repainted
        only if anything changed (populate_if_changed, selection kept
        by key). This is what lets one account's new mail appear the
        moment it is known, without waiting for every other account's
        server -- and what keeps an account's rows on screen when its
        own refresh fails. Returns True when the list was updated.
        """
        kept = [
            envelope for envelope in self._all_envelopes
            if envelope.get("_zbox_account_id") != account_id
        ]
        return self.populate_if_changed(kept + list(envelopes))

    def append(self, envelopes):
        """
        Adds another page of envelopes below what is already showing
        instead of replacing the list (View > Load More Messages,
        audit finding 11 -- list_envelopes never asked Himalaya for
        anything past page 1, so message 201 was unreachable in any
        folder). Selection is preserved the same way populate_if_
        changed does it, by (account, id) key rather than row index,
        since the sort can move existing rows once the new ones are
        merged in.
        """
        if not envelopes:
            return

        selected_rows = []
        row_index = self.list_ctrl.GetFirstSelected()
        while row_index != -1:
            selected_rows.append(row_index)
            row_index = self.list_ctrl.GetNextSelected(row_index)
        previous_keys = [
            (
                self._row_envelopes[row].get("_zbox_account_id"),
                self._row_envelopes[row].get("id"),
            )
            for row in selected_rows
            if row < len(self._row_envelopes)
        ]

        # Appends to self._all_envelopes, the real full set -- not
        # self._row_envelopes, which is only the currently *visible*
        # rows and would silently and permanently drop every reply
        # hidden inside a collapsed thread if appended to instead.
        self.populate(self._all_envelopes + list(envelopes))

        if previous_keys:
            matched = [
                row for row, envelope in enumerate(self._row_envelopes)
                if (envelope.get("_zbox_account_id"), envelope.get("id")) in previous_keys
            ]
            if matched:
                self.list_ctrl.Select(matched[0])
                self.list_ctrl.Focus(matched[0])
                for row in matched[1:]:
                    self.list_ctrl.Select(row)
                self.list_ctrl.EnsureVisible(matched[0])

    def drop_hidden_rows(self):
        """
        Takes the rows the hidden_keys_provider now names off the list
        at once, with no fetch -- the instant half of a batched Delete
        or Shift+Delete. Redraws from _all_envelopes, then selects and
        focuses the row index the first selected row had, clamped to
        the new end, so the reader lands on the message that moved up
        into the deleted one's place.
        """
        remaining = self._without_hidden(self._all_envelopes)
        if len(remaining) == len(self._all_envelopes):
            return
        first = self.list_ctrl.GetFirstSelected()
        self._all_envelopes = remaining
        self._render()
        count = len(self._row_envelopes)
        if count == 0:
            return
        index = min(max(first, 0), count - 1)
        self.list_ctrl.Select(index)
        self.list_ctrl.Focus(index)
        self.list_ctrl.EnsureVisible(index)

    def focus_top_message(self):
        """
        Selects and focuses the first row, so a screen reader
        announces the top message immediately after the initial load
        completes rather than requiring the user to Ctrl+PageDown
        into the message list and press an arrow key first. Does
        nothing if the list is empty or only showing a placeholder
        row (see has_content()) -- there's nothing meaningful to land
        on yet.
        """
        if not self.has_content():
            return
        self.list_ctrl.SetFocus()
        self.list_ctrl.Select(0)
        self.list_ctrl.Focus(0)
        self.list_ctrl.EnsureVisible(0)

    def selected_envelope(self):
        index = self.list_ctrl.GetFirstSelected()
        if index == -1 or index >= len(self._row_envelopes):
            return None
        return self._row_envelopes[index]

    def get_all_selected_envelopes(self):
        """
        Returns every envelope a bulk action (Delete, Archive, Move
        To, Copy To, Flag/Unflag, Mark as Junk/Not Junk, Mark as
        Read/Unread) should act on for the current selection, in row
        order. Returns an empty list if nothing is selected.

        A selected row standing in for a COLLAPSED thread expands to
        every message in that thread (via _thread_members_by_key) --
        closing a high-traffic thread and then deleting or archiving
        it takes the whole thread with it, matching Thunderbird,
        rather than silently acting on just the one visible root
        message underneath. A selected row that is part of an
        EXPANDED thread (its root or one of its visible replies)
        still contributes only its own single message, exactly as
        before threading existed -- expanding a thread is what lets
        the messages inside it be worked on one at a time, so an
        expanded row must never pull its whole thread along too.
        """
        selected_indices = []
        item_index = self.list_ctrl.GetFirstSelected()
        while item_index != -1:
            selected_indices.append(item_index)
            item_index = self.list_ctrl.GetNextSelected(item_index)

        envelopes = []
        seen_ids = set()
        for index in selected_indices:
            if index >= len(self._row_envelopes):
                continue
            info = self._row_thread_info[index] if index < len(self._row_thread_info) else None
            if info is not None and info.is_collapsed:
                members = self._thread_members_by_key.get(info.thread_key, [])
            else:
                members = [self._row_envelopes[index]]
            for member in members:
                member_id = member.get("id")
                if member_id is not None and member_id in seen_ids:
                    continue
                seen_ids.add(member_id)
                envelopes.append(member)
        return envelopes

    def get_selected_item_count(self):
        """
        Returns the total number of currently selected items in the list.
        Used for UX guards (e.g., showing an error if multi-select is
        active but the action only supports single messages like Reply).
        """
        return self.list_ctrl.GetSelectedItemCount()

    def _on_selection_count_changed(self, event):
        """
        Debounced entry point for both EVT_LIST_ITEM_SELECTED and
        EVT_LIST_ITEM_DESELECTED. A single Shift+Down sweep across 5
        rows fires 5 separate events; without debouncing, a screen
        reader would hear 5 rapid-fire announcements instead of one
        settled count. wx.CallLater coalesces the burst -- each new
        event restarts the short timer, so _announce_selection_state
        only actually runs once, after the sweep stops moving.
        """
        event.Skip()
        if self._selection_announce_handler is None:
            return
        if self._announce_timer is not None:
            self._announce_timer.Stop()
        self._announce_timer = wx.CallLater(180, self._announce_selection_state)

    def _announce_selection_state(self):
        """
        Speaks the settled selection count once a Shift+Up/Down sweep
        or Ctrl+A / Select All finishes. Counts of 0 or 1 are left
        silent: 1 selected is already announced by the normal
        arrow-key row reading, and 0 (nothing selected) isn't
        actionable enough to interrupt with a spoken message.
        """
        if self._selection_announce_handler is None:
            return
        count = self.list_ctrl.GetSelectedItemCount()
        if count < 2:
            return
        total = self.list_ctrl.GetItemCount()
        if count == total and total > 1:
            message = f"All {count} messages selected."
        else:
            message = f"{count} messages selected."
        self._selection_announce_handler(message)

    def select_all(self):
        """
        Selects every row in the list (Ctrl+A / the "Select All"
        context menu entry). No-op on an empty or placeholder list.
        """
        if not self.has_content():
            return
        for row in range(self.list_ctrl.GetItemCount()):
            self.list_ctrl.Select(row)

    def mark_row_read(self, message_id):
        """
        Marks one message read in place, without a full refetch, so
        the change is visible immediately after opening it. See
        mark_ids_read for the multi-message form and
        _mark_ids_and_refresh for why this looks the message up in
        the full underlying set rather than by visible row.
        """
        self.mark_ids_read([message_id])

    def mark_row_unread(self, message_id):
        self.mark_ids_unread([message_id])

    def mark_row_flagged(self, message_id, flagged):
        """Same in-place update pattern as mark_row_read/unread, kept
        separate since flagging doesn't touch the read state."""
        self.mark_ids_flagged([message_id], flagged)

    def mark_ids_read(self, message_ids):
        """Bulk form of mark_row_read -- one redraw for the whole
        batch instead of one per message, used after a multi-select
        (or whole-collapsed-thread) Mark as Read completes."""
        self._mark_ids_and_refresh(message_ids, lambda e: _set_envelope_flag(e, "seen", True))

    def mark_ids_unread(self, message_ids):
        self._mark_ids_and_refresh(message_ids, lambda e: _set_envelope_flag(e, "seen", False))

    def mark_ids_flagged(self, message_ids, flagged):
        self._mark_ids_and_refresh(
            message_ids, lambda e: _set_envelope_flag(e, "flagged", flagged)
        )

    def mark_ids_spam(self, labels):
        """
        Stamps each message id in `labels` (message_id -> "Blocked
        Sender" / "Possible Phishing") onto its envelope for the
        Status column to show, then redraws once -- the junk rules'
        counterpart to mark_ids_read/mark_ids_flagged above, called
        by mail_fetch._apply_spam_filter once the rules find something
        in new mail. Same underlying-set lookup as
        _mark_ids_and_refresh, and for the same reason: a newly-
        arrived message has no row of its own yet to patch directly.

        The label lives under a private key on the envelope dict
        (_zbox_spam_label) rather than as a real IMAP flag -- ZBox
        never wrote one server-side for this, and the Status column
        text must not imply that it did.
        """
        if not labels:
            return
        matched = False
        for envelope in self._all_envelopes:
            label = labels.get(envelope.get("id"))
            if label:
                envelope["_zbox_spam_label"] = label
                matched = True
        if not matched:
            return

        selected_rows = []
        index = self.list_ctrl.GetFirstSelected()
        while index != -1:
            selected_rows.append(index)
            index = self.list_ctrl.GetNextSelected(index)

        self._render()

        for selected_row in selected_rows:
            if selected_row < self.list_ctrl.GetItemCount():
                self.list_ctrl.Select(selected_row)
        if selected_rows:
            self.list_ctrl.Focus(selected_rows[0])

    def _mark_ids_and_refresh(self, message_ids, mutate):
        """
        Applies `mutate` to every envelope in self._all_envelopes --
        not just the currently visible rows -- whose id is in
        message_ids, then redraws once.

        Looking this up in the full underlying set rather than by row
        is what makes marking a whole collapsed thread read/unread/
        flagged actually visible: most of those messages have no row
        of their own to patch, since they're folded into the thread's
        one collapsed row (see get_all_selected_envelopes) -- their
        status can only change by mutating the envelope itself and
        letting a full _render() recompute that row's aggregate from
        it (_build_threaded_rows' aggregate_unread/aggregate_flagged).
        An ordinary non-threaded row is one id, one match, the same
        net effect as the direct single-cell SetItem this replaces.

        Selection is preserved by row index across the one _render()
        call: the same envelopes, sort and expanded set always
        produce the same row plan, so a render never reorders or
        drops rows on its own and the row selected before is still
        the right row after.
        """
        ids = set(message_ids)
        if not ids:
            return
        matched = False
        for envelope in self._all_envelopes:
            if envelope.get("id") in ids:
                mutate(envelope)
                matched = True
        if not matched:
            return

        selected_rows = []
        index = self.list_ctrl.GetFirstSelected()
        while index != -1:
            selected_rows.append(index)
            index = self.list_ctrl.GetNextSelected(index)

        self._render()

        for selected_row in selected_rows:
            if selected_row < self.list_ctrl.GetItemCount():
                self.list_ctrl.Select(selected_row)
        if selected_rows:
            self.list_ctrl.Focus(selected_rows[0])

    def _on_item_selected(self, event):
        # EVT_LIST_ITEM_SELECTED fires once per row as a multi-select
        # sweep (Shift+Up/Down, Select All) lands, and the selection
        # handler opens + marks read whatever it's given -- so with
        # multiple rows selected this is skipped entirely rather than
        # reading and marking every swept-over message read. A plain
        # single click/arrow-move (the normal case) still previews
        # and marks read exactly as before.
        if self.list_ctrl.GetSelectedItemCount() != 1:
            return
        envelope = self.selected_envelope()
        if envelope is not None:
            self._selection_handler(envelope)

    def rows_around_selection(self, before=3, after=9):
        """
        The envelopes on either side of the current selection, nearest
        first, for warming the cache in the direction the user is
        heading. Empty when nothing is selected.

        Weighted downward on purpose: people arrow down through a
        folder far more than up, and the row directly below the
        cursor is the one about to be needed. Nearest-first ordering
        matters because the caller warms only as many as its budget
        allows -- the next row must not lose its turn to one nine
        rows away.
        """
        index = self.list_ctrl.GetFirstSelected()
        if index == -1 or not self._row_envelopes:
            return []
        total = len(self._row_envelopes)
        ordered = []
        for distance in range(1, max(before, after) + 1):
            if distance <= after and index + distance < total:
                ordered.append(self._row_envelopes[index + distance])
            if distance <= before and index - distance >= 0:
                ordered.append(self._row_envelopes[index - distance])
        return ordered

    def _selected_row_and_info(self):
        """(row index, _RowThreadInfo or None) for the current
        selection, or (None, None) with nothing usefully selected --
        the shared lookup every thread key handler below and
        collapse_focused_thread() start from."""
        index = self.list_ctrl.GetFirstSelected()
        if index == -1 or index >= len(self._row_thread_info):
            return None, None
        return index, self._row_thread_info[index]

    def selected_thread_key(self):
        """
        Stable thread_key (see thread_grouping.thread_key) for
        whatever is currently selected, or None with nothing
        usefully selected. Used by main_frame's Watch Thread /
        Ignore Thread commands so toggling either one from any row
        of a thread -- the collapsed row, the root, or one of its
        visible replies once expanded -- always lands on the one key
        the View > Threads filters and the collapsed-row lookups
        already use.

        A row with a _RowThreadInfo (collapsed, or any row of an
        expanded thread) already carries the thread's own correct
        key there -- _build_threaded_rows stamps every one of them
        with thread_key(root.envelope), never the individual row's
        own envelope, so a reply's row is never mistaken for a
        thread of its own. A row with no info at all is either
        threading being off (search results, Related Messages) or a
        genuine single-message thread -- either way the envelope
        actually selected IS its own thread's root, so computing
        thread_key directly from it is correct rather than a
        fallback approximation.
        """
        index, info = self._selected_row_and_info()
        if index is None:
            return None
        if info is not None:
            return info.thread_key
        if index >= len(self._row_envelopes):
            return None
        return thread_grouping.thread_key(self._row_envelopes[index])

    def refresh_thread_flags(self):
        """
        Redraws so a Watch Thread / Ignore Thread toggle is
        reflected immediately -- most visibly when View > Threads
        is already filtered to Watched or Ignored, where toggling
        the very thread on screen has to make it appear or
        disappear right away rather than waiting for the next
        unrelated redraw. Same _render() plain set_thread_filter/
        expand_all_threads/collapse_all_threads already use for the
        same reason: nothing new was fetched, only what should be
        visible changed. A no-op when threading is off.
        """
        if self._enable_threading:
            self._render()

    def _on_item_activated(self, event):
        # NOTE: this fires for Enter and double-click -- and also
        # whenever something invokes the list item's default action
        # through the accessibility layer, which a screen reader can
        # do with no keystroke from the user at all. If a message
        # ever opens during plain arrow navigation, the presence of
        # this line with no preceding key event is the evidence for
        # that; its absence means the tab came from somewhere else
        # entirely (see _open_message_tab's own caller logging).
        index, info = self._selected_row_and_info()
        # Traced because activating a message inside an EXPANDED
        # thread was reported as doing nothing at all, and every
        # early return on this path is silent -- the whole gesture
        # left no line in the debug log to tell them apart.
        logger.debug(
            "Item activated: row=%s info=%s rows=%d envelopes=%d expanded=%s",
            index,
            None if info is None else (
                f"thread_key={info.thread_key} collapsed={info.is_collapsed} "
                f"root={info.is_root} depth={info.depth} count={info.message_count}"
            ),
            len(self._row_thread_info), len(self._row_envelopes),
            sorted(self._expanded_thread_keys),
        )
        if info is not None and info.is_collapsed:
            # Open the newest UNREAD message in the thread, as one
            # message, in one tab -- then Ctrl+Shift+Page Up/Down
            # flip through the rest without ever opening a second
            # message view. Right arrow still expands without
            # opening anything.
            #
            # This is the third behaviour for this gesture. It first
            # opened every message in the thread at once, which meant
            # one WebView2 per message and a screen reader that read
            # one message and went silent. It then expanded, which is
            # what Thunderbird does but left the user doing the work
            # of finding what was actually new. Opening the newest
            # unread message directly is what the user asked for, and
            # it is only safe now because flipping replaces the tab
            # rather than adding one.
            target = _newest_unread(
                self._thread_members_by_key.get(info.thread_key, [])
            )
            if target is None:
                logger.debug(
                    "Enter/Space on collapsed thread %s: no members known, "
                    "expanding instead.", info.thread_key,
                )
                self._expand_thread(info.thread_key)
                return
            logger.debug(
                "Enter/Space on collapsed thread %s (%d messages): opening %s.",
                info.thread_key, info.message_count, target.get("id"),
            )
            self._activation_handler(target)
            return
        envelope = self.selected_envelope()
        if envelope is None:
            logger.debug(
                "Item activated but selected_envelope() is None "
                "(first selected row=%s, %d envelopes) -- nothing opened.",
                self.list_ctrl.GetFirstSelected(), len(self._row_envelopes),
            )
            return
        logger.debug(
            "Activating envelope id=%s backend-ish keys=%s",
            envelope.get("id"),
            sorted(k for k in envelope if k.startswith("_zbox")),
        )
        self._activation_handler(envelope)

    def selected_thread_envelopes(self):
        """
        Every message of whichever thread the selection belongs to,
        in row order, for Message > Open Message in Conversation.
        Returns [] when nothing usable is selected.

        Works from the collapsed row or from any row of an expanded
        thread. They need different sources: _thread_members_by_key
        is only populated for COLLAPSED threads (and is rebuilt on
        every render), so an expanded thread has to be recovered from
        the rows actually on screen. A row with no thread info at all
        is a genuine thread of one.

        This replaced open_selected_thread_messages, which handed the
        same envelopes to a batch tab-opener. See
        conversation_panel.py for why opening a tab per message had
        to go.
        """
        index, info = self._selected_row_and_info()
        if index is None:
            return []
        if info is None:
            envelope = self.selected_envelope()
            return [envelope] if envelope is not None else []
        if info.is_collapsed:
            return list(self._thread_members_by_key.get(info.thread_key, []))
        return [
            self._row_envelopes[row]
            for row, row_info in enumerate(self._row_thread_info)
            if row_info is not None
            and row_info.thread_key == info.thread_key
            and row < len(self._row_envelopes)
        ]

    def _on_context_menu(self, event):
        self._context_menu_handler(self.list_ctrl, event)

    def _on_key_down(self, event):
        # Any key in the list ends the startup hold on the top row
        # (_holding_top): from here on repaints keep the reader's place.
        self.reader_moved = True
        key = event.GetKeyCode()
        # Every keypress the list actually receives. Without this
        # there is no way to show that an activation arrived with NO
        # key behind it -- which is the difference between the user
        # pressing Enter and a screen reader invoking the row's
        # default action through the accessibility layer. Absence of
        # evidence is the evidence here, so it has to be recorded.
        logger.debug(
            "Key down in the message list: code=%s ctrl=%s shift=%s alt=%s",
            key, event.ControlDown(), event.ShiftDown(), event.AltDown(),
        )
        if key == wx.WXK_DELETE:
            if self._key_handler is not None:
                self._key_handler("delete", event.ShiftDown())
            return  # consumed either way; ZBox handles Delete itself
        if event.ControlDown() and not event.AltDown() and key in (ord("A"), ord("a")):
            self.select_all()
            return
        if self._enable_threading and not event.ControlDown() and not event.AltDown():
            # Right reveals a collapsed thread's messages as a
            # sub-list without "opening" anything -- no size warning,
            # since nothing is being read yet, just shown. Left is
            # its plain inverse: collapses whichever thread the
            # selection is currently inside (root or one of its
            # visible replies), same as Escape (main_frame delegates
            # to collapse_focused_thread() for that one, since the
            # frame's own char hook would otherwise swallow Escape
            # first -- see that method's docstring). Space does what
            # Enter/double-click already do via EVT_LIST_ITEM_
            # ACTIVATED (_on_item_activated) -- ListCtrl doesn't fire
            # that event for a bare Space itself, so it's claimed
            # here instead.
            if key == wx.WXK_RIGHT:
                _index, info = self._selected_row_and_info()
                if info is not None and info.is_collapsed:
                    self._expand_thread(info.thread_key)
                    return
            elif key == wx.WXK_LEFT:
                _index, info = self._selected_row_and_info()
                if (
                    info is not None and not info.is_collapsed
                    and self._thread_is_expanded(info.thread_key)
                ):
                    self._collapse_thread(info.thread_key)
                    return
            elif key == wx.WXK_SPACE:
                _index, info = self._selected_row_and_info()
                if info is not None and info.is_collapsed:
                    # ListCtrl doesn't fire EVT_LIST_ITEM_ACTIVATED
                    # for a bare Space, so route it through the same
                    # handler Enter uses rather than duplicating the
                    # decision here.
                    self._on_item_activated(None)
                    return
            else:
                # "*"/"\" (Expand/Collapse All Threads) need the
                # actual typed character, not GetKeyCode()'s virtual
                # key: '*' is Shift+8 on a US layout, and different
                # again on others, so comparing GetKeyCode() against
                # ord("*") the way the bare-letter shortcuts below
                # compare against ord("S") would only work by
                # accident on one specific layout. GetUnicodeKey()
                # gives the character the OS actually produced for
                # this keypress instead, wx.WXK_NONE when the key has
                # no character at all (arrows, function keys, etc).
                character = event.GetUnicodeKey()
                if character in (ord("*"), ord("\\")):
                    expanded = character == ord("*")
                    if self._threads_mode_handler is not None:
                        self._threads_mode_handler(expanded)
                    else:
                        self.set_threads_expanded(expanded)
                    return
        # Bare S/A intentionally override wx.ListCtrl's built-in
        # type-ahead jump-to-item-starting-with-this-letter behavior,
        # matching Thunderbird's single-key Star/Archive shortcuts
        # from the accessibility blueprint. Only bare presses are
        # claimed; Ctrl/Alt combinations pass through untouched.
        # Configurable (audit finding 19): Settings > "Enable
        # single-key S and A shortcuts" turns this off, returning S
        # and A to plain list type-ahead -- for anyone who relies on
        # first-letter navigation, or wants Archive's single
        # unmodified keypress (no confirmation, no undo) out of reach.
        if (
            not event.ControlDown()
            and not event.AltDown()
            and self._bare_key_shortcuts_enabled()
        ):
            if key in (ord("S"), ord("s")):
                if self._key_handler is not None:
                    self._key_handler("flag", False)
                return
            if key in (ord("A"), ord("a")):
                if self._key_handler is not None:
                    self._key_handler("archive", False)
                return
            # Audit finding 46: bare J/Shift+J for Mark as Junk/Not
            # Junk, matching Thunderbird's own single-key bindings --
            # same reasoning and same opt-out (Settings) as S and A
            # above.
            if key in (ord("J"), ord("j")):
                if self._key_handler is not None:
                    self._key_handler("not_junk" if event.ShiftDown() else "junk", False)
                return
            # Bare M: the Move To menu of folders; Shift+M: move by
            # typing part of a folder name; bare C: the Copy To menu.
            # The same folders, wording and actions as the list's own
            # context menu (main_frame._move_copy_targets). Same opt-out
            # as the keys above. Shift+C is left to type-ahead.
            if key in (ord("M"), ord("m")):
                if self._key_handler is not None:
                    self._key_handler("move_to", event.ShiftDown())
                return
            if key in (ord("C"), ord("c")) and not event.ShiftDown():
                if self._key_handler is not None:
                    self._key_handler("copy_to", False)
                return
            # Bare W/K: Watch Thread / Ignore Thread, matching
            # Thunderbird's own single-key bindings for the same
            # commands (there, scoped to newsgroups; here, to
            # whichever thread the selection is currently part of).
            # Gated on _enable_threading same as the Right/Left/
            # Space/*/\ handling above -- on the non-threaded
            # search-results/Related-Messages instances of this
            # same panel, W and K simply fall through to plain list
            # type-ahead, same as before this existed.
            if self._enable_threading:
                if key in (ord("W"), ord("w")):
                    if self._key_handler is not None:
                        self._key_handler("watch_thread", False)
                    return
                if key in (ord("K"), ord("k")):
                    if self._key_handler is not None:
                        self._key_handler("ignore_thread", False)
                    return
        event.Skip()
