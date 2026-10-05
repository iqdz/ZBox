"""
S/MIME Certificate Manager (Tools > S/MIME Certificate Manager): your own
certificates, imported from .p12 or .pfx files with their private keys, and
your correspondents' certificates, imported by hand or kept from mail they
signed.

Laid out like the OpenPGP Key Manager: one line per certificate in a plain
list, so a screen reader reads each as one sentence, the selected
certificate's details in a read-only field below it, every label created
just before its own control.
"""

import logging
import os
import re

import wx

import lang
import notice_toast
import single_instance
import smime

logger = logging.getLogger("zbox.smime")


def _title():
    return lang.t("dialogs", "smime_title", default="S/MIME Certificate Manager")


def _date(text):
    return str(text or "")[:10]


def row_text(entry):
    """One list line: name, address, your own, and the dates."""
    parts = [entry.get("name") or ", ".join(entry.get("emails") or []) or entry["fingerprint"][:16]]
    emails = entry.get("emails") or []
    if emails and entry.get("name"):
        parts.append(emails[0])
    if entry.get("own"):
        parts.append(lang.t("dialogs", "smime_own", default="your certificate"))
    parts.append(_state_text(entry))
    return ", ".join(parts)


def _state_text(entry):
    import datetime

    try:
        end = datetime.datetime.fromisoformat(entry.get("not_after"))
        if end < datetime.datetime.now(datetime.timezone.utc):
            return lang.t("dialogs", "smime_expired", default="expired")
    except Exception:  # noqa: BLE001
        pass
    return lang.t("dialogs", "smime_expires", default="expires {date}", date=_date(entry.get("not_after")))


def details_text(entry):
    """Everything about one certificate, one fact per line."""
    lines = []
    if entry.get("name"):
        lines.append(entry["name"])
    lines += list(entry.get("emails") or [])
    lines.append("%s: %s" % (lang.t("dialogs", "smime_issuer", default="Issued by"), entry.get("issuer") or ""))
    lines.append(lang.t(
        "dialogs", "smime_valid", default="Valid from {start} to {end}",
        start=_date(entry.get("not_before")), end=_date(entry.get("not_after")),
    ))
    fp = entry.get("fingerprint") or ""
    lines.append("%s: %s" % (
        lang.t("dialogs", "smime_fingerprint", default="SHA-256 fingerprint"),
        ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper(),
    ))
    if entry.get("own"):
        lines.append(lang.t("dialogs", "smime_own_key", default="Your certificate, with its private key: mail encrypted to it can be read here."))
    return "\n".join(lines)


def show_certificate_manager(parent, paths):
    """Tools > S/MIME Certificate Manager."""
    if not smime.available():
        notice_toast.notify(parent, lang.t(
            "dialogs", "smime_unavailable", default="S/MIME is not available in this copy of ZBox.",
        ), _title())
        return
    try:
        store = smime.CertStore(paths)
    except Exception as exc:  # noqa: BLE001
        wx.MessageBox(str(exc), _title(), wx.OK | wx.ICON_ERROR, parent)
        return
    dialog = CertificateManagerDialog(parent, store)
    dialog.ShowModal()
    dialog.Destroy()


class CertificateManagerDialog(wx.Dialog):
    def __init__(self, parent, store):
        super().__init__(
            parent, title=_title(), size=(640, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.store = store
        self._rows = []

        sizer = wx.BoxSizer(wx.VERTICAL)
        list_label = wx.StaticText(self, label=_title())
        list_label.SetFont(list_label.GetFont().Bold())
        self.cert_list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.cert_list.SetName(_title())
        self.cert_list.Bind(wx.EVT_LISTBOX, lambda event: self._show_details())
        sizer.Add(list_label, 0, wx.ALL, 8)
        sizer.Add(self.cert_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        details_label = wx.StaticText(self, label=lang.t("dialogs", "smime_details", default="Certificate Details"))
        self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.details.SetName(lang.t("dialogs", "smime_details", default="Certificate Details"))
        self.details.SetMinSize((-1, self.details.GetCharHeight() * 7))
        sizer.Add(details_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._buttons = {}
        for key, label, handler in (
            ("import", lang.control_label("smime_import", "&Import Certificate File"), self._on_import),
            ("export", lang.control_label("smime_export", "&Export Certificate"), self._on_export),
            ("delete", lang.control_label("smime_delete", "&Delete Certificate"), self._on_delete),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, 4)
            self._buttons[key] = button
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)

        close_sizer = wx.BoxSizer(wx.HORIZONTAL)
        close_button = wx.Button(self, wx.ID_CLOSE, lang.t("dialogs", "btn_close_plain", default="Close"))
        close_button.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_CLOSE))
        close_sizer.Add(close_button, 0)
        sizer.Add(close_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)
        self._populate()
        wx.CallAfter(self.cert_list.SetFocus)

    def _populate(self, select=None):
        self._rows = sorted(
            self.store.entries(),
            key=lambda entry: (not entry.get("own"), (entry.get("name") or "").lower()),
        )
        self.cert_list.Clear()
        if not self._rows:
            self.cert_list.Append(lang.t(
                "dialogs", "smime_no_certs",
                default="No certificates yet. Import your own .p12 or .pfx file, or a correspondent's certificate.",
            ))
            self.cert_list.SetSelection(0)
            self.details.SetValue("")
            self._sync_buttons()
            return
        index = 0
        for row, entry in enumerate(self._rows):
            self.cert_list.Append(row_text(entry))
            if entry["fingerprint"] == select:
                index = row
        self.cert_list.SetSelection(index)
        self._show_details()

    def _selected(self):
        index = self.cert_list.GetSelection()
        if index == wx.NOT_FOUND or index < 0 or index >= len(self._rows):
            return None
        return self._rows[index]

    def _show_details(self):
        entry = self._selected()
        self.details.SetValue(details_text(entry) if entry else "")
        self._sync_buttons()

    def _sync_buttons(self):
        has = self._selected() is not None
        self._buttons["export"].Enable(has)
        self._buttons["delete"].Enable(has)

    def _error(self, exc):
        wx.MessageBox(
            lang.t("dialogs", "smime_import_failed", default="The certificate could not be imported: {error}", error=str(exc)),
            _title(), wx.OK | wx.ICON_ERROR, self,
        )

    def _on_import(self, event):
        try:
            self._import_file()
        finally:
            self._refocus()

    def _refocus(self):
        """The manager back in front with focus on its list, after a
        file dialog, password dialog or message box: Windows may hand
        focus to the disabled main window instead."""
        try:
            self.Raise()
            single_instance.force_foreground(self.GetHandle())
        except Exception:  # noqa: BLE001
            pass
        wx.CallAfter(self.cert_list.SetFocus)

    def _import_file(self):
        dialog = wx.FileDialog(
            self, lang.t("dialogs", "smime_import_title", default="Import Certificate File"),
            wildcard="*.p12;*.pfx;*.cer;*.crt;*.pem;*.der|*.p12;*.pfx;*.cer;*.crt;*.pem;*.der|*.*|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        try:
            if os.path.getsize(path) > smime.MAX_IMPORT_BYTES:
                raise smime.SmimeError("larger than 5 MB")
            with open(path, "rb") as handle:
                raw = handle.read()
        except (OSError, smime.SmimeError) as exc:
            self._error(exc)
            return
        password = None
        if smime.CertStore.is_pkcs12(raw):
            ask = wx.PasswordEntryDialog(
                self,
                lang.t("dialogs", "smime_password_prompt", default="Enter the password of {name}.",
                       name=os.path.basename(path)),
                lang.t("dialogs", "smime_password_title", default="Certificate Password"),
            )
            try:
                if ask.ShowModal() != wx.ID_OK:
                    return
                password = ask.GetValue()
            finally:
                ask.Destroy()
        try:
            added = self.store.import_bytes(raw, password)
        except (smime.SmimeError, OSError) as exc:
            self._error(exc)
            return
        self._populate(select=added[0]["fingerprint"] if added else None)
        self.cert_list.SetFocus()
        notice_toast.notify(self, lang.t("dialogs", "smime_imported", default="Certificate imported."), _title())

    def _on_export(self, event):
        try:
            self._export_file()
        finally:
            self._refocus()

    def _export_file(self):
        entry = self._selected()
        if entry is None:
            return
        name = re.sub(r"[^\w.@-]+", "_", (entry.get("emails") or [entry.get("name") or "certificate"])[0]) or "certificate"
        dialog = wx.FileDialog(
            self, lang.t("dialogs", "smime_export_title", default="Export Certificate"),
            defaultFile=name + ".pem", wildcard="*.pem|*.pem",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        try:
            with open(path, "wb") as handle:
                handle.write(self.store.public_pem(entry["fingerprint"]) or b"")
        except OSError as exc:
            self._error(exc)
            return
        notice_toast.notify(self, lang.t("dialogs", "smime_exported", default="Certificate exported."), _title())

    def _on_delete(self, event):
        entry = self._selected()
        if entry is None:
            return
        answer = wx.MessageBox(
            lang.t(
                "dialogs", "smime_delete_confirm",
                default="Delete the certificate of {name}? If it is your own, mail encrypted to it can no longer be read here.",
                name=entry.get("name") or ", ".join(entry.get("emails") or []),
            ),
            _title(), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self,
        )
        if answer != wx.YES:
            self.cert_list.SetFocus()
            return
        try:
            self.store.delete(entry["fingerprint"])
        except OSError as exc:
            self._error(exc)
            return
        self._populate()
        self.cert_list.SetFocus()
        notice_toast.notify(self, lang.t("dialogs", "smime_deleted", default="Certificate deleted."), _title())
