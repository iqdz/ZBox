"""
Keyboard Shortcuts dialog.

Was a wx.MessageBox with a hand-maintained string that had drifted
out of sync with the actual bindings, and twenty-odd lines of text in
a MessageBox has no way to arrow through it a line at a time -- audit
finding 20. Reuses DocumentationDialog's pattern: a read-only
wx.TextCtrl wearing the static-text accessible role, so NVDA/JAWS
read it as ordinary text and Up/Down/Ctrl+Home/End all work.

SHORTCUTS_TEXT is generated from the same bindings the app actually
sets up (see accel_table in main_frame._bind_commands, the \t
accelerators on the menu items themselves, compose_panel's
accel_table, and envelope_list_panel._on_key_down) -- if a shortcut
changes, update it here in the same commit.
"""

import wx

import lang
from accessible import bind_close_accelerators, make_read_only_viewer


SHORTCUTS_TEXT = (
    "General\n"
    "F6: Cycle focus between panes\n"
    "Ctrl+Tab: Next tab\n"
    "Ctrl+Shift+Tab: Previous tab\n"
    "Ctrl+W or Ctrl+F4: Close current tab\n"
    "Escape: Collapse an expanded thread if the message list has "
    "focus and one is expanded; otherwise close current tab (message, "
    "saved message, or an unsent compose -- prompts first if it has "
    "unsaved changes)\n"
    "Application key or Shift+F10: Open context menu on the focused "
    "list or tree item\n"
    "F1: Keyboard Shortcuts, this page\n"
    "Ctrl+Shift+Q: Exit -- always actually quits ZBox, even when "
    "\"Close to the system tray\" is on in Settings (the window's "
    "Close button, Alt+F4, and Ctrl+W on the Mail tab hide to the "
    "tray instead of quitting when that setting is enabled; the "
    "tray icon's own Quit item also always really quits)\n"
    "Ctrl+Shift+Delete: Clear Offline Message Cache -- ZBox's local "
    "copies only, nothing on any server (also a button in Tools > "
    "Cache Clean Up Configuration)\n"
    "\n"
    "Mail\n"
    "Ctrl+N: New Message\n"
    "Ctrl+Shift+N: New Email Account\n"
    "F5: Get New Mail for the current account\n"
    "Shift+F5: Get New Mail for all accounts\n"
    "Ctrl+Shift+J: Jump to Unified Folders\n"
    "Ctrl+Shift+B: Address Book\n"
    "Ctrl+Shift+F: Search Messages\n"
    "Ctrl+Shift+T: Show Related Messages\n"
    "\n"
    "The message list\n"
    "Delete, Shift+Delete, Archive, Flag/Unflag, Mark as Junk/Not "
    "Junk, Move To and Copy To all apply to every message in a "
    "thread when used on a COLLAPSED thread row -- expand the "
    "thread first (Right arrow or Enter/Space) to act on one "
    "message inside it instead\n"
    "Ctrl+A: Select all messages\n"
    "Ctrl+Shift+L: Load More Messages\n"
    "Delete: Move message(s) to Trash\n"
    "Shift+Delete: Permanently delete message(s)\n"
    "Ctrl+Z: Undo the last Delete, Archive, Mark as Junk or Mark as "
    "Not Junk\n"
    "S: Flag/unflag the selected message(s)\n"
    "A: Archive the selected message(s)\n"
    "J: Mark the selected message(s) as junk\n"
    "Shift+J: Mark the selected message(s) as not junk\n"
    "W: Watch Thread -- toggle whether the thread the selection is "
    "part of is Watched (also Message menu and right-click)\n"
    "K: Ignore Thread -- toggle whether that thread is Ignored "
    "(also Message menu and right-click)\n"
    "S, A, J, W and K above are on by default and can be turned off "
    "in Settings (they otherwise take priority over the message "
    "list's own first-letter type-ahead navigation)\n"
    "Enter or Space on a collapsed thread: opens the newest unread "
    "message in that thread (the newest message if the whole thread "
    "is read) as a single message, in one tab -- then Ctrl+Shift+"
    "Page Down and Ctrl+Shift+Page Up flip through the rest of the "
    "thread without ever opening a second message. On an ordinary "
    "message, opens it as usual\n"
    "Right arrow on a collapsed thread: reveals its messages as a "
    "list, without opening any of them -- open one at a time from "
    "there with its own Enter or Space\n"
    "Ctrl+Shift+O: Open Message in Conversation -- the whole thread "
    "in one tab, as a list of its messages with the selected one's "
    "body underneath. Arrow through the list to read them one at a "
    "time; Enter on a row opens that single message in its own full "
    "tab. Same name and key as Thunderbird's own command\n"
    "Left arrow: collapse the thread the selection is currently "
    "inside\n"
    "*: Expand All Threads (also View > Threads); \\: Collapse "
    "All Threads (also View > Threads)\n"
    "View > Threads > All / Watched Threads / Ignored Threads "
    "filters which threads are shown -- switching back to All "
    "always brings every message back; nothing is ever removed "
    "from the folder by this\n"
    "\n"
    "An open message\n"
    "Alt+A: Archive this message -- only while its body (Text or "
    "HTML) actually has keyboard focus; elsewhere Alt+A keeps its "
    "normal meaning of opening the Accounts menu\n"
    "Ctrl+R: Reply\n"
    "Ctrl+Shift+R: Reply All\n"
    "Ctrl+L: Forward\n"
    "Ctrl+Delete or Ctrl+D: Move this message to Trash. Plain Delete "
    "is often taken by the screen reader while an HTML message has "
    "focus; Shift+Delete deletes permanently\n"
    "Ctrl+Shift+Page Down: Next message in this thread; "
    "Ctrl+Shift+Page Up: previous. Replaces the current tab rather "
    "than opening another, so only one message is ever open. "
    "At the last message (or the first, going up) it says so and "
    "asks; pressing the same key again closes the message and ends "
    "the thread, landing back in the message list. "
    "(Ctrl+Page Up/Down are left alone -- those switch tabs)\n"
    "Ctrl+B: Toggle message view (Text/HTML)\n"
    "Ctrl+P: Print\n"
    "Ctrl+Shift+P: Print Preview\n"
    "Ctrl+S: Save Message As\n"
    "Ctrl+U: Announce sender name and address\n"
    "Ctrl+Shift+U: Announce sender and copy the address\n"
    "Ctrl+Shift+I: Show remote content in this message\n"
    "Ctrl+Shift+=: Increase message font size\n"
    "Ctrl+Shift+-: Decrease message font size\n"
    "Ctrl+Shift+0: Reset message font size\n"
    "\n"
    "Composing a message\n"
    "Ctrl+Enter or Alt+S: Send\n"
    "Ctrl+S: Save Draft\n"
    "Ctrl+Shift+A: Attach File\n"
    "Ctrl+Shift+S: Insert the signature of the account in From\n"
    "Ctrl+Shift+V: Paste as quotation\n"
    "Ctrl+Shift+W: Word count\n"
    "Ctrl+Shift+P: Preview the message the way it will be sent\n"
    "Escape, Ctrl+W or Ctrl+F4: Close the compose tab -- asks first "
    "if anything is unsaved, offering Save Draft, Discard or Keep "
    "editing\n"
    "Every command below is also on the Compose menu, which Alt+C "
    "opens even while focus is inside the message body\n"
    "\n"
    "Formatting, while composing\n"
    "Ctrl+B: Bold\n"
    "Ctrl+I: Italic\n"
    "Ctrl+Shift+X: Strikethrough\n"
    "Ctrl+Shift+8: Bulleted list\n"
    "Ctrl+Shift+7: Numbered list\n"
    "Ctrl+Shift+9: Quote\n"
    "Ctrl+Shift+6: Code\n"
    "Ctrl+Alt+1: Heading\n"
    "Ctrl+K: Insert link\n"
    "Ctrl+Shift+K: Say what formatting is in force where the caret "
    "is\n"
    "Ctrl+Shift+M: Open the Format menu wherever focus is, the "
    "message body included\n"
    "\n"
    "Spelling, while composing\n"
    "Ctrl+Shift+F7: Check spelling\n"
    "Ctrl+Shift+F9: Go to the next misspelling\n"
    "Ctrl+Shift+F8: Add the word it stopped on to the dictionary\n"
    "\n"
    "The same key in two places\n"
    "Ctrl+S: Save Draft while composing, Save Message As in an open "
    "message\n"
    "Ctrl+B: Bold while composing, switch between Text and HTML in "
    "an open message\n"
    "Ctrl+Shift+P: Preview while composing, Print Preview in an open "
    "message\n"
    "Alt+A: Archive, but only while a message body has keyboard "
    "focus; anywhere else it opens the Accounts menu\n"
    "\n"
    "Diagnostics\n"
    "Ctrl+Shift+F12: Hotkeys Diagnostic -- writes a line to the "
    "debug log for every combination pressed in a compose tab, so "
    "what a key actually reached can be proved. Off by default, and "
    "the same key turns it off again; the state is spoken either "
    "way\n"
)


class ShortcutsDialog(wx.Dialog):
    """Read-only, arrow-key-navigable list of every keyboard shortcut
    ZBox currently binds, grouped the way a screen-reader user
    encounters them (general, mail, the message list, an open
    message, composing)."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title=lang.t(
                "dialogs", "shortcuts_heading", default="Keyboard Shortcuts",
            ),
            size=(560, 640),
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)

        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "shortcuts_heading", default="Keyboard Shortcuts",
            ),
        )
        heading.SetFont(heading.GetFont().Bold())
        sizer.Add(heading, 0, wx.ALL, 8)

        self.text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.text.SetName(
            lang.t(
                "dialogs", "shortcuts_text_name",
                default="Keyboard shortcuts text",
            )
        )
        make_read_only_viewer(self.text)
        self.text.SetValue(lang.body("shortcuts", SHORTCUTS_TEXT))
        sizer.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        close_button = wx.Button(
            self, wx.ID_CLOSE,
            lang.t("dialogs", "btn_close_plain", default="Close"),
        )
        close_button.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        sizer.Add(close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.SetSizer(sizer)

        bind_close_accelerators(self, wx.ID_CLOSE)
        # Land on the shortcuts text so arrow keys read it immediately.
        wx.CallAfter(self.text.SetFocus)
