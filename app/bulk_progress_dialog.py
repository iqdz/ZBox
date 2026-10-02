"""
Progress dialog for a bulk action on more than one message (Delete,
Archive, Move To, Copy To, flag toggle, Mark Read/Unread) -- audit
finding 34's visible half: a Cancel button, and a running count so a
selection of 50 does not look identical to a hang.

Deliberately not the vehicle for a screen reader to *hear* progress:
announce.py's own design note applies here just as much -- interrupting
on every one of up to however-many items would be worse than the
silence it replaces. The label and gauge are for sighted users; the
one thing every user needs is a reachable way to cancel, which is
what the Cancel button is for. The run's outcome (including any
failures) is read aloud once at the end via announce(), by the
caller, after this dialog has closed.
"""

import wx

import lang
from accessible import fit_dialog, wrap_text


class BulkProgressDialog(wx.Dialog):
    def __init__(self, parent, title, total):
        super().__init__(parent, title=title, style=wx.CAPTION)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.total = total
        self._cancelled = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.status_text = wx.StaticText(
            self,
            label=lang.t(
                "dialogs", "bulk_progress", default=f"0 of {total} done.",
                completed=0, total=total,
            ),
        )
        wrap_text(self.status_text, 52)
        sizer.Add(self.status_text, 0, wx.ALL, 12)

        self.gauge = wx.Gauge(self, range=max(total, 1))
        self.gauge.SetMinSize((320, -1))
        sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        self.cancel_button = wx.Button(
            self, wx.ID_CANCEL,
            lang.t("dialogs", "btn_cancel_plain", default="Cancel"),
        )
        self.cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
        sizer.Add(self.cancel_button, 0, wx.ALIGN_RIGHT | wx.ALL, 12)

        fit_dialog(self, sizer)
        wx.CallAfter(self.cancel_button.SetFocus)

    def _on_cancel(self, _event):
        self._cancelled = True
        self.cancel_button.Disable()
        self.status_text.SetLabel(
            lang.t(
                "dialogs", "bulk_cancelling",
                default="Cancelling -- finishing the current message...",
            )
        )

    def is_cancelled(self):
        return self._cancelled

    def update(self, completed, total):
        """Runs on the main thread via wx.CallAfter, posted from the
        worker thread's run_bulk() progress callback. Guards against
        a stray update arriving after the dialog has already been
        Destroy()-ed (EndModal fired, the modal loop returned, and
        the caller cleaned up) -- the underlying C++ object is gone
        by then and touching it raises RuntimeError, not an
        AttributeError, since the Python wrapper is still alive."""
        try:
            self.gauge.SetValue(min(completed, total))
            if not self._cancelled:
                self.status_text.SetLabel(
                    lang.t(
                        "dialogs", "bulk_progress",
                        default=f"{completed} of {total} done.",
                        completed=completed, total=total,
                    )
                )
        except RuntimeError:
            pass  # dialog already destroyed

    def finish(self):
        if self.IsModal():
            self.EndModal(wx.ID_OK)
