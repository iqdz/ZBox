"""
Signature editing for one account.

Two editors on a notebook. The formatted one is the same body engine
the composer uses -- Trix in a WebView, with the markup edit box as
the automatic fallback -- so a signature is written the same way a
message is. The HTML source page is there for anyone who would
rather write the markup themselves.

Whichever page was last edited is the one OK reads, and switching
pages says which that is rather than leaving it to be guessed.

No RichTextCtrl anywhere. It swallows Tab and traps the keyboard
inside the control, which is the whole reason compose_body exists.
"""

import wx

import body_html
import compose_body
import lang
from accessible import fit_dialog, wrap_text
from announce import speak


class SignatureEditDialog(wx.Dialog):
    """
    Edits one account's signature. After ShowModal() returns
    wx.ID_OK, read signature_html and signature_text off the dialog;
    they are what the account should be given.
    """

    def __init__(self, parent, signature_html="", signature_text=""):
        super().__init__(
            parent,
            title=lang.t("dialogs", "acct_signature", default="Signature"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.signature_html = signature_html or ""
        self.signature_text = signature_text or ""
        # True once the HTML source page has actually been typed in.
        # Until then the source is regenerated from the formatted
        # editor, and the formatted editor is what OK reads.
        self._html_edited = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.notebook = wx.Notebook(self)
        self.notebook.SetName(
            lang.t("dialogs", "sig_editors", default="Signature editors")
        )

        formatted_page = wx.Panel(self.notebook)
        formatted_sizer = wx.BoxSizer(wx.VERTICAL)
        # A visible label immediately in front of the control: on
        # Windows a screen reader names an edit box from the static
        # text before it, not from SetName on its own.
        formatted_sizer.Add(
            wx.StaticText(
                formatted_page,
                label=lang.t(
                    "dialogs", "sig_formatted", default="Signature, formatted",
                ),
            ),
            0, wx.LEFT | wx.RIGHT | wx.TOP, 6,
        )
        self.body = compose_body.make_body(
            formatted_page, initial_html=self._starting_html(),
        )
        try:
            self.body.control.SetName(
                lang.t(
                    "dialogs", "sig_formatted", default="Signature, formatted",
                )
            )
        except RuntimeError:
            pass
        formatted_sizer.Add(self.body.control, 1, wx.EXPAND | wx.ALL, 6)
        formatted_page.SetSizer(formatted_sizer)
        self.notebook.AddPage(formatted_page, lang.t('dialogs', 'sig_tab_formatted', default="Formatted"))

        source_page = wx.Panel(self.notebook)
        source_sizer = wx.BoxSizer(wx.VERTICAL)
        source_sizer.Add(
            wx.StaticText(
                source_page,
                label=lang.t(
                    "dialogs", "sig_source", default="Signature, HTML source",
                ),
            ),
            0, wx.LEFT | wx.RIGHT | wx.TOP, 6,
        )
        self.source_field = wx.TextCtrl(source_page, style=wx.TE_MULTILINE)
        self.source_field.SetName(
            lang.t("dialogs", "sig_source", default="Signature, HTML source")
        )
        self.source_field.SetValue(self.signature_html)
        self.source_field.Bind(wx.EVT_TEXT, self._on_source_typed)
        source_sizer.Add(self.source_field, 1, wx.EXPAND | wx.ALL, 6)
        source_page.SetSizer(source_sizer)
        self.notebook.AddPage(source_page, lang.t('dialogs', 'sig_tab_source', default="HTML source"))

        sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)

        text_label = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "sig_plain_label",
                default="Plain text alternative (sent alongside the HTML)",
            ),
        )
        self.text_field = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        self.text_field.SetName(
            lang.t(
                "dialogs", "sig_plain_name", default="Plain text alternative",
            )
        )
        self.text_field.SetValue(self.signature_text)
        self.text_field.SetMinSize((-1, self.text_field.GetCharHeight() * 4))

        note = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "sig_note_plain",
                default=(
                    "Leave the plain text alternative blank and it is written "
                    "from the formatted signature when you press OK."
                ),
            ),
        )
        wrap_text(note)

        sizer.Add(text_label, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.text_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        sizer.Add(note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(
            self.CreateButtonSizer(wx.OK | wx.CANCEL), 0,
            wx.ALIGN_RIGHT | wx.ALL, 10,
        )

        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_page_changed)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        fit_dialog(self, sizer)
        self.SetSize((640, 600))
        # Land in the editor itself, not on the notebook tab.
        wx.CallAfter(self.body.focus)

    def _starting_html(self):
        """What the formatted editor opens with: the stored HTML, or
        the plain signature converted, for an account that has only
        ever had a plain one."""
        if self.signature_html:
            return self.signature_html
        if self.signature_text:
            return body_html.text_to_html(self.signature_text)
        return ""

    def _say(self, text):
        # speak() takes no focus and raises no window. The dialog
        # fallback in announce.py is deliberately not used here: a
        # second dialog over this one would move focus out of the
        # editor in the middle of writing.
        speak(self, text)

    def _on_source_typed(self, event):
        self._html_edited = True
        event.Skip()

    def _on_page_changed(self, event):
        if self.notebook.GetSelection() == 1:
            if not self._html_edited:
                self.body.flush(self._fill_source)
            self._say(lang.t(
                "actions_announcements", "signature_page_html",
                default="HTML source. This is what will be saved.",
            ))
        elif self._html_edited:
            self._say(lang.t(
                "actions_announcements", "signature_page_html_still_wins",
                default="Formatted. The HTML source is still what will be saved.",
            ))
        else:
            self._say(lang.t(
                "actions_announcements", "signature_page_formatted",
                default="Formatted. This is what will be saved.",
            ))
        event.Skip()

    def _fill_source(self):
        """ChangeValue rather than SetValue, so refilling the source
        from the formatted editor does not count as editing it."""
        try:
            self.source_field.ChangeValue(self.body.get_html())
        except RuntimeError:
            pass

    def _on_ok(self, event):
        # A WebView body cannot be read synchronously, so OK does not
        # close the dialog itself: it asks the page for its content
        # and closes in the callback. The markup fallback answers
        # immediately and takes the same path. event.Skip() is not
        # called, which is what stops wx closing it underneath us.
        self.body.flush(self._finish)

    def _finish(self):
        if self._html_edited:
            self.signature_html = self.source_field.GetValue().strip()
        else:
            self.signature_html = (self.body.get_html() or "").strip()
        text = self.text_field.GetValue().strip()
        if not text:
            text = body_html.html_to_text(self.signature_html).strip()
        self.signature_text = text
        self.EndModal(wx.ID_OK)

    def _on_destroy(self, event):
        # Fires for every child as well, so only this dialog's own
        # destruction tears the body down.
        if event.GetWindow() is self:
            self.body.teardown()
        event.Skip()
