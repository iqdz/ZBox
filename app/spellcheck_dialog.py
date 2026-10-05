"""
The spell check dialog.

Kept apart from app/spellcheck.py, which has no wx in it, so the word
list and the suggester stay testable without a display -- the same
split already used for body_html, message_build and trix_html.

The dialog itself came from the session y compose lab and is barely
changed. What it does differently here is record what it changed, not
just what the text ended up as. A Trix body cannot have its text
replaced wholesale without flattening every bold, link and list in it
(open item 47), so the caller replays these edits one range at a time
against the editor instead. Each edit is expressed against the text as
it stood after the previous one, so replaying them in order is all the
caller has to do.
"""

import re

import wx

import lang
import notice_toast
from accessible import make_read_only_viewer
from spoken_feedback import speak
from spellcheck import SUGGESTION_LIMIT, get_dictionary, misspellings


class SpellCheckDialog(wx.Dialog):
    """
    One misspelling at a time.

    The unknown word sits in a focused read-only field, so a reader
    speaks it the moment the dialog appears rather than waiting to be
    asked. The line it came from is underneath, because a word out of
    context is often not enough to know what was meant. The
    suggestions are a real list control, which is arrowable and
    announced as a list, not a custom-drawn thing that reads as
    nothing.
    """

    def __init__(self, parent, text, dictionary):
        super().__init__(parent,
                         title=lang.t(
                             "dialogs", "title_spell_check",
                             default="Spell Check",
                         ),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.dictionary = dictionary
        self.result_text = text
        self.changes = []
        self._ignored = set()
        self._position = 0
        self._scan = []
        self._scan_text = None

        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                self,
                label=lang.control_label(
                    "sp_not_in_dictionary_label", "&Not in dictionary:",
                ),
            ),
            0, wx.ALL, 8,
        )
        self.word_field = wx.TextCtrl(self, style=wx.TE_READONLY)
        self.word_field.SetName(
            lang.t(
                "dialogs", "sp_not_in_dictionary_name",
                default="Not in dictionary",
            )
        )
        make_read_only_viewer(self.word_field)
        sizer.Add(self.word_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(
            wx.StaticText(
                self,
                label=lang.t(
                    "dialogs", "sp_in_this_line_label", default="In this line:",
                ),
            ),
            0, wx.ALL, 8,
        )
        self.context_field = wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE
        )
        self.context_field.SetName(
            lang.t("dialogs", "sp_in_this_line_name", default="In this line")
        )
        make_read_only_viewer(self.context_field)
        sizer.Add(self.context_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(
            wx.StaticText(
                self,
                label=lang.control_label(
                    "sp_suggestions_label", "&Suggestions:",
                ),
            ),
            0, wx.ALL, 8,
        )
        self.suggestion_list = wx.ListBox(self, choices=[])
        self.suggestion_list.SetName(
            lang.t("dialogs", "sp_suggestions_name", default="Suggestions")
        )
        sizer.Add(self.suggestion_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(
            wx.StaticText(
                self,
                label=lang.control_label(
                    "sp_replace_with_label", "Replace &with:",
                ),
            ),
            0, wx.ALL, 8,
        )
        self.replacement_field = wx.TextCtrl(self)
        self.replacement_field.SetName(
            lang.t("dialogs", "sp_replace_with_name", default="Replace with")
        )
        sizer.Add(self.replacement_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.replace_button = wx.Button(
            self, label=lang.control_label("sp_replace", "&Replace")
        )
        self.replace_all_button = wx.Button(
            self, label=lang.control_label("sp_replace_all", "Replace &All")
        )
        self.ignore_button = wx.Button(
            self, label=lang.control_label("sp_ignore", "&Ignore")
        )
        self.ignore_all_button = wx.Button(
            self, label=lang.control_label("sp_ignore_all", "Ignore A&ll")
        )
        self.add_button = wx.Button(
            self, label=lang.control_label("sp_add_to_dictionary", "Add to &Dictionary")
        )
        self.close_button = wx.Button(
            self, wx.ID_CLOSE, label=lang.control_label("btn_close", "&Close")
        )
        self._action_buttons = (
            self.replace_button, self.replace_all_button, self.ignore_button,
            self.ignore_all_button, self.add_button,
        )
        for button in self._action_buttons + (self.close_button,):
            buttons.Add(button, 0, wx.RIGHT, 6)
        sizer.Add(buttons, 0, wx.ALL, 8)

        self.SetSizerAndFit(sizer)
        self.SetSize((560, 520))

        self.suggestion_list.Bind(wx.EVT_LISTBOX, self._on_pick)
        self.suggestion_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_replace)
        # Enter on the list, as well as the default button. A wx.ListBox
        # can consume Return before the dialog's default button ever
        # sees it, and Thunderbird treated exactly this as a bug worth
        # fixing (Bugzilla 1935401): people arrive expecting Enter to
        # accept the suggestion.
        self.suggestion_list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.replace_button.Bind(wx.EVT_BUTTON, self._on_replace)
        self.replace_button.SetDefault()
        self.replace_all_button.Bind(wx.EVT_BUTTON, self._on_replace_all)
        self.ignore_button.Bind(wx.EVT_BUTTON, self._on_ignore)
        self.ignore_all_button.Bind(wx.EVT_BUTTON, self._on_ignore_all)
        self.add_button.Bind(wx.EVT_BUTTON, self._on_add)
        self.close_button.Bind(wx.EVT_BUTTON, lambda _e: self.EndModal(wx.ID_OK))
        self.SetEscapeId(wx.ID_CLOSE)

        self._advance()

    # --- the walk -----------------------------------------------------

    def _current(self):
        """The next misspelling to deal with.

        The scan is cached against the text it was taken from, so
        arrowing through a long message does not re-scan the whole body
        on every word. Only a replacement invalidates it, which is the
        only thing that can change the answer.
        """
        if self._scan_text != self.result_text:
            self._scan_text = self.result_text
            self._scan = misspellings(self.result_text, self.dictionary)
        for start, end, word in self._scan:
            if start < self._position:
                continue
            if word.lower() in self._ignored:
                continue
            return start, end, word
        return None

    def _advance(self):
        found = self._current()
        if found is None:
            # Nothing left: close at once and get out of the way.
            # No announcement here, and nothing scheduled -- the
            # composer says how many corrections were applied once
            # focus is back in the body, which is both sooner and one
            # message rather than two. EndModal directly when the
            # modal loop is running; the very first _advance happens
            # inside __init__, before there is a loop to end.
            if self.IsModal():
                self.EndModal(wx.ID_OK)
            else:
                wx.CallAfter(self.EndModal, wx.ID_OK)
            return
        start, end, word = found
        self.word_field.SetValue(word)
        line_start = self.result_text.rfind("\n", 0, start) + 1
        line_end = self.result_text.find("\n", end)
        line_end = len(self.result_text) if line_end == -1 else line_end
        line = self.result_text[line_start:line_end]
        self.context_field.SetValue(line)
        picks = self.dictionary.suggest(word, SUGGESTION_LIMIT)
        self.suggestion_list.Set(picks)
        self.replacement_field.SetValue(picks[0] if picks else word)
        # Focus goes to the suggestions, so Down moves to the next one
        # and Enter accepts it. With no suggestions there is nothing to
        # arrow through, so the field where a correction is typed is
        # the only sensible place to land.
        if picks:
            self.suggestion_list.SetSelection(0)
            self.suggestion_list.SetFocus()
        else:
            self.replacement_field.SetFocus()
            self.replacement_field.SelectAll()
        # Nothing is said per word, deliberately. The reader already
        # announces the suggestion focus lands on, and a sentence about
        # every single word turns a fast walk into a lecture. The word
        # itself and the line it sits in are in the two read-only
        # fields above, a Shift+Tab away, for the times that is not
        # enough to know what was meant.

    def _on_pick(self, _event):
        selection = self.suggestion_list.GetStringSelection()
        if selection:
            self.replacement_field.SetValue(selection)

    def _on_list_key(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_pick(None)
            self._on_replace(None)
            return
        event.Skip()

    def _apply(self, start, end, replacement):
        """Records the edit as well as making it. The record is what
        lets a formatted body be corrected in place rather than
        rewritten."""
        self.result_text = (
            self.result_text[:start] + replacement + self.result_text[end:]
        )
        self.changes.append((start, end, replacement))
        self._position = start + len(replacement)

    def _on_replace(self, _event):
        found = self._current()
        if found is None:
            return
        start, end, _word = found
        self._apply(start, end, self.replacement_field.GetValue())
        self._advance()

    def _on_replace_all(self, _event):
        """Every remaining occurrence of the word on screen, one edit at
        a time rather than a single substitution over the whole text,
        so the change list stays a list of real ranges the caller can
        replay.

        This means every occurrence of THIS word, which is what Replace
        All means in every other editor, and not every misspelling in
        the message. It says how many it replaced, because a button
        that acts silently on a word you have already read is
        indistinguishable from one that did nothing -- which is exactly
        how it was reported.

        The sweep has to start at the top, or occurrences above the
        caret are missed. What it must not do is LEAVE the walk at the
        top: that marched back over every word already passed with
        Ignore, since Ignore only moves the position forward. So the
        walk resumes just past the occurrence that was on screen,
        shifted by whatever the replacements above it added or removed.
        """
        found = self._current()
        if found is None:
            return
        anchor, _end, word = found
        replacement = self.replacement_field.GetValue()
        if replacement == word:
            self._on_ignore(None)
            return
        pattern = re.compile(r"\b%s\b" % re.escape(word))
        self._position = 0
        shift = 0
        replaced = 0
        guard = 0
        while guard < 500:
            guard += 1
            match = pattern.search(self.result_text, self._position)
            if match is None:
                break
            if match.start() < anchor:
                shift += len(replacement) - (match.end() - match.start())
            self._apply(match.start(), match.end(), replacement)
            replaced += 1
        self._position = max(0, anchor + shift + len(replacement))
        if replaced == 1:
            spoken = lang.t(
                "actions_announcements", "occurrences_replaced_one",
                default="1 occurrence replaced.",
            )
        else:
            spoken = lang.t(
                "actions_announcements", "occurrences_replaced_many",
                default="{count} occurrences replaced.", count=replaced,
            )
        speak(self, spoken)
        self._advance()

    def _on_ignore(self, _event):
        """This one occurrence. The walk simply moves past it."""
        found = self._current()
        if found is None:
            return
        _start, end, _word = found
        self._position = end
        self._advance()

    def _on_ignore_all(self, _event):
        """Every remaining occurrence of the same word. Kept separate
        from Ignore because a button that quietly does more than its
        label says is how somebody stops trusting a dialog."""
        found = self._current()
        if found is None:
            return
        _start, _end, word = found
        self._ignored.add(word.lower())
        self._advance()

    def _on_add(self, _event):
        found = self._current()
        if found is None:
            return
        _start, _end, word = found
        self.dictionary.add_personal(word)
        self._advance()


def check_text(parent, text, personal_path=None):
    """
    Runs the walk over text.

    Returns (corrected_text, changes). changes is a list of
    (start, end, replacement) against the text as it stood after the
    previous entry, so a caller holding a formatted document can
    replay them in order instead of replacing the whole body.
    """
    dictionary = get_dictionary(personal_path)
    if not dictionary.loaded:
        wx.MessageBox(
            lang.t(
                "dialogs", "spellcheck_no_dictionary",
                default="The spelling dictionary is missing, so spell check is "
                "not available in this copy of ZBox.",
            ),
            lang.t("dialogs", "title_spell_check", default="Spell Check"),
            wx.OK | wx.ICON_WARNING, parent,
        )
        return text, []
    if not misspellings(text, dictionary):
        notice_toast.notify(
            parent,
            lang.t(
                "dialogs", "spellcheck_none_found",
                default="No misspellings found.",
            ),
            lang.t("dialogs", "title_spell_check", default="Spell Check"),
        )
        return text, []
    dialog = SpellCheckDialog(parent, text, dictionary)
    try:
        dialog.ShowModal()
        return dialog.result_text, dialog.changes
    finally:
        dialog.Destroy()
