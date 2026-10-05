"""
Compose panel. Opens as its own closable tab (Ctrl+N for a blank
message; Reply/Reply All/Forward will open one pre-filled once wired
to a selected message).

The body is Trix in ZBox's own WebView, or the markup engine in a
plain wx.TextCtrl when this machine has no WebView backend. Which one
is decided in code, never offered as a setting; see
app/compose_body.py. RichTextCtrl is not an option and never will be:
it is custom-drawn rather than a native Windows edit box, publishes
nothing to the accessibility layer, and JAWS reads blank while typing
into it. Measured, not assumed.

The one thing that changed shape when the body stopped being an edit
box: it cannot be read synchronously. The page pushes its content back
on every change and compose_body keeps the latest copy, so the unsaved
check and the autosave tick are as cheap as GetValue ever was. Send and
a manual Save Draft flush first, because those are the two places a
stale keystroke would actually leave the building.

Send builds a real RFC822 message with Python's email library and
pipes it directly to 'himalaya message send' on stdin (no temp file
needed), matching the pattern used consistently across every real
Himalaya usage example. Save Draft (Ctrl+S), a timed autosave, and
save-on-close all go through the same 'himalaya message add --mailbox
<Drafts> -f draft' append (audit finding 40) -- see
himalaya_client.add_message_raw and _save_draft below.
"""

import logging
import mimetypes
import os
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr

import wx

import body_html
import compose_body
import himalaya_client
import lang
import notice_toast
import openpgp_compose
import spellcheck
import trix_page
from accessible import fit_dialog, make_read_only_viewer
from account_manager import all_identities
from spoken_feedback import speak
from contacts import format_contact
from envelope_format import _folder_display_to_himalaya

logger = logging.getLogger("zbox.compose")

# RFC 3676's signature delimiter. Kept as its own constant because
# body_html.wrap_lines refuses to rewrap this exact line, and the two
# have to agree on what it looks like.
SIGNATURE_SEPARATOR = "-- "


def _signature_html(signature, formatted=""):
    """A signature as HTML, whatever it was stored as. The formatted
    version written by the signature editor wins when there is one.
    With none, this behaves exactly as it did before that editor
    existed: a plain text signature is converted, and one that
    already carries markup is left alone. That is what lets an
    account saved by an older build keep working with no migration."""
    formatted = (formatted or "").strip()
    if formatted:
        return formatted
    signature = signature or ""
    if body_html.has_formatting(signature):
        return signature
    return body_html.text_to_html(signature)


def _initial_body_html(body, signature, formatted=""):
    """What a compose tab opens with: the quoted original if there is
    one, then the delimiter and the signature. Built as HTML because
    the body is an HTML body now -- the plain text alternative is
    generated from it at send time, rather than the other way round."""
    parts = []
    if body:
        # An empty first line above the quoted original or the
        # forwarded message, with the caret on it (SET_HTML puts the
        # caret at the start), so a reply is written on top. Reply and
        # forward bodies are the ones that start with blank lines;
        # text_to_html drops those, so the empty line is put in here
        # as its own block. Any other body (a mailto: body) is left
        # as it was given.
        if body.startswith("\n"):
            parts.append("<div><br></div>")
        parts.append(body_html.text_to_html(body))
    if signature or formatted:
        parts.append(body_html.text_to_html(SIGNATURE_SEPARATOR))
        parts.append(_signature_html(signature, formatted))
    return "\n".join(parts)


def _identity_extra(account, email):
    """The extra identity entry sending as email from account, or an
    empty dict for the account's primary address. Every per-identity
    setting reads through this, so an identity with none of them
    behaves exactly as the account does."""
    finder = getattr(account, "identity_settings", None) if account is not None else None
    return (finder(email) if finder else None) or {}


def _identity_signature(account, email):
    """(plain, formatted, own) signature for sending as email from
    account: the identity's own when it has one, else the account's.
    own tells the announcements which one they are naming."""
    if account is None:
        return "", "", False
    extra = _identity_extra(account, email)
    if extra.get("own_signature"):
        return extra.get("signature", ""), extra.get("signature_html", ""), True
    return account.signature, account.signature_html, False


def _recipient_addresses(*fields):
    """Every address in the To, Cc and Bcc fields, in order. Each field is
    parsed on its own: since Python 3.13 getaddresses parses strictly, and
    an empty Cc or Bcc in one joined list could leave it with nothing,
    which sent encrypted mail with only the sender's own key. A field it
    cannot parse is split at its commas instead."""
    found = []
    for field in fields:
        field = (field or "").strip()
        if not field:
            continue
        parsed = [address for _name, address in getaddresses([field]) if address]
        if not parsed:
            parsed = [parseaddr(part)[1] for part in field.split(",") if parseaddr(part)[1]]
        found.extend(parsed)
    return found


def _address_items(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _swap_addresses(value, old, new):
    """A Cc or Bcc field's text with the addresses in old taken out,
    where they are still there unchanged, and those in new added once.
    Everything the writer typed stays as it is."""
    old_keys = {item.lower() for item in _address_items(old)}
    items = [item for item in _address_items(value) if item.lower() not in old_keys]
    present = {item.lower() for item in items}
    for item in _address_items(new):
        if item.lower() not in present:
            items.append(item)
            present.add(item.lower())
    return ", ".join(items)


def _focus_page_underneath(notebook):
    """Put focus on whatever tab a notebook shows now, the same way
    main_frame._close_tab_at does for every other tab close. A message
    tab focuses the message, a list focuses the list with its
    selection untouched, so a reply or a forward returns to what it
    was answering without the composer having to remember where it
    came from.

    Module level, and holding the notebook rather than the panel, on
    purpose. This runs after DeletePage, by which point the compose
    panel's C++ object is gone; a bound method would be reaching into
    a destroyed window.
    """
    try:
        index = notebook.GetSelection()
        if index == wx.NOT_FOUND:
            return
        page = notebook.GetPage(index)
    except RuntimeError:
        return
    # A message tab: the top of the message, inside the page
    # (MessageViewPanel.focus_page_top).
    focus_top = getattr(page, "focus_page_top", None)
    if focus_top is not None:
        focus_top()
        return
    focus_default = getattr(page, "focus_default", None)
    if focus_default is not None:
        focus_default()


def _suggestion_entries(provider, text):
    """The suggestions for an address field holding text, as (head,
    entries). Fields hold several comma-separated addresses, so only the
    part after the last comma is matched; head is everything before it,
    with the separator. Each entry is (value, email): one contact and its
    address, or every member of a matching group at once with None, since
    a group is not one address."""
    head, _, segment = (text or "").rpartition(",")
    segment = segment.strip()
    if not segment:
        return "", []
    head_text = (head + ", ") if head else ""
    entries = [
        (format_contact(contact), contact.get("email"))
        for contact in provider.search(segment)
    ]
    for group in provider.search_groups(segment):
        members = provider.group_members(group["name"])
        if members:
            entries.append((", ".join(format_contact(contact) for contact in members), None))
    return head_text, entries


def _suggestions(provider, text):
    """(head, values): _suggestion_entries without the addresses."""
    head, entries = _suggestion_entries(provider, text)
    return head, [value for value, _email in entries]


class _SavingDraftDialog(wx.Dialog):
    """
    Shown modally only when Save Draft is chosen from the close-tab
    prompt (confirm_discard). That method must answer synchronously
    whether it's safe to close the tab, but every Himalaya call in
    this app runs on the background worker thread by rule (see
    himalaya_worker.py's own docstring) -- never on the UI thread.
    ShowModal() here pumps the UI event loop while the real append
    happens on the worker; its on_success/on_error callback (always
    delivered via wx.CallAfter, so always back on the UI thread) ends
    the modal loop by calling finish(). Same technique
    bulk_progress_dialog.py already uses for the same reason, minus a
    Cancel button -- a draft save is one quick round trip, and
    cancelling it mid-way would defeat the reason it exists (not
    losing what was written).
    """

    def __init__(self, parent):
        super().__init__(
            parent,
            title=lang.t("dialogs", "title_save_draft", default="Save Draft"),
            style=wx.CAPTION,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)

        sizer = wx.BoxSizer(wx.VERTICAL)

        # A focused read-only field, not a StaticText, so a screen
        # reader announces it the instant the dialog appears --same
        # reasoning as AnnouncementDialog in announce.py.
        message = wx.TextCtrl(
            self, value=lang.t('dialogs', 'comp_saving_draft', default="Saving draft\u2026"),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE,
        )
        message.SetName(lang.t("dialogs", "title_save_draft", default="Save Draft"))
        make_read_only_viewer(message)
        width = self.GetTextExtent("x" * 40).width
        message.SetMinSize((width, message.GetCharHeight() * 2))
        sizer.Add(message, 1, wx.EXPAND | wx.ALL, 16)

        fit_dialog(self, sizer, min_width_chars=40)

    def finish(self):
        if self.IsModal():
            self.EndModal(wx.ID_OK)


class ComposePanel(wx.Panel):
    def __init__(self, parent, main_frame, from_identity="", to="", cc="", bcc="",
                 subject="", body="", in_reply_to="", references="", encrypt=None):
        super().__init__(parent)
        self.main_frame = main_frame
        # OpenPGP (stage 3): True for a reply or forward of encrypted mail,
        # which opens with Encrypt on, as Thunderbird does. The boxes the
        # writer changed by hand are never reset by a From change.
        self._pgp_reply_encrypted = bool(encrypt)
        self._pgp_touched = set()
        self.attachment_paths = []
        # Threading, set by Reply and Forward from the original
        # message's own headers (see message_body.threading_headers).
        # Empty for a new message, which genuinely starts a thread.
        self.in_reply_to = in_reply_to or ""
        self.references = references or ""

        sizer = wx.BoxSizer(wx.VERTICAL)

        header_grid = wx.FlexGridSizer(5, 2, 6, 8)
        header_grid.AddGrowableCol(1)

        from_label = wx.StaticText(
            self, label=lang.t("dialogs", "comp_from", default="From")
        )
        self.from_choice = wx.Choice(self, choices=self._identity_choices())
        self.from_choice.SetName(lang.t("dialogs", "comp_from", default="From"))
        if self.from_choice.GetCount() > 0:
            # Audit finding 49: from_identity is an email address to
            # preselect (the account's primary address for a new
            # message/forward, or whichever of the replying account's
            # identities the original was actually sent to -- see
            # envelope_format._reply_from_identity), matched against
            # the flattened identity list rather than the choice
            # control's own display strings, which now carry a
            # formatted "Name <email>" label rather than a bare
            # address.
            index = self._identity_index_for_email(from_identity) if from_identity else None
            self.from_choice.SetSelection(index if index is not None else 0)

        to_label = wx.StaticText(
            self, label=lang.t("dialogs", "comp_to", default="To")
        )
        self.to_field = wx.TextCtrl(self, value=to)
        self.to_field.SetName(lang.t("dialogs", "comp_to", default="To"))

        cc_label = wx.StaticText(
            self, label=lang.t("dialogs", "comp_cc", default="Cc")
        )
        self.cc_field = wx.TextCtrl(self, value=cc)
        self.cc_field.SetName(lang.t("dialogs", "comp_cc", default="Cc"))

        bcc_label = wx.StaticText(
            self, label=lang.t("dialogs", "comp_bcc", default="Bcc")
        )
        self.bcc_field = wx.TextCtrl(self, value=bcc)
        self.bcc_field.SetName(lang.t("dialogs", "comp_bcc", default="Bcc"))

        self.from_choice.Bind(wx.EVT_CHOICE, self._on_from_changed)
        # The selected identity's Always Cc and Always Bcc go into the
        # fields themselves, where they can be read and removed before
        # sending. Done before the unsaved-changes snapshot below, so
        # they never count as typing. See _apply_identity_copies.
        self._auto_copies = ("", "")
        opening_entry = self._selected_identity_entry()
        if opening_entry is not None:
            self._apply_identity_copies(opening_entry)

        subject_label = wx.StaticText(
            self, label=lang.t("dialogs", "comp_subject", default="Subject")
        )
        self.subject_field = wx.TextCtrl(self, value=subject)
        self.subject_field.SetName(
            lang.t("dialogs", "comp_subject", default="Subject")
        )

        # Address suggestions (_open_suggestions): a real list, hidden and
        # out of the tab order until Down Arrow in To, Cc or Bcc opens it.
        # Made here, after Subject and before the body, which is its place
        # in the tab order while it is shown.
        # An empty label right before the list. Windows names a list after
        # the label in front of it, which made JAWS call this one "Subject
        # list"; with an empty one it reads the addresses alone. Shown and
        # hidden with the list.
        self.suggestion_label = wx.StaticText(self, label="")
        self.suggestion_label.Hide()
        self.suggestion_list = wx.ListBox(self, choices=[], style=wx.LB_SINGLE)
        self.suggestion_list.SetMinSize(wx.Size(-1, 140))
        self.suggestion_list.Hide()
        self.suggestion_list.Bind(wx.EVT_LISTBOX_DCLICK, lambda _event: self._pick_suggestion())
        self.suggestion_list.Bind(wx.EVT_KILL_FOCUS, self._on_suggestions_left)
        self._suggest_field = None
        self._suggest_head = ""
        self._suggest_values = []
        self._suggest_emails = []

        for label, field in (
            (from_label, self.from_choice),
            (to_label, self.to_field),
            (cc_label, self.cc_field),
            (bcc_label, self.bcc_field),
            (subject_label, self.subject_field),
        ):
            header_grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
            header_grid.Add(field, 1, wx.EXPAND)

        sizer.Add(header_grid, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.suggestion_label, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.suggestion_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # No wx.StaticText label in front of the body, deliberately.
        # Three things would otherwise name this control and a screen
        # reader reads all of them: a static text, the WebView's
        # SetName, and the label inside the page itself. The page's
        # own label is the one that has to stay, because it names the
        # editor once the virtual cursor is inside it.
        #
        # Signature (audit finding 41) is taken from whichever account
        # is initially selected in From above, put in once here.
        # Switching From later rebuilds the body with the new account's
        # signature, but only while the body is untouched: finding and
        # replacing just the signature inside a body the writer has
        # edited around is not worth the risk. Once they have typed,
        # changing From says so and points at Insert Signature. See
        # _on_from_changed.
        #
        # The signature is the selected identity's own when it has one,
        # otherwise its account's. _signature_pair is the one the body
        # holds now, which is what a From change compares against.
        self._opening_body = body
        if opening_entry is not None:
            opening_plain, opening_formatted, _own = _identity_signature(
                opening_entry[0], opening_entry[2],
            )
            self._signature_account_id = opening_entry[0].account_id
        else:
            opening_plain, opening_formatted = "", ""
            self._signature_account_id = None
        self._signature_pair = (opening_plain, opening_formatted)
        self.body = compose_body.make_body(
            self,
            on_command=self._run_chord,
            on_announce=self._say,
            on_link=self._open_link,
            on_spell_menu=self._on_spell_menu,
            initial_html=_initial_body_html(body, opening_plain, opening_formatted),
        )
        sizer.Add(self.body.control, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # The hotkeys diagnostic, for everything in this tab that is an
        # ordinary wx control: the header fields, the attachment list
        # and the buttons. The body reports itself from inside its own
        # page, because a chord pressed in a WebView never reaches wx
        # at all. Bound always, doing nothing while the diagnostic is
        # off, and the key is passed on either way.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_hotkey_watch)
        self.body.set_hotkey_logging(self.main_frame.hotkeys_diagnostic)

        # Start reading the spelling dictionary now, on a background
        # thread, so the first Ctrl+Shift+F7 does not pay for it
        # between the keystroke and the caret moving. Costs nothing if
        # spell check is never used in this tab.
        self._spell_position = 0
        self._spell_word = ""
        spellcheck.warm(
            spellcheck.personal_path_for(self.main_frame.paths.userdata)
        )
        self._maybe_auto_diagnose()

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self.send_button = wx.Button(
            self, label=lang.control_label("title_send", "Send\tCtrl+Enter")
        )
        self.save_button = wx.Button(
            self,
            label=lang.control_label("title_save_draft", "Save Draft\tCtrl+S"),
        )
        self.attach_button = wx.Button(
            self,
            label=lang.control_label(
                "comp_attach_button", "Attach File...\tCtrl+Shift+A",
            ),
        )
        button_row.Add(self.send_button, 0, wx.RIGHT, 6)
        button_row.Add(self.save_button, 0, wx.RIGHT, 6)
        button_row.Add(self.attach_button, 0)
        # OpenPGP (stage 3): Thunderbird's Encrypt, Digitally Sign and
        # Attach My Public Key, after Attach File in the tab order, set from
        # the identity's End-to-End Encryption settings. Also on the Compose
        # menu, which reaches them from inside the body.
        self.encrypt_box = wx.CheckBox(self, label=lang.t("dialogs", "pgp_cm_encrypt", default="Encrypt"))
        self.sign_box = wx.CheckBox(self, label=lang.t("dialogs", "pgp_cm_sign", default="Digitally Sign"))
        self.attach_key_box = wx.CheckBox(
            self, label=lang.t("dialogs", "pgp_cm_attach_key", default="Attach My Public Key"),
        )
        for box in (self.encrypt_box, self.sign_box, self.attach_key_box):
            button_row.Add(box, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 12)
            box.Bind(wx.EVT_CHECKBOX, self._on_pgp_box)
        self._apply_pgp_defaults(opening_entry)
        sizer.Add(button_row, 0, wx.ALL, 8)

        self.SetSizer(sizer)

        self.send_button.Bind(wx.EVT_BUTTON, self._on_send)
        self.save_button.Bind(wx.EVT_BUTTON, self._on_save_draft)
        self.attach_button.Bind(wx.EVT_BUTTON, self._on_attach)

        self._bind_local_accelerators(self.send_button, self.save_button, self.attach_button)

        # Snapshot of everything the reader could have typed, taken
        # once the tab is fully built. Comparing against this beats
        # binding a "changed" flag to every field: a reply tab opens
        # pre-filled with a quoted body, and treating that pre-fill as
        # unsaved work would make every abandoned reply ask a
        # question nobody wants asked. Deleting a character and
        # retyping it also correctly leaves the tab clean.
        self._sent = False
        self._initial_state = self._current_state()
        # Updated on every successful draft save (autosave or
        # manual) so has_unsaved_changes() goes false right after a
        # save instead of staying permanently "dirty" against the
        # original pre-fill for the rest of the tab's life.
        self._last_saved_state = self._initial_state

        # Set on first save; reused on every later revision (manual
        # or autosave) of the SAME draft so the previous IMAP/Maildir
        # copy can be found and purged after a new one is appended --
        # there's no in-place update, only append + delete. See
        # _save_draft.
        self._draft_message_id = None
        self._last_draft_id = None
        self._last_draft_account_id = None
        # The folder that revision was saved to: an identity can have
        # its own Drafts folder, and From can change between saves.
        self._last_draft_folder = None

        self._autosave_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_autosave_timer, self._autosave_timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._restart_autosave_timer()

        # Where focus starts. A reply arrives with its recipient and
        # subject already filled in, so the only thing left to do is
        # write -- landing in To means tabbing past two fields that
        # are already correct before reaching the one that isn't.
        # The insertion point goes above the quoted original, which
        # is where a reply is normally written. A new message and a
        # forward both still start in To, because neither has a
        # recipient yet and that is genuinely the first thing needed.
        if to.strip():
            # The body may not have finished loading yet, in which
            # case it remembers the request and takes focus the moment
            # it has a document: focusing a WebView that has none is
            # what makes a screen reader announce an empty control.
            self.body.focus()
        else:
            self.to_field.SetFocus()

    def _current_state(self):
        return (
            self.to_field.GetValue(),
            self.cc_field.GetValue(),
            self.bcc_field.GetValue(),
            self.subject_field.GetValue(),
            self.body.get_text(),
            tuple(self.attachment_paths),
        )

    def has_unsaved_changes(self):
        """True if this tab holds typing that would be lost. False
        once the message has been sent, false for a pre-filled reply
        or forward the reader never touched, and false again right
        after a draft save (manual or autosave) -- compared against
        _last_saved_state, not the tab's original pre-fill."""
        if self._sent:
            return False
        try:
            return self._current_state() != self._last_saved_state
        except RuntimeError:
            # Controls already destroyed; nothing left to lose.
            return False

    def confirm_discard(self):
        """Asked before this tab closes. Returns True to go ahead.

        Three options now that drafts exist (finding 40): Save Draft,
        Discard, Keep editing. Save Draft blocks via
        _save_draft_blocking() until the append genuinely finishes --
        answering True here and closing the tab out from under a
        still-in-flight save would silently drop it back to no draft
        existing at all.
        """
        if not self.has_unsaved_changes():
            return True
        subject = self.subject_field.GetValue().strip()
        described = ('"%s"' % subject) if subject else lang.t('dialogs', 'comp_this_message', default="this message")
        dialog = wx.MessageDialog(
            self,
            lang.t('dialogs', 'comp_unsent_save_q', default="Save %s as a draft before closing?") % described,
            lang.t('dialogs', 'title_unsent_message', default="Unsent Message"),
            wx.YES_NO | wx.CANCEL | wx.ICON_WARNING,
        )
        dialog.SetYesNoCancelLabels(lang.control_label('comp_btn_save_draft', "&Save Draft"), lang.control_label('comp_btn_discard', "&Discard"), lang.control_label('comp_btn_keep_editing', "&Keep editing"))
        try:
            choice = dialog.ShowModal()
        finally:
            dialog.Destroy()

        if choice == wx.ID_CANCEL:
            return False  # Keep editing
        if choice == wx.ID_NO:
            # Discard has to mean gone, including a revision autosave
            # already wrote before this prompt ever appeared -- not
            # "no NEW draft gets saved", which would silently leave an
            # old autosaved copy sitting in Drafts forever.
            self._purge_saved_draft_async()
            return True
        return self._save_draft_blocking()  # choice == wx.ID_YES: Save Draft

    def confirm_for_tray(self):
        """Asked when ZBox goes to the tray with this tab open.
        Returns True when the tab may close.

        Two choices only, Save Draft and Discard. Escape, Alt+F4 and
        closing the prompt all mean Discard, because going to the tray
        is a decision already made. A failed save returns False so the
        tab stays open and nothing typed is lost. No parent window, so
        the prompt still shows while the frame is minimizing.
        """
        if not self.has_unsaved_changes():
            return True
        subject = self.subject_field.GetValue().strip()
        described = ('"%s"' % subject) if subject else lang.t('dialogs', 'comp_this_message', default="this message")
        dialog = wx.MessageDialog(
            None,
            lang.t('dialogs', 'comp_unsent_tray_q', default="Save %s as a draft before ZBox goes to the tray?") % described,
            lang.t('dialogs', 'title_unsent_message', default="Unsent Message"),
            wx.OK | wx.CANCEL | wx.ICON_WARNING | wx.STAY_ON_TOP,
        )
        dialog.SetOKCancelLabels(lang.control_label('comp_btn_save_draft', "&Save Draft"), lang.control_label('comp_btn_discard', "&Discard"))
        try:
            choice = dialog.ShowModal()
        finally:
            dialog.Destroy()
        if choice == wx.ID_OK:
            return self._save_draft_blocking()
        self._purge_saved_draft_async()
        return True

    def _purge_saved_draft_async(self):
        """
        Fire-and-forget cleanup for Discard. Runs after the tab has
        already agreed to close, so failures are logged only -- there
        is no UI left to show them in, and Discard proceeding doesn't
        depend on this succeeding (worst case, same as before this
        method existed: one orphaned draft revision).
        """
        if self._last_draft_id is None:
            return
        account = self._selected_account()
        if account is None or self._last_draft_account_id != account.account_id:
            return
        drafts_folder = self._last_draft_folder or _folder_display_to_himalaya("Drafts", account)
        trash_folder = _folder_display_to_himalaya("Trash", account)
        draft_id = self._last_draft_id
        header = self._draft_message_id.strip("<>") if self._draft_message_id else None

        def work():
            himalaya_client.permanently_delete_message(
                self.main_frame.paths, account, draft_id, drafts_folder,
                trash_folder, message_id_header=header,
            )

        def on_error(exc):
            logger.warning("Could not purge discarded draft %s.", draft_id, exc_info=True)

        self.main_frame.lanes.for_account(account.account_id).submit(
            work, lambda _result: None, on_error,
        )

    def _identity_entries(self):
        """
        Every identity every configured account can send/reply as --
        audit finding 49 -- as (account, display_name, email) triples
        in the same order the From wx.Choice lists them, so a choice
        index maps directly to entries[index]. Recomputed fresh each
        call, same as _selected_account() always did, rather than
        cached: accounts can be added/removed while a compose tab is
        still open.
        """
        return all_identities(self.main_frame.account_manager.accounts)

    def _identity_choices(self):
        entries = self._identity_entries()
        if not entries:
            return [lang.t('dialogs', 'comp_no_account', default="No account configured")]
        return [
            formataddr((name, email)) if name else email
            for _account, name, email in entries
        ]

    def _identity_index_for_email(self, email):
        """Index into _identity_entries()/the From choice whose
        address matches `email` (case-insensitive), or None if there
        is no such identity -- e.g. the account it belonged to was
        since removed."""
        target = email.strip().lower()
        if not target:
            return None
        for index, (_account, _name, candidate) in enumerate(self._identity_entries()):
            if candidate.lower() == target:
                return index
        return None

    def prepare_close(self):
        """Deterministic teardown, called by main_frame._close_tab_at
        immediately before wx.Notebook.DeletePage -- which destroys the
        page from C++ and never routes through Python's own Destroy.
        Stopping the WebView here is what keeps a completed-load event
        fired during shutdown from running script against a half-closed
        controller, which raises a native "Error running JavaScript"
        dialog no Python try/except can suppress. Idempotent, so the
        send path and _on_destroy call it too."""
        self.body.teardown()

    def focus_default(self):
        """Where focus lands when the notebook returns to this tab
        (main_frame._close_tab_at) -- back in the body being written,
        not on whatever control wx would pick first."""
        self.body.focus()

    def _bind_local_accelerators(self, send_button, save_button, attach_button):
        """
        This tab's own chords, for when focus is NOT inside the body.
        Inside it, the page's key router catches the same chords and
        sends the same names back (see app/trix_page.py), so every
        command has one implementation whichever side it came from.

        A local accelerator table gets first look at a keystroke,
        ahead of the frame's, which is what lets this tab claim three
        chords the frame owns elsewhere: Ctrl+S is Save Draft rather
        than Save Message As, as it already was; Ctrl+B is Bold rather
        than Switch Between Text and HTML View; Ctrl+Shift+P is
        Preview rather than Print Preview. None of those frame
        commands means anything in a compose tab, and claiming them
        makes each chord mean one thing throughout this tab instead of
        two depending on where focus sits.

        Ctrl+Shift+Q is deliberately NOT claimed. It is Exit, quitting
        has to work from everywhere, and that is exactly why Quote
        moved off it to Ctrl+Shift+9.
        """
        entries = []
        for flags, key, handler in (
            (wx.ACCEL_CTRL, wx.WXK_RETURN, self._on_send),
            (wx.ACCEL_CTRL, wx.WXK_NUMPAD_ENTER, self._on_send),
            # Alt+S, added 2026-09-06 at the user's request as a
            # second Send chord alongside Ctrl+Enter -- familiar to
            # anyone coming from Outlook, where Alt+S sends.
            # Ctrl+Enter stays the one shown on the Send button.
            (wx.ACCEL_ALT, ord("S"), self._on_send),
            (wx.ACCEL_CTRL, ord("S"), self._on_save_draft),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("A"), self._on_attach),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("S"),
             lambda _e: self.cmd_insert_signature()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("W"),
             lambda _e: self.cmd_word_count()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("V"),
             lambda _e: self.cmd_paste_quotation()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("P"),
             lambda _e: self.cmd_preview()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F7,
             lambda _e: self.cmd_spellcheck()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F9,
             lambda _e: self.cmd_spellcheck_next()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F8,
             lambda _e: self.cmd_spellcheck_add()),
            # The hotkeys diagnostic. On the panel's own table on
            # purpose, so it is reachable from the header fields when
            # the body's key router is the thing that has stopped
            # working. The still report is on the Compose menu.
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F12,
             lambda _e: self.cmd_hotkeys_diagnostic()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("K"),
             lambda _e: self.cmd_formatting()),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("M"),
             lambda _e: self.cmd_format_menu()),
            (wx.ACCEL_CTRL, ord("K"), lambda _e: self.cmd_link()),
            (wx.ACCEL_CTRL, ord("B"), lambda _e: self.cmd_format("bold")),
            (wx.ACCEL_CTRL, ord("I"), lambda _e: self.cmd_format("italic")),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("X"),
             lambda _e: self.cmd_format("strike")),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("8"),
             lambda _e: self.cmd_format("bullet")),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("7"),
             lambda _e: self.cmd_format("number")),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("9"),
             lambda _e: self.cmd_format("quote")),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("6"),
             lambda _e: self.cmd_format("code")),
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord("1"),
             lambda _e: self.cmd_format("heading1")),
        ):
            command_id = wx.NewIdRef()
            self.Bind(wx.EVT_MENU, handler, id=command_id)
            entries.append((flags, key, command_id))
        self.SetAcceleratorTable(wx.AcceleratorTable(entries))

    # --- commands: one implementation, three ways in ------------------

    def _run_chord(self, name):
        """Everything the page's key router hands back. An fmt: prefix
        is formatting; the rest are the same methods the accelerator
        table and the Format menu call.

        Every one of them is scheduled rather than called. This
        function runs inside the WebView's own script-message event,
        with the control's C++ callback still on the stack, and some
        of these commands destroy that control: close_tab deletes the
        page, and so does send once it succeeds. Calling them here
        deletes the object underneath the code still executing in it,
        which is a hard crash rather than an exception. CallAfter puts
        every command one turn of the event loop later, after the
        callback has returned. One rule for all of them rather than a
        list of exceptions, which also covers the two that open a
        modal from inside the same callback: the Format menu and the
        link dialog.
        """
        if name.startswith("fmt:"):
            wx.CallAfter(self.cmd_format, name[4:])
            return
        handler = {
            "send": lambda: self._on_send(None),
            "save_draft": lambda: self._on_save_draft(None),
            "attach": lambda: self._on_attach(None),
            "insert_signature": self.cmd_insert_signature,
            "word_count": self.cmd_word_count,
            "paste_quotation": self.cmd_paste_quotation,
            "spellcheck": self.cmd_spellcheck,
            "spellcheck_next": self.cmd_spellcheck_next,
            "spellcheck_add": self.cmd_spellcheck_add,
            "hotkeys_diagnostic": self.cmd_hotkeys_diagnostic,
            "preview": self.cmd_preview,
            "formatting": self.cmd_formatting,
            "format_menu": self.cmd_format_menu,
            "link": self.cmd_link,
            "pgp_encrypt": lambda: self.cmd_toggle_pgp("encrypt"),
            "pgp_sign": lambda: self.cmd_toggle_pgp("sign"),
            "pgp_attach_key": lambda: self.cmd_toggle_pgp("attach_key"),
            # The way out. Without these four the body is a keyboard
            # trap, which is the bug the reading view already had to
            # fix once -- see app/trix_page.py's chord table.
            "cycle_panes": lambda: self.main_frame._on_cycle_panes(None),
            "close_tab": self.cmd_close_tab,
            "next_tab": self.main_frame._next_tab,
            "previous_tab": self.main_frame._previous_tab,
            # Quit works everywhere else in ZBox, so it works here.
            # Routed through the frame's own close, which is what runs
            # the unsaved-changes prompt for this tab and every other
            # one -- quitting from inside a half-written message must
            # not be the one path that skips it.
            "quit": lambda: self.main_frame.Close(),
        }.get(name)
        if handler is not None:
            wx.CallAfter(handler)

    def cmd_close_tab(self):
        """Escape, Ctrl+W or Ctrl+F4 from inside the body. Goes through
        the frame so the unsaved-changes prompt, the teardown hook and
        where focus lands afterwards are all the ones every other close
        route already uses."""
        index = self.main_frame.notebook.FindPage(self)
        if index != wx.NOT_FOUND:
            self.main_frame._close_tab_at(index)

    def cmd_format(self, attribute):
        spoken = self.body.apply_format(attribute)
        if spoken:
            self._say(spoken)

    def cmd_formatting(self):
        """Ctrl+Shift+K: what formatting is in force where the caret
        is. Nothing announces bold to a screen reader on its own -- a
        sighted writer sees the text thicken, and that is the whole
        feedback loop being replaced here."""
        spoken = self.body.describe_formatting()
        if spoken:
            self._say(spoken)

    def cmd_format_menu(self):
        """The Format menu, opened wherever focus is, body included.
        Built fresh each time rather than borrowed from a menu bar,
        and focus goes back to the body afterwards: opening a menu is
        not a reason to leave the place you were writing."""
        menu = wx.Menu()
        for label, attribute, accelerator in trix_page.FORMAT_COMMANDS:
            item = menu.Append(
                wx.ID_ANY,
                lang.menu_label(
                    "format_" + attribute, "%s\t%s" % (label, accelerator)
                ),
            )
            self.Bind(wx.EVT_MENU, lambda _e, a=attribute: self.cmd_format(a), item)
        menu.AppendSeparator()
        link_item = menu.Append(
            wx.ID_ANY,
            lang.menu_label("format_insert_link", "Insert &link...\tCtrl+K"),
        )
        self.Bind(wx.EVT_MENU, lambda _e: self.cmd_link(), link_item)
        here_item = menu.Append(
            wx.ID_ANY,
            lang.menu_label(
                "format_formatting_here", "Formatting &here\tCtrl+Shift+K"
            ),
        )
        self.Bind(wx.EVT_MENU, lambda _e: self.cmd_formatting(), here_item)
        self.PopupMenu(menu)
        menu.Destroy()
        wx.CallAfter(self.body.focus)

    def cmd_link(self):
        with wx.TextEntryDialog(
            self,
            lang.t("dialogs", "comp_link_prompt", default="Link address:"),
            lang.t("dialogs", "title_insert_link", default="Insert Link"),
            "https://",
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            url = dialog.GetValue().strip()
        if not url:
            return
        spoken = self.body.apply_link(url)
        if spoken:
            self._say(spoken)

    # --- OpenPGP (stage 3) ---------------------------------------------

    def _pgp_boxes(self):
        return {"encrypt": self.encrypt_box, "sign": self.sign_box, "attach_key": self.attach_key_box}

    def pgp_state(self):
        """(Encrypt, Digitally Sign, Attach My Public Key), for ticking the
        Compose menu."""
        return (self.encrypt_box.GetValue(), self.sign_box.GetValue(), self.attach_key_box.GetValue())

    def _pgp_settings(self, entry=None):
        entry = entry if entry is not None else self._selected_identity_entry()
        if entry is None:
            return openpgp_compose.settings_for(None, "")
        return openpgp_compose.settings_for(entry[0], entry[2])

    def _apply_pgp_defaults(self, entry):
        """Sets the boxes the writer has not changed from the identity's
        settings, as Thunderbird does: Encrypt from encryption for new
        messages (always on for a reply to encrypted mail), Digitally Sign
        whenever Encrypt is on or unencrypted messages are signed, Attach My
        Public Key whenever the message is signed. True when one changed."""
        settings = self._pgp_settings(entry)
        if str(settings.get("key") or "") == "none" and not self._pgp_reply_encrypted:
            wanted = {"encrypt": False, "sign": False, "attach_key": False}
        else:
            encrypt = bool(settings.get("encrypt")) or self._pgp_reply_encrypted
            sign = encrypt or bool(settings.get("sign"))
            wanted = {"encrypt": encrypt, "sign": sign, "attach_key": sign}
        changed = False
        for name, box in self._pgp_boxes().items():
            if name in self._pgp_touched or box.GetValue() == wanted[name]:
                continue
            box.SetValue(wanted[name])
            changed = True
        return changed

    def _follow_pgp_boxes(self):
        """Digitally Sign follows Encrypt, and Attach My Public Key follows
        Digitally Sign, until each is changed by hand. Returns what changed,
        in words, to be said."""
        spoken = []
        if "sign" not in self._pgp_touched:
            value = self.encrypt_box.GetValue() or bool(self._pgp_settings().get("sign"))
            if self.sign_box.GetValue() != value:
                self.sign_box.SetValue(value)
                spoken.append(self._pgp_state_words(self.sign_box))
        if "attach_key" not in self._pgp_touched:
            value = self.sign_box.GetValue()
            if self.attach_key_box.GetValue() != value:
                self.attach_key_box.SetValue(value)
                spoken.append(self._pgp_state_words(self.attach_key_box))
        return spoken

    def _pgp_state_words(self, box):
        if box.GetValue():
            return lang.t("actions_announcements", "pgp_cm_on", default="{name} on.", name=box.GetLabel())
        return lang.t("actions_announcements", "pgp_cm_off", default="{name} off.", name=box.GetLabel())

    def _on_pgp_box(self, event):
        """A box changed by hand. The box says its own state; any box that
        follows it is said after."""
        for name, box in self._pgp_boxes().items():
            if box is event.GetEventObject():
                self._pgp_touched.add(name)
        spoken = self._follow_pgp_boxes()
        if spoken:
            self._say(" ".join(spoken))
        event.Skip()

    def cmd_toggle_pgp(self, name):
        """Compose > Encrypt, Digitally Sign or Attach My Public Key, which
        work from inside the body too. The new state is said, with any box
        that follows it."""
        box = self._pgp_boxes().get(name)
        if box is None:
            return
        box.SetValue(not box.GetValue())
        self._pgp_touched.add(name)
        self._say(" ".join([self._pgp_state_words(box)] + self._follow_pgp_boxes()))

    def _apply_identity_copies(self, entry):
        """Puts the identity's Always Cc and Always Bcc addresses into
        the Cc and Bcc fields, taking out those the previous identity
        added where they are still there unchanged. True when a field
        changed. Silent; _on_from_changed says so."""
        account, _name, email = entry
        extra = _identity_extra(account, email)
        new = (extra.get("always_cc", ""), extra.get("always_bcc", ""))
        old = self._auto_copies
        changed = False
        for field, before, after in (
            (self.cc_field, old[0], new[0]),
            (self.bcc_field, old[1], new[1]),
        ):
            if before == after:
                continue
            value = field.GetValue()
            updated = _swap_addresses(value, before, after)
            if updated != value.strip():
                field.SetValue(updated)
                changed = True
        self._auto_copies = new
        return changed

    def _identity_drafts_folder(self, account, email):
        """The identity's own Drafts folder, or its account's."""
        own = _identity_extra(account, email).get("drafts_folder")
        return own or _folder_display_to_himalaya("Drafts", account)

    def _on_from_changed(self, event):
        """From now names another identity: its Always Cc and Bcc, and
        its signature, should be the ones in the message. The signature
        is the identity's own when it has one, else its account's. It
        is rebuilt while the body is untouched. Either way it is said
        out loud, in one announcement with any Cc or Bcc change,
        because both are invisible to a screen reader user until the
        message has gone."""
        event.Skip()
        entry = self._selected_identity_entry()
        if entry is None:
            return
        account, _name, email = entry
        spoken = []
        if self._apply_identity_copies(entry):
            spoken.append(lang.t(
                "actions_announcements", "identity_copies_updated",
                default="Cc and Bcc updated for this identity.",
            ))
        if self._apply_pgp_defaults(entry):
            spoken.append(lang.t(
                "actions_announcements", "pgp_cm_identity",
                default="Encryption settings changed to this identity's.",
            ))
        plain, formatted, own = _identity_signature(account, email)
        new = (plain, formatted)
        if new == self._signature_pair:
            self._signature_account_id = account.account_id
        elif self.body.is_pristine():
            self.body.set_html(_initial_body_html(self._opening_body, plain, formatted))
            self._signature_pair = new
            self._signature_account_id = account.account_id
            if plain or formatted:
                if own:
                    spoken.append(lang.t(
                        "actions_announcements", "signature_switched_identity",
                        default="Signature changed to this identity's.",
                    ))
                else:
                    spoken.append(lang.t(
                        "actions_announcements", "signature_switched",
                        default="Signature changed to this account's.",
                    ))
            elif own:
                spoken.append(lang.t(
                    "actions_announcements", "signature_removed_identity",
                    default="Signature removed. This identity has none.",
                ))
            else:
                spoken.append(lang.t(
                    "actions_announcements", "signature_removed_no_signature",
                    default="Signature removed. This account has none.",
                ))
        elif own:
            spoken.append(lang.t(
                "actions_announcements", "signature_not_switched_identity",
                default="Signature not changed. Press Ctrl+Shift+S to insert "
                        "this identity's signature.",
            ))
        else:
            spoken.append(lang.t(
                "actions_announcements", "signature_not_switched",
                default="Signature not changed. Press Ctrl+Shift+S to insert "
                        "this account's signature.",
            ))
        if spoken:
            self._say(" ".join(spoken))

    def cmd_insert_signature(self):
        entry = self._selected_identity_entry()
        if entry is not None:
            signature, formatted, own = _identity_signature(entry[0], entry[2])
        else:
            signature, formatted, own = "", "", False
        if not signature and not formatted:
            if own:
                self._say(lang.t(
                    "actions_announcements", "signature_none_identity",
                    default="This identity has no signature.",
                ))
            else:
                self._say(lang.t(
                    "actions_announcements", "signature_none",
                    default="This account has no signature.",
                ))
            return
        self.body.insert_html(_signature_html(signature, formatted))
        self._say(lang.t(
            "actions_announcements", "signature_inserted",
            default="Signature inserted.",
        ))

    def cmd_word_count(self):
        text = self.body.get_text()
        words = len([word for word in text.split() if word.strip()])
        self._say(lang.t(
            "actions_announcements", "word_count",
            default="{words} words, {characters} characters.",
            words=words, characters=len(text),
        ))

    def cmd_paste_quotation(self):
        text = ""
        if wx.TheClipboard.Open():
            try:
                data = wx.TextDataObject()
                if wx.TheClipboard.GetData(data):
                    text = data.GetText()
            finally:
                wx.TheClipboard.Close()
        if not text:
            self._say(lang.t(
                "actions_announcements", "clipboard_empty",
                default="There is nothing on the clipboard.",
            ))
            return
        quoted = "\n".join(
            ("> " + line) if line else ">" for line in text.splitlines()
        )
        self.body.insert_text(quoted + "\n")
        self._say(lang.t(
            "actions_announcements", "pasted_as_quotation",
            default="Pasted as a quotation.",
        ))

    def cmd_spellcheck(self):
        """Ctrl+Shift+F7: the spell check dialog, on every engine.

        It walks the misspellings one at a time, says each word and the
        line it sits in, and offers suggestions in an arrowable list
        where Down moves and Enter replaces.

        Corrections come back as a list of ranges and are applied to
        the body in a single batch, so the formatting around each one
        survives and the offsets cannot drift between them.
        """
        self.body.request_document(self._run_spellcheck_dialog)

    def _run_spellcheck_dialog(self):
        import spellcheck_dialog

        text = self.body.document_text
        if not text.strip():
            self._say(lang.t(
                "actions_announcements", "nothing_to_check",
                default="There is nothing to check.",
            ))
            return
        personal = spellcheck.personal_path_for(self.main_frame.paths.userdata)
        _corrected, changes = spellcheck_dialog.check_text(self, text, personal)
        # Focus first, then speak. The writer is back in the body at
        # the moment the dialog goes, and the count arrives while they
        # are already there rather than holding the return up.
        self.body.focus()
        if changes:
            applied = self.body.replace_ranges(changes)
            if applied == len(changes):
                if applied == 1:
                    self._say(lang.t(
                        "actions_announcements", "corrections_applied_one",
                        default="1 correction applied.",
                    ))
                else:
                    self._say(lang.t(
                        "actions_announcements", "corrections_applied_many",
                        default="{count} corrections applied.", count=applied,
                    ))
            else:
                # Said plainly rather than rounded up. A message that
                # reports every correction and delivers some of them
                # is worse than one that admits it: the writer sends
                # the half-corrected version believing it is clean.
                self._say(lang.t(
                    "actions_announcements", "corrections_applied_partly",
                    default="{applied} of {total} corrections applied. "
                            "The rest were refused; see the log.",
                    applied=applied, total=len(changes),
                ))

    def cmd_spellcheck_next(self):
        """Move the caret to the next misspelling and say it, without
        a dialog.

        Kept alongside the dialog rather than instead of it. In the
        Trix body this puts the caret on the word so the Application
        key gives Chromium's own suggestions, which are instant; the
        dialog is the one to reach for when you want to work through a
        whole message rather than fix one word in passing.
        """
        self.body.request_document(self._goto_next_misspelling)

    def _goto_next_misspelling(self):
        text = self.body.document_text
        if not text.strip():
            self._say(lang.t(
                "actions_announcements", "nothing_to_check",
                default="There is nothing to check.",
            ))
            return
        dictionary = spellcheck.get_dictionary(
            spellcheck.personal_path_for(self.main_frame.paths.userdata)
        )
        if not dictionary.loaded:
            self._say(lang.t(
                "actions_announcements", "spelling_dictionary_unavailable",
                default="The spelling dictionary is not available.",
            ))
            return
        found = spellcheck.next_misspelling(text, dictionary, self._spell_position)
        if found is None and self._spell_position:
            # Wrap once, so a second pass starts from the top rather
            # than reporting nothing left when there is plenty above.
            found = spellcheck.next_misspelling(text, dictionary, 0)
        if found is None:
            self._spell_position = 0
            self._say(lang.t(
                "actions_announcements", "no_misspellings",
                default="No misspellings.",
            ))
            return
        start, end, word = found
        self._spell_word = word
        self._spell_position = end
        self.body.select_range(start, end)
        self._say(word)

    def cmd_spellcheck_add(self):
        """Ctrl+Shift+F8: keep the word F7 just stopped on.

        Separate from Chromium's own Add to Dictionary, which writes
        into the WebView2 user data folder and cannot be read from
        here. Adding it there stops the squiggle; adding it here stops
        F7 walking you back to it. Both are worth having and pretending
        they are one list would only mislead.
        """
        if not self._spell_word:
            self._say(lang.t(
                "actions_announcements", "next_misspelling_first",
                default="Use next misspelling first.",
            ))
            return
        dictionary = spellcheck.get_dictionary(
            spellcheck.personal_path_for(self.main_frame.paths.userdata)
        )
        if dictionary.add_personal(self._spell_word):
            self.body.refresh_marks()
            self._say(lang.t(
                "actions_announcements", "word_added",
                default="{word} added to the dictionary.",
                word=self._spell_word,
            ))
        else:
            self._say(lang.t(
                "actions_announcements", "word_not_added",
                default="{word} could not be added.",
                word=self._spell_word,
            ))
        self._spell_word = ""

    def _on_spell_menu(self, start, end):
        """The Applications key, Shift+F10 or a right click on a
        marked word. Reads the document afresh first, so the range is
        checked against what is really there."""
        self.body.request_document(lambda: self._show_spell_menu(start, end))

    def _show_spell_menu(self, start, end):
        """ZBox's own suggestions for one misspelled word, in a native
        menu: up to seven suggestions, then Add to dictionary. Chromium's
        menu used to supply these; its spell check is off in the editor
        now, so it has none to give."""
        text = self.body.document_text
        word = text[start:end] if 0 <= start < end <= len(text) else ""
        if not word.strip():
            return
        dictionary = spellcheck.get_dictionary(
            spellcheck.personal_path_for(self.main_frame.paths.userdata)
        )
        if not dictionary.loaded or dictionary.is_known(word):
            self.body.refresh_marks()
            return
        menu = wx.Menu()
        suggestions = dictionary.suggest(word)
        for suggestion in suggestions:
            item = menu.Append(wx.ID_ANY, suggestion)
            self.Bind(
                wx.EVT_MENU,
                lambda _e, s=suggestion: self._replace_misspelling(start, end, s),
                item,
            )
        if not suggestions:
            empty = menu.Append(
                wx.ID_ANY,
                lang.t("dialogs", "spell_no_suggestions", default="No suggestions"),
            )
            empty.Enable(False)
        menu.AppendSeparator()
        add_item = menu.Append(
            wx.ID_ANY,
            lang.t(
                "dialogs", "spell_add_to_dictionary",
                default="&Add to dictionary",
            ),
        )
        self.Bind(
            wx.EVT_MENU, lambda _e: self._add_misspelling(word), add_item,
        )
        try:
            position = self.body.control.GetPosition()
        except (AttributeError, RuntimeError):
            position = wx.DefaultPosition
        self.PopupMenu(menu, position)
        menu.Destroy()
        self.body.focus()

    def _replace_misspelling(self, start, end, replacement):
        applied = self.body.replace_ranges([(start, end, replacement)])
        if applied:
            self._say(lang.t(
                "actions_announcements", "spell_replaced_with",
                default="Replaced with {word}.", word=replacement,
            ))
        else:
            self._say(lang.t(
                "actions_announcements", "spell_not_replaced",
                default="The word could not be replaced.",
            ))

    def _add_misspelling(self, word):
        dictionary = spellcheck.get_dictionary(
            spellcheck.personal_path_for(self.main_frame.paths.userdata)
        )
        if dictionary.add_personal(word):
            self.body.refresh_marks()
            self._say(lang.t(
                "actions_announcements", "word_added",
                default="{word} added to the dictionary.", word=word,
            ))
        else:
            self._say(lang.t(
                "actions_announcements", "word_not_added",
                default="{word} could not be added.", word=word,
            ))

    def _maybe_auto_diagnose(self):
        """Dump the composer diagnostics to a file, unattended.

        Off unless ZBOX_COMPOSE_DIAGNOSTICS names a path, so nothing
        here runs for a normal user. It exists because the interesting
        failure is a composer whose keyboard does not work, and asking
        somebody to drive a dialog with a keyboard that does not work
        is not a diagnosis, it is a joke.

        The wait is for the page: a report taken before the document
        has loaded says nothing except that the document has not
        loaded.
        """
        import os

        path = os.environ.get("ZBOX_COMPOSE_DIAGNOSTICS")
        if not path:
            return

        def dump():
            lines = [
                "ZBox composer diagnostics, taken automatically "
                "%.1f seconds after the tab opened." % 2.5,
                "",
            ]
            try:
                lines.extend(self._diagnostic_lines())
            except Exception as exc:
                lines.append("The report itself failed: %r" % (exc,))
            for line in lines:
                logger.info("compose diagnostics | %s", line)
            try:
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write("\n".join(lines) + "\n")
                logger.info("compose diagnostics written to %s", path)
            except OSError as exc:
                logger.warning("compose diagnostics could not be written: %r", exc)

        wx.CallLater(2500, dump)

    def _on_hotkey_watch(self, event):
        """One line per key combination pressed anywhere in this tab
        outside the body, then the key is always passed on.

        Combinations only. A plain letter typed into a header field is
        message content and never reaches the log, which is the same
        rule the page's own router follows.

        Everything here sits inside a guard and ends in Skip. A
        diagnostic that could swallow a keystroke would be worse than
        the fault it exists to find.
        """
        try:
            if self.main_frame.hotkeys_diagnostic:
                described = self._describe_key(event)
                if described:
                    logger.info(
                        "hotkeys | %s | %s", self._focused_name(), described
                    )
        except Exception:
            pass
        # The address suggestion keys, the only keys this hook ever keeps.
        if self._suggestion_key(event):
            return
        event.Skip()

    # --- Address suggestions ---------------------------------------------

    def _address_fields(self):
        return (self.to_field, self.cc_field, self.bcc_field)

    def _suggestion_key(self, event):
        """Down Arrow in To, Cc or Bcc opens the suggestions; in the list,
        Enter and Tab put the chosen one in the field and Escape goes back
        with nothing changed. True when the key was used here."""
        key = event.GetKeyCode()
        plain = not (event.ControlDown() or event.AltDown() or event.ShiftDown() or event.MetaDown())
        focus = wx.Window.FindFocus()
        if focus is self.suggestion_list and self.suggestion_list.IsShown():
            if plain and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_TAB):
                self._pick_suggestion()
                return True
            if key == wx.WXK_BACK and not (event.ControlDown() or event.AltDown()):
                self._backspace_from_suggestions()
                return True
            if (key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE) and event.ShiftDown()
                    and not (event.ControlDown() or event.AltDown())):
                self._remove_suggested_contact()
                return True
            if key == wx.WXK_ESCAPE:
                self._close_suggestions(back_to_field=True)
                return True
            return False
        if plain and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN) and focus in self._address_fields():
            return self._open_suggestions(focus)
        return False

    def _open_suggestions(self, field):
        """Shows the suggestions for what is typed in field, most used
        first, with focus on the first one. False, and nothing shown, when
        there are none: Down Arrow then does what it always did."""
        head, entries = _suggestion_entries(self.main_frame.contacts, field.GetValue())
        if not entries:
            return False
        self._suggest_field = field
        self._suggest_head = head
        self._suggest_values = [value for value, _email in entries]
        self._suggest_emails = [email for _value, email in entries]
        self.suggestion_list.Set(self._suggest_values)
        self.suggestion_list.SetSelection(0)
        self.suggestion_label.Show()
        self.suggestion_list.Show()
        self.Layout()
        self.suggestion_list.SetFocus()
        return True

    def _pick_suggestion(self):
        """The selected suggestion goes into its field after what was there
        before the last comma, followed by a comma and a space for the next
        address, and focus returns to the end of the field."""
        index = self.suggestion_list.GetSelection()
        field = self._suggest_field
        if field is not None and index != wx.NOT_FOUND and index < len(self._suggest_values):
            field.ChangeValue(self._suggest_head + self._suggest_values[index] + ", ")
        self._close_suggestions(back_to_field=True)

    def _close_suggestions(self, back_to_field):
        """Hides the list. Focus goes back to the field first, so hiding the
        focused list never hands focus to whatever control comes next."""
        field = self._suggest_field
        self._suggest_field = None
        self._suggest_values = []
        self._suggest_emails = []
        if back_to_field and field is not None:
            field.SetFocus()
            field.SetInsertionPointEnd()
        if self.suggestion_list.IsShown():
            self.suggestion_label.Hide()
            self.suggestion_list.Hide()
            self.Layout()

    def _backspace_from_suggestions(self):
        """Backspace in the list: the list closes and the field loses its
        last character, as if Backspace had been pressed there. Held down,
        the repeats then reach the field itself and go on deleting."""
        field = self._suggest_field
        self._close_suggestions(back_to_field=True)
        if field is None:
            return
        end = field.GetLastPosition()
        if end > 0:
            field.Remove(end - 1, end)
            field.SetInsertionPointEnd()

    def _remove_suggested_contact(self):
        """Shift+Delete in the list: the selected address leaves the
        Address Book, and so the suggestions. Focus stays in the list on
        the next suggestion, or goes back to the field when none is left.
        A group's entry is not one address and is left alone."""
        index = self.suggestion_list.GetSelection()
        field = self._suggest_field
        if field is None or index == wx.NOT_FOUND or index >= len(self._suggest_emails):
            return
        email = self._suggest_emails[index]
        if not email:
            return
        self.main_frame.contacts.remove(email)
        head, entries = _suggestion_entries(self.main_frame.contacts, field.GetValue())
        if not entries:
            self._close_suggestions(back_to_field=True)
            return
        self._suggest_head = head
        self._suggest_values = [value for value, _email in entries]
        self._suggest_emails = [address for _value, address in entries]
        self.suggestion_list.Set(self._suggest_values)
        self.suggestion_list.SetSelection(min(index, len(self._suggest_values) - 1))

    def _on_suggestions_left(self, event):
        # Shift+Tab or a click elsewhere: the list goes away once focus
        # has settled somewhere else.
        event.Skip()
        wx.CallAfter(self._hide_if_left)

    def _hide_if_left(self):
        try:
            if wx.Window.FindFocus() is not self.suggestion_list:
                self._close_suggestions(back_to_field=False)
        except RuntimeError:  # the tab closed meanwhile
            pass

    def _describe_key(self, event):
        """The combination as text, or an empty string when the key is
        ordinary typing and must not be logged."""
        code = event.GetKeyCode()
        ctrl = event.ControlDown()
        alt = event.AltDown()
        shift = event.ShiftDown()
        function_key = wx.WXK_F1 <= code <= wx.WXK_F24
        if not (ctrl or alt or function_key or code == wx.WXK_ESCAPE):
            return ""
        names = {
            wx.WXK_ESCAPE: "Escape",
            wx.WXK_TAB: "Tab",
            wx.WXK_RETURN: "Enter",
            wx.WXK_NUMPAD_ENTER: "NumpadEnter",
            wx.WXK_SPACE: "Space",
            wx.WXK_BACK: "Backspace",
            wx.WXK_DELETE: "Delete",
            wx.WXK_UP: "Up",
            wx.WXK_DOWN: "Down",
            wx.WXK_LEFT: "Left",
            wx.WXK_RIGHT: "Right",
            wx.WXK_HOME: "Home",
            wx.WXK_END: "End",
            wx.WXK_PAGEUP: "PageUp",
            wx.WXK_PAGEDOWN: "PageDown",
        }
        if function_key:
            name = "F%d" % (code - wx.WXK_F1 + 1)
        elif code in names:
            name = names[code]
        elif 32 < code < 127:
            name = chr(code).upper()
        else:
            name = "key %d" % code
        parts = []
        if ctrl:
            parts.append("Ctrl")
        if shift:
            parts.append("Shift")
        if alt:
            parts.append("Alt")
        parts.append(name)
        return "+".join(parts)

    def _focused_name(self):
        """Which control had focus, by the name a screen reader reads,
        so a line says where the key was pressed as well as what it
        was."""
        try:
            window = wx.Window.FindFocus()
        except RuntimeError:
            return "unknown"
        if window is None:
            return "unknown"
        try:
            return window.GetName() or "unnamed"
        except RuntimeError:
            return "unknown"

    def cmd_hotkeys_diagnostic(self):
        """Ctrl+Shift+F12. One switch for the whole application, held
        by the frame, so two compose tabs can never disagree about
        whether the diagnostic is on."""
        self.main_frame.toggle_hotkeys_diagnostic()

    def cmd_diagnostics(self):
        """Compose > Composer Diagnostics: what the body actually is
        right now.

        Everything in here is read back through RunScript rather than
        through the message bridge, because the bridge is the usual
        suspect when the composer stops responding and a report that
        travels through the broken channel says nothing at all.

        This is the still picture. The hotkeys diagnostic beside it is
        Ctrl+Shift+F12 and writes a line per combination to the log as
        it happens.
        """
        dialog = wx.Dialog(
            self,
            title=lang.t(
                "dialogs", "title_composer_diagnostics",
                default="Composer Diagnostics",
            ),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(
            wx.StaticText(
                dialog,
                label=lang.control_label("comp_report_label", "&Report:"),
            ),
            0, wx.ALL, 8,
        )
        report = wx.TextCtrl(
            dialog, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP
        )
        report.SetName(lang.t("dialogs", "comp_report_name", default="Report"))
        make_read_only_viewer(report)
        sizer.Add(report, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        refresh = wx.Button(
            dialog, label=lang.control_label("comp_refresh", "Re&fresh")
        )
        copy = wx.Button(dialog, label=lang.control_label("comp_copy", "C&opy"))
        close = wx.Button(
            dialog, wx.ID_CLOSE, label=lang.control_label("btn_close", "&Close")
        )
        for button in (refresh, copy, close):
            buttons.Add(button, 0, wx.RIGHT, 6)
        sizer.Add(buttons, 0, wx.ALL, 8)

        def fill(lines):
            report.SetValue("\n".join(lines))
            report.SetInsertionPoint(0)

        def header():
            """Read from the frame, which owns the one switch."""
            return [
                "HOTKEYS DIAGNOSTIC IS %s" % (
                    "ON" if self.main_frame.hotkeys_diagnostic else "OFF"
                ),
                "Ctrl+Shift+F12 turns it on and off. Every combination",
                "pressed in a compose tab goes to the log while it is on.",
                "",
            ]

        def refill(_event=None):
            fill(header() + self._diagnostic_lines())

        def to_clipboard(_event):
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(report.GetValue()))
                finally:
                    wx.TheClipboard.Close()
                self._say(lang.t(
                    "actions_announcements", "report_copied",
                    default="Report copied.",
                ))

        refresh.Bind(wx.EVT_BUTTON, refill)
        copy.Bind(wx.EVT_BUTTON, to_clipboard)
        close.Bind(wx.EVT_BUTTON, lambda _e: dialog.EndModal(wx.ID_OK))
        dialog.SetEscapeId(wx.ID_CLOSE)

        dialog.SetSizerAndFit(sizer)
        dialog.SetSize((760, 620))
        refill()
        report.SetFocus()
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _diagnostic_lines(self):
        lines = [
            "Body engine: %s" % (
                "Trix in a WebView" if self.body.rich else "markup, plain edit box"
            ),
            "",
        ]
        lines.extend(self.body.diagnostics())
        lines.append("")
        lines.append("Chords this tab claims in wx, for the header fields:")
        lines.append(
            "  Ctrl+Enter and Alt+S send, Ctrl+S saves a draft, "
            "Ctrl+Shift+A attaches,"
        )
        lines.append(
            "  Ctrl+Shift+F7 next misspelling, Ctrl+Shift+F8 adds the word,"
        )
        lines.append(
            "  Ctrl+Shift+F12 is the hotkeys diagnostic, and this report"
        )
        lines.append(
            "  is on the Compose menu. Everything else is the page's."
        )
        return lines

    def cmd_preview(self):
        """The message as the recipient gets it: what a reader would
        say, and the HTML that will actually be sent. Flushes first, so
        it shows what is there rather than what was there."""
        self.body.flush(self._show_preview)

    def _show_preview(self):
        html_body = self.body.get_html()
        reading = (
            body_html.html_to_text(html_body) if html_body else self.body.get_text()
        )
        dialog = wx.Dialog(
            self,
            title=lang.t(
                "dialogs", "title_formatted_preview",
                default="Formatted Preview",
            ),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(
            wx.StaticText(
                dialog,
                label=lang.t(
                    "dialogs", "comp_preview_read",
                    default="As the recipient reads it",
                ),
            ),
            0, wx.ALL, 8
        )
        read_field = wx.TextCtrl(
            dialog, value=reading, style=wx.TE_MULTILINE | wx.TE_READONLY
        )
        read_field.SetName(
            lang.t(
                "dialogs", "comp_preview_read",
                default="As the recipient reads it",
            )
        )
        make_read_only_viewer(read_field)
        sizer.Add(read_field, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(
            wx.StaticText(
                dialog,
                label=lang.t(
                    "dialogs", "comp_preview_source",
                    default="The HTML that will be sent",
                ),
            ),
            0, wx.ALL, 8
        )
        source_field = wx.TextCtrl(
            dialog, value=html_body or "(no HTML part: this message is plain text)",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        source_field.SetName(
            lang.t(
                "dialogs", "comp_preview_source",
                default="The HTML that will be sent",
            )
        )
        make_read_only_viewer(source_field)
        sizer.Add(source_field, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(dialog.CreateStdDialogButtonSizer(wx.OK), 0, wx.EXPAND | wx.ALL, 8)
        dialog.SetSizerAndFit(sizer)
        dialog.SetSize((680, 600))
        read_field.SetFocus()
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        self.body.focus()

    def _say(self, text):
        """Said straight to whichever screen reader is running, with no
        focus change and no dialog -- announce.speak, which is what
        accessible-output2 is already a dependency for. The status bar
        alone is not enough: readers only speak it when status bar
        reporting is switched on, and it is off by default."""
        if not text:
            return
        try:
            self.main_frame.GetStatusBar().SetStatusText(text)
        except RuntimeError:
            pass
        speak(self, text)

    def _open_link(self, url):
        """A link clicked inside the body. mailto stays in ZBox, which
        is a mail client and should not hand a request to write mail to
        whatever else the system registered; anything else goes to the
        browser. Either way this tab never navigates away from what is
        being written."""
        if not url:
            return
        if url[:7].lower() == "mailto:":
            self.main_frame.open_compose_from_mailto(url)
            return
        logger.debug("Opening a link from the compose body: %s", url)
        wx.LaunchDefaultBrowser(url)

    def _selected_identity_entry(self):
        """The (account, display_name, email) triple for whatever is
        currently selected in From, or None (no account configured,
        or the selection is stale after an account was removed)."""
        entries = self._identity_entries()
        index = self.from_choice.GetSelection()
        if index == wx.NOT_FOUND or index >= len(entries):
            return None
        return entries[index]

    def _selected_account(self):
        entry = self._selected_identity_entry()
        return entry[0] if entry else None

    def _build_message(self, account, display_name, email):
        """
        Builds the RFC822 message from the compose fields, minus a
        Message-ID (Send and Save Draft each set their own -- see
        callers). Shared by Send and Save Draft/autosave so headers
        never drift between what would have been sent and what gets
        saved as a draft of it.
        """
        message = EmailMessage()
        message["From"] = formataddr((display_name, email))
        to = self.to_field.GetValue().strip()
        if to:
            message["To"] = to
        cc = self.cc_field.GetValue().strip()
        if cc:
            message["Cc"] = cc
        bcc = self.bcc_field.GetValue().strip()
        if bcc:
            message["Bcc"] = bcc
        # The identity's own Reply-To, when it has one.
        reply_to = _identity_extra(account, email).get("reply_to", "")
        if reply_to:
            message["Reply-To"] = reply_to
        message["Subject"] = self.subject_field.GetValue().strip()
        message["Date"] = formatdate(localtime=True)
        # Without these two headers every reply starts a fresh
        # conversation in Gmail, Outlook and Thunderbird, however
        # the subject reads.
        if self.in_reply_to:
            message["In-Reply-To"] = self.in_reply_to
        if self.references:
            message["References"] = self.references
        # A single text part when the body carries nothing a plain
        # part could not express, multipart/alternative when it does.
        # The HTML part never travels alone: a recipient whose client
        # shows plain text still gets a readable message.
        text_body = self.body.get_text()
        html_body = self.body.get_html()
        message.set_content(text_body)
        if html_body:
            message.add_alternative(
                body_html.wrap_document(html_body), subtype="html"
            )
        return message

    def _attach_files_to(self, message):
        """
        Reads every attached file into `message`. Returns a list of
        (path, exception) pairs for any that could not be read, so
        the caller decides how loudly to report it: Send and a
        manual Save Draft show a dialog and stop, autosave logs and
        carries on rather than popping a dialog on top of someone
        mid-sentence (announce.py's own rule).
        """
        errors = []
        for path in self.attachment_paths:
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
                filename = os.path.basename(path)
                # Everything used to go out as application/octet-stream,
                # which is why images and PDFs sent from ZBox would not
                # preview in the recipient's client -- it is the "I have
                # no idea what this is" type. Guess from the filename and
                # fall back to octet-stream only when there is genuinely
                # nothing to go on.
                guessed, _encoding = mimetypes.guess_type(filename)
                if guessed and "/" in guessed:
                    maintype, subtype = guessed.split("/", 1)
                else:
                    maintype, subtype = "application", "octet-stream"
                message.add_attachment(
                    data, maintype=maintype, subtype=subtype, filename=filename
                )
            except OSError as exc:
                errors.append((path, exc))
        return errors

    def _on_send(self, event):
        """Asks the body for its content, then sends what comes back.
        The mirror is almost always already right; the flush is for the
        keystroke typed a moment before Ctrl+Enter, which may not have
        raised a change event in the page yet."""
        self.body.flush(self._send_now)

    def _send_now(self):
        entry = self._selected_identity_entry()
        if entry is None:
            wx.MessageBox(
                lang.t(
                    "dialogs", "send_no_account",
                    default="Add an account before sending, or select one in From.",
                ),
                lang.t("dialogs", "title_send", default="Send"),
                wx.OK | wx.ICON_WARNING,
            )
            return
        account, display_name, email = entry
        # An identity's own outgoing server and Sent folder, if it has
        # them; None keeps the account's.
        extra = _identity_extra(account, email)
        send_section = account.identity_section_name(extra) if extra.get("own_smtp") else None
        sent_folder = extra.get("sent_folder") or None

        to = self.to_field.GetValue().strip()
        if not to:
            wx.MessageBox(
                lang.t(
                    "dialogs", "send_no_recipient",
                    default="Enter at least one recipient in To.",
                ),
                lang.t("dialogs", "title_send", default="Send"),
                wx.OK | wx.ICON_WARNING,
            )
            self.to_field.SetFocus()
            return

        cc = self.cc_field.GetValue().strip()
        bcc = self.bcc_field.GetValue().strip()

        message = self._build_message(account, display_name, email)
        message["Message-ID"] = make_msgid()

        errors = self._attach_files_to(message)
        if errors:
            path, exc = errors[0]
            wx.MessageBox(
                lang.t(
                    "errors", "attach_unreadable",
                    default=f"Could not read attachment {path}:\n{exc}",
                    path=path, error=exc,
                ),
                lang.t("dialogs", "title_attach_file", default="Attach File"),
                wx.OK | wx.ICON_ERROR,
            )
            return

        # OpenPGP (stage 3): what Encrypt, Digitally Sign, Attach My Public
        # Key and the Autocrypt header do to this message, after any question
        # (openpgp_compose.plan_for_send). An empty plan sends exactly as
        # before; None means the send was cancelled, and why was said.
        recipients = _recipient_addresses(to, cc, bcc)
        plan = openpgp_compose.plan_for_send(
            self, self.main_frame.paths, account, email, recipients,
            self.encrypt_box.GetValue(), self.sign_box.GetValue(), self.attach_key_box.GetValue(),
        )
        if plan is None:
            return

        self._set_sending_state(True)
        self.main_frame.GetStatusBar().SetStatusText(lang.t(
            "main_ui", "sending_via",
            default=f"Sending via {email}...", sender=email,
        ))

        # With OpenPGP the message is built on the worker (openpgp_send),
        # once; a retry with AUTH LOGIN sends the same text again.
        raw_message = None if plan.active() else message.as_string()
        built = {"raw": raw_message}
        # Snapshot before the worker runs -- self._last_draft_id can
        # legitimately change (a new autosave lands) while Send is in
        # flight, and the cleanup below should purge whatever draft
        # existed at the moment Send was clicked, not whatever the
        # attribute happens to hold when the callback fires.
        # Where the superseded revision was saved, which is where the
        # purge below has to look for it.
        drafts_folder = self._last_draft_folder or self._identity_drafts_folder(account, email)
        trash_folder = _folder_display_to_himalaya("Trash", account)
        superseded_draft_id = (
            self._last_draft_id if self._last_draft_account_id == account.account_id else None
        )
        # Bracket-free, matching Himalaya's own envelope "message-id"
        # field -- see the identical comment in _save_draft.
        draft_message_id_header = (
            self._draft_message_id.strip("<>") if self._draft_message_id else None
        )

        paths = self.main_frame.paths
        lanes = self.main_frame.lanes

        def work():
            # SMTP only. The Sent copy and the draft purge run after the
            # send is reported (after_send), so the tab closes at once.
            if built["raw"] is None:
                import openpgp_send

                built["raw"] = openpgp_send.build(message, plan)
            himalaya_client.send_message_raw(
                paths, account, built["raw"], save_copy=False, section=send_section,
            )

        def after_send():
            himalaya_client.ensure_sent_copy(paths, account, built["raw"], folder=sent_folder)
            if superseded_draft_id is not None:
                # Best-effort: a sent message shouldn't leave a stale
                # copy of itself sitting in Drafts. Failure here
                # doesn't affect Send, which has already succeeded by
                # this point -- just logged, not surfaced.
                try:
                    himalaya_client.permanently_delete_message(
                        paths, account, superseded_draft_id,
                        drafts_folder, trash_folder, message_id_header=draft_message_id_header,
                    )
                except himalaya_client.HimalayaError:
                    logger.warning(
                        "Could not purge draft %s after sending.",
                        superseded_draft_id, exc_info=True,
                    )

        def after_send_failed(exc):
            logger.warning("After-send work failed: %s", exc)

        def on_success(_result):
            # Quiet background work: no status line, failures logged.
            lanes.for_account(account.account_id).submit(
                after_send, lambda _r: None, after_send_failed,
            )
            self._sent = True
            self._autosave_timer.Stop()
            self._set_sending_state(False)
            self.main_frame.GetStatusBar().SetStatusText(lang.t(
                "actions_announcements", "message_sent", default="Message sent."
            ))
            self.main_frame._play_sound("action-done")
            for field_value in (to, cc, bcc):
                # record_use: sending to someone is the signal that
                # ranks them in autocomplete next time (see
                # ContactManager.search). Receiving mail from them is
                # not -- a mailing list would otherwise outrank every
                # person you actually write to.
                self.main_frame.contacts.add_from_address_field(
                    field_value, record_use=True
                )
            # Mail to one of your own accounts: check that Inbox within
            # seconds rather than waiting for the server's push.
            self.main_frame._check_own_inboxes_after_send(
                ", ".join(value for value in (to, cc, bcc) if value)
            )
            index = self.main_frame.notebook.FindPage(self)
            if index != wx.NOT_FOUND and index > 0:
                # This route calls DeletePage directly instead of
                # going through main_frame._close_tab_at, so the
                # prepare_close hook never runs for it. The WebView
                # has to stop before the page is destroyed, or a
                # completed-load event lands on a controller that is
                # already shutting down.
                notebook = self.main_frame.notebook
                self.body.teardown()
                notebook.DeletePage(index)
                # _close_tab_at's other half is missing here too: it
                # hands focus to whatever page the notebook now shows.
                # Without this the notebook keeps focus and a screen
                # reader announces the tab rather than the message, so
                # a reply or a forward does not put you back where you
                # were reading. Scheduled, so the notebook has settled
                # after the delete, and holding the notebook rather
                # than this panel, which no longer exists by then.
                wx.CallAfter(_focus_page_underneath, notebook)
            # After the tab has gone, so focus lands back on the
            # message list rather than on a panel being destroyed.
            self.main_frame.announce_action(
                lang.t("actions_announcements", "message_sent",
                       default="Message sent."),
                title=lang.t("dialogs", "title_send", default="Send"),
            )

        def on_error(exc):
            # 504 5.5.4 Unsupported AUTH mechanism: the server refuses
            # AUTH PLAIN. Switch this account to AUTH LOGIN once, keep
            # it, and send again; a second failure reports normally.
            if (
                "unsupported auth mechanism" in str(exc).lower()
                and (
                    extra.get("smtp_auth", "plain") if send_section
                    else getattr(account, "smtp_auth", "plain")
                ) == "plain"
                and self._switch_smtp_to_login(account, extra if send_section else None)
            ):
                logger.info("Retrying send for %s with AUTH LOGIN.", account.account_id)
                lanes.for_account(account.account_id).submit(work, on_success, on_error)
                return
            try:
                self._set_sending_state(False)
            except RuntimeError:
                # The tab closed while the send was in flight; the
                # failure below must still be reported.
                logger.debug("Compose tab closed before its send failed.")
            self.main_frame.GetStatusBar().SetStatusText(lang.t(
                "main_ui", "send_failed",
                default="Send failed. See logs for details.",
            ))
            # A TLS library error, not a mail one: ZBox opened an
            # encrypted connection and the outgoing server answered in
            # plain text, so encryption and port do not match (587
            # wants STARTTLS, 465 wants SSL/TLS). The raw text says
            # none of that, so it gets a plain explanation, with the
            # original kept below it for reference.
            if "InvalidContentType" in str(exc):
                text = lang.t(
                    "errors", "send_tls_mismatch",
                    default=(
                        "Could not send the message. The outgoing server answered "
                        "without encryption, so the outgoing encryption does not "
                        "match the port. In Tools, Account and Identities Settings, use STARTTLS "
                        f"for port 587, or SSL/TLS for port 465, then send again.\n\n{exc}"
                    ),
                    error=exc,
                )
            else:
                text = lang.t(
                    "errors", "send_failed",
                    default=f"Could not send message.\n\n{exc}", error=exc,
                )
            wx.MessageBox(
                text,
                lang.t("dialogs", "title_send_failed", default="Send Failed"),
                wx.OK | wx.ICON_ERROR,
            )

        # A tick as the send starts, so there is something to hear
        # while it runs; action-done and the confirmation follow on
        # success, and the dialog above on failure.
        self.main_frame._play_sound("progress-tick")
        self.main_frame.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _switch_smtp_to_login(self, account, extra=None):
        """Moves an account's outgoing login to AUTH LOGIN, or, with
        extra, that of an identity with its own outgoing server. Saves
        it and rewrites Himalaya's config. True when that worked."""
        manager = getattr(self.main_frame, "account_manager", None)
        if manager is None:
            logger.warning("No account manager; cannot switch to AUTH LOGIN.")
            return False
        holder = extra if extra is not None else None
        if holder is not None:
            holder["smtp_auth"] = "login"
        else:
            account.smtp_auth = "login"
        try:
            manager.save_accounts()
            manager.write_himalaya_config()
        except Exception:  # noqa: BLE001 - fall back to the normal error
            if holder is not None:
                holder["smtp_auth"] = "plain"
            else:
                account.smtp_auth = "plain"
            logger.warning("Could not switch %s to AUTH LOGIN.", account.account_id, exc_info=True)
            return False
        return True

    def _set_sending_state(self, sending):
        if sending:
            # Disabling the focused Send button hands focus to the
            # next enabled control, which is Save Draft -- so the
            # screen reader reads "Save Draft" for the fraction of a
            # second before the tab closes, which is both wrong and
            # the only thing the sender hears. Parking focus on the
            # panel first leaves nothing to read but the send's own
            # sound and confirmation.
            self.SetFocus()
        self.send_button.Enable(not sending)
        self.to_field.Enable(not sending)
        self.cc_field.Enable(not sending)
        self.bcc_field.Enable(not sending)
        self.subject_field.Enable(not sending)
        self.body.enable(not sending)
        self.send_button.SetLabel(
            lang.control_label("comp_sending", "Sending...\tCtrl+Enter")
            if sending
            else lang.control_label("title_send", "Send\tCtrl+Enter")
        )

    def _on_save_draft(self, event):
        """A manual save flushes first, for the same reason Send does.
        The autosave tick deliberately does not: a round trip on every
        background tick costs more than one keystroke of lag in a
        draft revision."""
        self.body.flush(lambda: self._save_draft(silent=False))

    def _restart_autosave_timer(self):
        self._autosave_timer.Stop()
        seconds = getattr(
            self.main_frame.settings_manager.settings, "draft_autosave_seconds", 60
        )
        if seconds and seconds > 0:
            self._autosave_timer.Start(int(seconds) * 1000, wx.TIMER_CONTINUOUS)

    def _on_autosave_timer(self, event):
        if self.has_unsaved_changes():
            self._save_draft(silent=True)

    def _on_destroy(self, event):
        # The tab is closing (Send, Discard, or a plain Escape/Ctrl+W
        # once nothing was unsaved) -- nothing left to autosave.
        # wx.Timer isn't guaranteed to stop itself just because its
        # owner window was destroyed, and a stray tick calling back
        # into a torn-down panel's fields is exactly the kind of bug
        # BulkProgressDialog.update() has to guard against with
        # try/except RuntimeError; stopping explicitly here avoids
        # ever reaching that state.
        self._autosave_timer.Stop()
        # Idempotent, and the last of three routes that reach it:
        # prepare_close for a normal tab close, the send path for its
        # own DeletePage, and this for anything else that destroys the
        # panel.
        self.body.teardown()
        event.Skip()

    def _save_draft(self, silent, on_done=None):
        """
        Appends the current compose state to Drafts as a new
        revision (himalaya_client.add_message_raw, 'message add
        --mailbox <Drafts> -f draft'), then purges the previous
        revision of THIS draft -- found by the stable Message-ID
        generated the first time this tab was ever saved -- so
        autosave doesn't leave a trail of duplicates behind on every
        tick. IMAP/Maildir APPEND has no in-place update, only
        append-then-delete; see add_message_raw's docstring.

        silent=True (autosave ticks) never raises a dialog and never
        steals focus: failures go to the status bar and the log only,
        matching announce.py's rule that interrupting on every
        background tick is worse than the silence it replaces.
        silent=False (the Save Draft button/Ctrl+S, and Save from the
        close-tab prompt) shows a dialog on failure.

        on_done, when given, is called on the main thread with
        True/False (success) once this save has actually finished --
        used by _save_draft_blocking(), which has to know before it
        can tell confirm_discard whether closing the tab is safe.
        Called synchronously, before returning, for every failure
        that's caught before ever reaching the worker thread (no
        account selected, an unreadable attachment); asynchronously,
        from the worker's callback, otherwise.
        """
        entry = self._selected_identity_entry()
        if entry is None:
            if not silent:
                wx.MessageBox(
                    lang.t(
                        "dialogs", "draft_no_account",
                        default="Add an account before saving a draft, or select one in From.",
                    ),
                    lang.t("dialogs", "title_save_draft", default="Save Draft"),
                    wx.OK | wx.ICON_WARNING,
                )
            if on_done is not None:
                on_done(False)
            return
        account, display_name, email = entry

        if self._draft_message_id is None:
            self._draft_message_id = make_msgid()

        message = self._build_message(account, display_name, email)
        message["Message-ID"] = self._draft_message_id

        errors = self._attach_files_to(message)
        if errors:
            unreadable = ", ".join(str(p) for p, _exc in errors)
            if not silent:
                path, exc = errors[0]
                wx.MessageBox(
                    lang.t(
                        "errors", "draft_attach_unreadable",
                        default=f"Could not read attachment {path}:\n{exc}\n\nDraft not saved.",
                        path=path, error=exc,
                    ),
                    lang.t("dialogs", "title_save_draft", default="Save Draft"),
                    wx.OK | wx.ICON_ERROR,
                )
                if on_done is not None:
                    on_done(False)
                return
            logger.warning("Draft autosave: skipping unreadable attachment(s): %s", unreadable)

        # Encrypt on: the draft is saved encrypted to your own key
        # (openpgp_send.build_draft), or not at all -- never in plain text.
        draft_key = None
        if self.encrypt_box.GetValue():
            draft_key, problem = openpgp_compose.draft_key(self.main_frame.paths, account, email)
            if draft_key is None:
                logger.warning("Draft not saved: it cannot be encrypted.")
                if silent:
                    try:
                        self.main_frame.GetStatusBar().SetStatusText(
                            lang.t(
                                "main_ui", "draft_autosave_failed",
                                default="Draft autosave failed. See logs for details.",
                            )
                        )
                    except RuntimeError:
                        pass
                else:
                    wx.MessageBox(
                        lang.t(
                            "errors", "draft_save_failed",
                            default=f"Could not save draft.\n\n{problem}", error=problem,
                        ),
                        lang.t("dialogs", "title_save_draft", default="Save Draft"),
                        wx.OK | wx.ICON_ERROR,
                    )
                if on_done is not None:
                    on_done(False)
                return
        raw_message = None if draft_key else message.as_string()
        drafts_folder = self._identity_drafts_folder(account, email)
        trash_folder = _folder_display_to_himalaya("Trash", account)
        previous_id = (
            self._last_draft_id if self._last_draft_account_id == account.account_id else None
        )
        # The previous revision is purged from wherever it was saved,
        # which differs when From moved to an identity with its own
        # Drafts folder.
        previous_folder = self._last_draft_folder or drafts_folder
        # Himalaya's own envelope "message-id" field is the bracket-
        # free form ("abc@host", confirmed live against the pinned
        # v2.1.0 build), but an RFC 5322 Message-ID *header* --
        # what's actually in raw_message above -- must keep its
        # angle brackets. permanently_delete_message compares against
        # the bracket-free envelope field, so the lookup value here
        # has to be stripped to match, or every purge would silently
        # report "not_found" and every autosave tick would leave
        # another orphaned revision behind in Drafts.
        draft_message_id_header = self._draft_message_id.strip("<>")

        def work():
            text = raw_message
            if text is None:
                import openpgp_send

                text = openpgp_send.build_draft(draft_key[0], message, draft_key[1])
            new_id = himalaya_client.add_message_raw(
                self.main_frame.paths, account, drafts_folder, text, flags=("draft",),
            )
            if previous_id is not None:
                try:
                    himalaya_client.permanently_delete_message(
                        self.main_frame.paths, account, previous_id, previous_folder,
                        trash_folder, message_id_header=draft_message_id_header,
                    )
                except himalaya_client.HimalayaError:
                    logger.warning(
                        "Could not purge superseded draft revision %s.",
                        previous_id, exc_info=True,
                    )
            return new_id

        def on_success(new_id):
            self._last_draft_id = new_id
            self._last_draft_account_id = account.account_id
            self._last_draft_folder = drafts_folder
            try:
                self._last_saved_state = self._current_state()
            except RuntimeError:
                pass  # tab closed while the save was in flight
            if not silent:
                # Autosave runs on a timer, so it writes no status line
                # at all: a screen reader reading status bar changes
                # spoke "Draft autosaved." every interval. A failed
                # autosave still says so (on_error below).
                try:
                    self.main_frame.GetStatusBar().SetStatusText(
                        lang.t(
                            "actions_announcements", "draft_saved",
                            default="Draft saved.",
                        )
                    )
                except RuntimeError:
                    pass
            if not silent:
                # The status bar alone is never spoken (announce.py),
                # which is why neither Ctrl+S, the Save Draft button
                # nor Compose > Save Draft said anything -- all three
                # land here. Same route the send path uses for
                # "Message sent.". Autosave stays silent on purpose.
                self.main_frame.announce_action(
                    lang.t("actions_announcements", "draft_saved",
                           default="Draft saved."),
                    title=lang.t(
                        "dialogs", "title_save_draft", default="Save Draft",
                    ),
                )
            if on_done is not None:
                on_done(True)

        def on_error(exc):
            logger.warning("Draft save failed.", exc_info=True)
            if silent:
                try:
                    self.main_frame.GetStatusBar().SetStatusText(
                        lang.t(
                            "main_ui", "draft_autosave_failed",
                            default="Draft autosave failed. See logs for details.",
                        )
                    )
                except RuntimeError:
                    pass
            else:
                wx.MessageBox(
                    lang.t(
                        "errors", "draft_save_failed",
                        default=f"Could not save draft.\n\n{exc}", error=exc,
                    ),
                    lang.t("dialogs", "title_save_draft", default="Save Draft"),
                    wx.OK | wx.ICON_ERROR,
                )
            if on_done is not None:
                on_done(False)

        self.main_frame.lanes.for_account(account.account_id).submit(work, on_success, on_error)

    def _save_draft_blocking(self):
        """
        Drives _save_draft(silent=False) to completion and returns
        whether it succeeded, for confirm_discard's "Save Draft"
        choice -- which must answer synchronously whether closing the
        tab is safe. See _SavingDraftDialog's docstring for how a
        worker-thread call gets turned into something confirm_discard
        can wait on without ever running Himalaya on the UI thread.
        """
        dialog = _SavingDraftDialog(self)
        outcome = {}

        def on_done(success):
            outcome["success"] = success
            if dialog.IsModal():
                dialog.EndModal(wx.ID_OK)

        self._save_draft(silent=False, on_done=on_done)

        if "success" not in outcome:
            # Only reached when _save_draft actually queued work on
            # the worker thread -- the synchronous failure paths
            # (no account, unreadable attachment) already called
            # on_done before returning above.
            dialog.ShowModal()
        dialog.Destroy()
        return outcome.get("success", False)

    def _on_attach(self, event):
        with wx.FileDialog(
            self,
            lang.t("dialogs", "title_attach_file", default="Attach File"),
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                path = dialog.GetPath()
                self.attachment_paths.append(path)
                notice_toast.notify(
                    self,
                    lang.t(
                        "dialogs", "attached_file",
                        default=f"Attached: {os.path.basename(path)}\n\n"
                        f"{len(self.attachment_paths)} file(s) will be attached when you send.",
                        name=os.path.basename(path),
                        count=len(self.attachment_paths),
                    ),
                    lang.t("dialogs", "title_attach_file", default="Attach File"),
                )
