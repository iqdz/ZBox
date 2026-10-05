"""
OpenPGP Key Manager (Tools > OpenPGP Key Manager), stage 1 of
end-to-end encryption: your keys and your correspondents' keys.

Thunderbird's words throughout. One line per key in a plain list, so a
screen reader reads each key as one sentence, and the selected key's
full details in a read-only field below it. Every label is created
just before its own control, because JAWS names controls by creation
order. Long work, generating an RSA key, runs on a worker thread so the
window stays responsive.
"""

import logging
import os
import re
import threading

import wx

import lang
import notice_toast
from accessible import wrap_text
import openpgp

logger = logging.getLogger("zbox.openpgp")


def _t(key, default, **values):
    return lang.t("dialogs", key, default=default, **values)


def _title():
    return lang.t("dialogs", "pgp_title", default="OpenPGP Key Manager")


def identity_pairs(accounts):
    """(display name, address) for every identity of every account,
    each address once."""
    pairs, seen = [], set()
    for account in accounts or []:
        try:
            items = account.identities()
        except Exception:  # noqa: BLE001 - an odd account adds nothing
            items = []
        for item in items or []:
            if isinstance(item, dict):
                name = item.get("display_name") or item.get("name") or ""
                email = item.get("email") or item.get("address") or ""
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                name, email = item[0], item[1]
            else:
                name = getattr(item, "display_name", "")
                email = getattr(item, "email", "")
            email = str(email or "").strip()
            if email and email.lower() not in seen:
                seen.add(email.lower())
                pairs.append((str(name or "").strip(), email))
    return pairs


def show_key_manager(parent, paths, accounts):
    """Tools > OpenPGP Key Manager."""
    if not openpgp.available():
        notice_toast.notify(
            parent,
            lang.t("dialogs", "pgp_unavailable", default="OpenPGP is not available in this copy of ZBox."),
            _title(),
        )
        return
    try:
        store = openpgp.KeyStore(paths)
    except openpgp.OpenPGPError as exc:
        wx.MessageBox(
            lang.t("dialogs", "pgp_save_failed", default="The keys could not be saved: {error}", error=str(exc)),
            _title(), wx.OK | wx.ICON_ERROR, parent,
        )
        return
    dialog = KeyManagerDialog(parent, store, identity_pairs(accounts))
    dialog.ShowModal()
    dialog.Destroy()


def _safe_name(text):
    return re.sub(r"[^\w.@-]+", "_", text or "key").strip("_") or "key"


def _status_text(entry):
    if entry.get("revoked"):
        return lang.t("dialogs", "pgp_revoked", default="revoked")
    if entry.get("expired"):
        return lang.t("dialogs", "pgp_expired", default="expired")
    if entry.get("expiration"):
        return lang.t("dialogs", "pgp_expires", default="expires: {date}", date=openpgp.format_date(entry["expiration"]))
    return lang.t("dialogs", "pgp_never_expires", default="never expires")


def row_text(entry):
    """One list line: name, address, personal key, and the key's state."""
    parts = [entry.get("name") or entry.get("email") or openpgp.key_id(entry["fingerprint"])]
    if entry.get("name") and entry.get("email"):
        parts.append(entry["email"])
    if entry.get("personal"):
        parts.append(lang.t("dialogs", "pgp_personal", default="personal key"))
    parts.append(_status_text(entry))
    return ", ".join(parts)


def _acceptance_text(value):
    return {
        "rejected": lang.t("dialogs", "pgp_acc_rejected", default="No, reject this key."),
        "unverified": lang.t("dialogs", "pgp_acc_unverified", default="Yes, but I have not verified that it is the correct key."),
        "verified": lang.t("dialogs", "pgp_acc_verified", default="Yes, I’ve verified in person this key has the correct fingerprint."),
    }.get(value, lang.t("dialogs", "pgp_acc_undecided", default="Not yet, maybe later."))


def details_text(entry):
    """Everything about one key, one fact per line."""
    lines = list(entry.get("user_ids") or [])
    lines.append("%s: %s" % (lang.t("dialogs", "pgp_fingerprint", default="Fingerprint"), openpgp.fingerprint_text(entry["fingerprint"])))
    lines.append("%s: %s" % (lang.t("dialogs", "pgp_key_id", default="Key ID"), openpgp.key_id(entry["fingerprint"])))
    created = openpgp.format_date(entry.get("created"))
    if created:
        lines.append("%s: %s" % (lang.t("dialogs", "pgp_created", default="Created"), created))
    lines.append(_status_text(entry))
    if entry.get("secret"):
        lines.append(
            lang.t("dialogs", "pgp_personal_yes", default="Yes, treat this key as a personal key.") if entry.get("personal")
            else lang.t("dialogs", "pgp_personal_no", default="No, don’t use it as my personal key.")
        )
    else:
        lines.append("%s: %s" % (lang.t("dialogs", "pgp_acceptance", default="Your Acceptance"), _acceptance_text(entry.get("acceptance"))))
    return "\n".join(lines)


def _ask_passphrase(parent, name):
    dialog = wx.PasswordEntryDialog(
        parent,
        lang.t("dialogs", "pgp_passphrase_prompt", default="Enter the passphrase of the secret key for {name}.", name=name),
        lang.t("dialogs", "pgp_passphrase_title", default="Passphrase required"),
    )
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return dialog.GetValue()
    finally:
        dialog.Destroy()


class KeyManagerDialog(wx.Dialog):
    def __init__(self, parent, store, identities):
        super().__init__(
            parent, title=_title(), size=(640, 540),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.store = store
        self.identities = list(identities or [])
        self._rows = []
        self._busy = False

        sizer = wx.BoxSizer(wx.VERTICAL)
        list_label = wx.StaticText(self, label=_title())
        list_label.SetFont(list_label.GetFont().Bold())
        self.key_list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.key_list.SetName(_title())
        self.key_list.Bind(wx.EVT_LISTBOX, lambda event: self._show_details())
        self.key_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_props)
        sizer.Add(list_label, 0, wx.ALL, 8)
        sizer.Add(self.key_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        details_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_details", default="Key Properties"))
        self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.details.SetName(lang.t("dialogs", "pgp_details", default="Key Properties"))
        self.details.SetMinSize((-1, self.details.GetCharHeight() * 7))
        sizer.Add(details_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._buttons = {}
        for key, label, handler in (
            ("generate", lang.control_label("pgp_generate", "&New Key Pair"), self._on_generate),
            ("import_file", lang.control_label("pgp_import_file", "&Import OpenPGP Key File"), self._on_import_file),
            ("import_clip", lang.control_label("pgp_import_clip", "Import Key(s) From Clip&board"), self._on_import_clip),
            ("discover", lang.control_label("pgp_discover", "Disc&over Keys Online"), self._on_discover),
            ("export", lang.control_label("pgp_export", "&Export Public Key To File"), self._on_export),
            ("copy", lang.control_label("pgp_copy", "&Copy Public Key"), self._on_copy),
            ("backup", lang.control_label("pgp_backup", "Back&up Secret Key To File"), self._on_backup),
            ("publish", lang.control_label("pgp_publish", "Publis&h"), self._on_publish),
            ("props", lang.control_label("pgp_props", "Key &Properties"), self._on_props),
            ("revoke", lang.control_label("pgp_revoke", "&Revoke Key"), self._on_revoke),
            ("delete", lang.control_label("pgp_delete", "&Delete Key"), self._on_delete),
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
        wx.CallAfter(self.key_list.SetFocus)

    # --- List ---------------------------------------------------------

    def _populate(self, select=None):
        current = self._selected()
        select = select or (current["fingerprint"] if current else None)
        try:
            self._rows = self.store.entries()
        except openpgp.OpenPGPError as exc:
            self._error(exc)
            self._rows = []
        self.key_list.Clear()
        if not self._rows:
            self.key_list.Append(lang.t("dialogs", "pgp_no_keys", default="No keys yet. Generate a new key pair or import a key."))
            self.key_list.SetSelection(0)
            self.details.SetValue("")
            self._sync_buttons()
            return
        for entry in self._rows:
            self.key_list.Append(row_text(entry))
        index = 0
        for row, entry in enumerate(self._rows):
            if entry["fingerprint"] == select:
                index = row
        self.key_list.SetSelection(index)
        self._show_details()

    def _selected(self):
        index = self.key_list.GetSelection() if hasattr(self, "key_list") else -1
        if index == wx.NOT_FOUND or index < 0 or index >= len(self._rows):
            return None
        return self._rows[index]

    def _show_details(self):
        entry = self._selected()
        self.details.SetValue(details_text(entry) if entry else "")
        self._sync_buttons()

    def _sync_buttons(self):
        entry = self._selected()
        has = entry is not None and not self._busy
        secret = has and entry.get("secret")
        for key in ("export", "copy", "props", "delete"):
            self._buttons[key].Enable(has)
        self._buttons["backup"].Enable(bool(secret))
        self._buttons["publish"].Enable(bool(secret))
        self._buttons["revoke"].Enable(bool(secret) and not entry.get("revoked"))
        for key in ("generate", "import_file", "import_clip", "discover"):
            self._buttons[key].Enable(not self._busy)

    def _need_selection(self):
        entry = self._selected()
        if entry is None:
            notice_toast.notify(
                self,
                lang.t("dialogs", "pgp_select_key", default="Select a key first."),
                _title(),
            )
        return entry

    def _error(self, exc):
        wx.MessageBox(
            lang.t("dialogs", "pgp_save_failed", default="The keys could not be saved: {error}", error=str(exc)),
            _title(), wx.OK | wx.ICON_ERROR, self,
        )

    def _done(self, key, default):
        notice_toast.notify(
            self,
            _t(key, default),
            _title(),
        )

    @staticmethod
    def _label(entry):
        return entry.get("name") or entry.get("email") or openpgp.key_id(entry["fingerprint"])

    # --- Generate -----------------------------------------------------

    def _on_generate(self, event):
        if not self.identities:
            notice_toast.notify(
                self,
                lang.t("dialogs", "pgp_gen_no_identity", default="Add an email account first."),
                _title(),
            )
            return
        dialog = GenerateDialog(self, self.identities)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name, email, suite, years = dialog.result()
        finally:
            dialog.Destroy()
        identity = openpgp.make_user_id(name, email)
        if wx.MessageBox(
            lang.t("dialogs", "pgp_gen_confirm", default="Generate public and secret key for {identity}?", identity=identity),
            lang.t("dialogs", "pgp_gen_title", default="Generate OpenPGP Key"), wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        self._busy = True
        self._sync_buttons()
        wx.BeginBusyCursor()
        self.details.SetValue(lang.t("dialogs", "pgp_gen_progress", default="Generating your new OpenPGP Key…"))

        def work():
            try:
                result = ("ok", self.store.generate(name, email, suite=suite, years=years))
            except Exception as exc:  # noqa: BLE001 - reported below
                logger.exception("OpenPGP key generation failed.")
                result = ("error", exc)
            wx.CallAfter(self._generated, result)

        threading.Thread(target=work, name="openpgp-keygen", daemon=True).start()

    def _generated(self, result):
        if wx.IsBusy():
            wx.EndBusyCursor()
        self._busy = False
        if not self:
            return
        if result[0] == "ok":
            self._populate(select=result[1])
            self._offer_publish(result[1])
        else:
            self._populate()
            wx.MessageBox(
                "%s\n%s" % (lang.t("dialogs", "pgp_gen_failed", default="OpenPGP Key generation unexpectedly failed"), result[1]),
                _title(), wx.OK | wx.ICON_ERROR, self,
            )
        self.key_list.SetFocus()

    # --- Publish ------------------------------------------------------

    def _offer_publish(self, fingerprint):
        """New Key Pair succeeded: one question, Yes the default, as
        Thunderbird offers to publish a new key."""
        entry = next((row for row in self._rows if row["fingerprint"] == fingerprint), None)
        addresses = entry.get("email") if entry else ""
        question = "%s\n\n%s\n%s" % (
            lang.t("dialogs", "pgp_gen_done", default="OpenPGP Key created successfully!"),
            lang.t("dialogs", "pgp_publish_suggest",
                   default="Publishing the public key on a keyserver allows others to discover it."),
            lang.t("dialogs", "pgp_publish_question",
                   default="Publish it on {keyserver} now? The server then sends a verification email "
                           "to {addresses}. Your address becomes findable once you open its link.",
                   keyserver=openpgp.KEYSERVER, addresses=addresses),
        )
        if wx.MessageBox(question, _title(), wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION, self) == wx.YES:
            self._publish(fingerprint)

    def _on_publish(self, event):
        entry = self._need_selection()
        if entry is not None and entry.get("secret"):
            self._publish(entry["fingerprint"])

    def _publish(self, fingerprint):
        self._busy = True
        self._sync_buttons()
        wx.BeginBusyCursor()
        locale = openpgp.vks_locale(getattr(lang, "_code", "en"))

        def work():
            try:
                result = ("ok", self.store.publish(fingerprint, locale=locale))
            except Exception as exc:  # noqa: BLE001 - reported below
                logger.warning("Publishing key %s failed: %s", fingerprint, exc)
                result = ("error", exc)
            wx.CallAfter(self._published, result)

        threading.Thread(target=work, name="openpgp-publish", daemon=True).start()

    def _published(self, result):
        if wx.IsBusy():
            wx.EndBusyCursor()
        self._busy = False
        if not self:
            return
        self._sync_buttons()
        if result[0] != "ok":
            wx.MessageBox(
                "%s\n%s" % (lang.t("dialogs", "pgp_publish_fail",
                                   default="Failed to send your public key to \"{keyserver}\".",
                                   keyserver=openpgp.KEYSERVER), result[1]),
                _title(), wx.OK | wx.ICON_ERROR, self,
            )
            return
        lines = [lang.t("dialogs", "pgp_publish_ok", default="Public key sent to \"{keyserver}\".",
                        keyserver=openpgp.KEYSERVER)]
        sent = result[1].get("sent") or result[1].get("pending") or []
        if sent:
            lines.append(lang.t(
                "dialogs", "pgp_publish_sent",
                default="A verification email was sent to {addresses}. Open the link in it to make "
                        "your key findable by your address.",
                addresses=", ".join(sent),
            ))
        if result[1].get("published"):
            lines.append(lang.t("dialogs", "pgp_publish_already", default="Already findable by {addresses}.",
                                addresses=", ".join(result[1]["published"])))
        wx.MessageBox("\n\n".join(lines), _title(), wx.OK | wx.ICON_INFORMATION, self)
        self.key_list.SetFocus()

    # --- Import -------------------------------------------------------

    def _on_import_file(self, event):
        dialog = wx.FileDialog(
            self, lang.t("dialogs", "pgp_import_file_title", default="Import OpenPGP Key File"),
            wildcard="*.asc;*.gpg;*.pgp;*.key;*.txt|*.asc;*.gpg;*.pgp;*.key;*.txt|*.*|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        try:
            if os.path.getsize(path) > openpgp.MAX_IMPORT_BYTES:
                raise openpgp.OpenPGPError("larger than 5 MB")
            with open(path, "rb") as handle:
                raw = handle.read()
        except (OSError, openpgp.OpenPGPError) as exc:
            self._error(exc)
            return
        self._import_bytes(raw)

    def _on_import_clip(self, event):
        text = ""
        if wx.TheClipboard.Open():
            try:
                data = wx.TextDataObject()
                if wx.TheClipboard.GetData(data):
                    text = data.GetText()
            finally:
                wx.TheClipboard.Close()
        if "-----BEGIN PGP" not in text:
            notice_toast.notify(
                self,
                lang.t("dialogs", "pgp_clip_empty", default="The clipboard holds no OpenPGP key."),
                _title(),
            )
            return
        self._import_bytes(text.encode("utf-8"))

    def _import_bytes(self, raw):
        import_key_bytes(
            self, raw, store=self.store,
            on_imported=lambda found: self._populate(select=found[0]["fingerprint"]),
        )

    def _on_discover(self, event):
        """Discover Keys Online, as in Thunderbird's key manager: the Web Key
        Directory and keys.openpgp.org, by address (openpgp_compose)."""
        import openpgp_compose

        imported = openpgp_compose.discover_dialog(self, self.store)
        if imported:
            self._populate(select=imported[0]["fingerprint"])

    # --- Export, copy, backup ----------------------------------------

    def _save_file(self, default_name, data):
        dialog = wx.FileDialog(
            self, _title(), defaultFile=default_name,
            wildcard="*.asc|*.asc|*.*|*.*", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return False
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            self._error(exc)
            return False
        return True

    def _on_export(self, event):
        entry = self._need_selection()
        if entry is None:
            return
        data = self.store.public_armored(entry["fingerprint"]).encode("ascii")
        name = _safe_name(entry.get("email") or entry.get("name")) + "-public.asc"
        if self._save_file(name, data):
            self._done("pgp_export_done", "Public Key successfully exported!")

    def _on_copy(self, event):
        entry = self._need_selection()
        if entry is None:
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(self.store.public_armored(entry["fingerprint"])))
            finally:
                wx.TheClipboard.Close()
            self._done("pgp_copied", "Key(s) copied to clipboard")

    def _on_backup(self, event):
        entry = self._need_selection()
        if entry is None or not entry.get("secret"):
            return
        dialog = BackupPasswordDialog(self)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            password = dialog.password()
        finally:
            dialog.Destroy()
        try:
            data = self.store.backup(entry["fingerprint"], password)
        except openpgp.OpenPGPError as exc:
            self._error(exc)
            return
        name = _safe_name(entry.get("email") or entry.get("name")) + "-secret-backup.asc"
        if self._save_file(name, data):
            self._done("pgp_backup_done", "Secret Key successfully exported!")

    # --- Properties, revoke, delete ----------------------------------

    def _on_props(self, event):
        entry = self._need_selection()
        if entry is None:
            return
        dialog = PropertiesDialog(self, entry)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            acceptance, personal = dialog.result()
        finally:
            dialog.Destroy()
        try:
            self.store.set_flags(entry["fingerprint"], acceptance=acceptance, personal=personal)
        except openpgp.OpenPGPError as exc:
            self._error(exc)
            return
        self._populate(select=entry["fingerprint"])

    def _on_revoke(self, event):
        entry = self._need_selection()
        if entry is None or not entry.get("secret"):
            return
        if wx.MessageBox(
            lang.t("dialogs", "pgp_revoke_confirm", default="Revoke the key of {name}? A revoked key can never be used again. "
               "Share the revoked public key so others stop using it.", name=self._label(entry)),
            _title(), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self,
        ) != wx.YES:
            return
        passphrase = None
        if self.store.is_locked(entry["fingerprint"]):
            passphrase = _ask_passphrase(self, self._label(entry))
            if passphrase is None:
                return
        try:
            self.store.revoke(entry["fingerprint"], passphrase)
        except openpgp.OpenPGPError as exc:
            self._error(exc)
            return
        self._populate(select=entry["fingerprint"])
        self._done("pgp_revoke_done", "Key successfully revoked.")

    def _on_delete(self, event):
        entry = self._need_selection()
        if entry is None:
            return
        if entry.get("secret"):
            question = lang.t("dialogs", "pgp_delete_secret_confirm", default="Delete your personal key {name}? Mail encrypted to it can never be read again. "
                "Back up the secret key first if you may need it.", name=self._label(entry),
            )
        else:
            question = lang.t("dialogs", "pgp_delete_public_confirm", default="Delete the public key of {name}?", name=self._label(entry))
        if wx.MessageBox(question, _title(), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
            return
        try:
            self.store.delete(entry["fingerprint"])
        except openpgp.OpenPGPError as exc:
            self._error(exc)
            return
        self._populate()


class GenerateDialog(wx.Dialog):
    """Generate OpenPGP Key: identity, key type and expiry."""

    def __init__(self, parent, identities):
        super().__init__(parent, title=lang.t("dialogs", "pgp_gen_title", default="Generate OpenPGP Key"),
                         style=wx.DEFAULT_DIALOG_STYLE)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.identities = list(identities)
        sizer = wx.BoxSizer(wx.VERTICAL)

        identity_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_gen_identity", default="Identity"))
        self.identity_choice = wx.Choice(
            self, choices=[openpgp.make_user_id(name, email) for name, email in self.identities],
        )
        self.identity_choice.SetName(lang.t("dialogs", "pgp_gen_identity", default="Identity"))
        self.identity_choice.SetSelection(0)
        sizer.Add(identity_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        sizer.Add(self.identity_choice, 0, wx.EXPAND | wx.ALL, 8)

        type_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_gen_type", default="Key type:"))
        sizer.Add(type_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.ecc_radio = wx.RadioButton(self, label=lang.t("dialogs", "pgp_gen_ecc", default="ECC (Elliptic Curve)"), style=wx.RB_GROUP)
        self.rsa3_radio = wx.RadioButton(self, label="RSA 3072")
        self.rsa4_radio = wx.RadioButton(self, label="RSA 4096")
        for radio in (self.ecc_radio, self.rsa3_radio, self.rsa4_radio):
            sizer.Add(radio, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.ecc_radio.SetValue(True)

        expiry_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_gen_expiry", default="Key expiry"))
        sizer.Add(expiry_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.years_radio = wx.RadioButton(self, label=lang.t("dialogs", "pgp_gen_expires_3y", default="Key expires in 3 years"), style=wx.RB_GROUP)
        self.never_radio = wx.RadioButton(self, label=lang.t("dialogs", "pgp_gen_no_expiry", default="Key does not expire"))
        sizer.Add(self.years_radio, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.never_radio, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.years_radio.SetValue(True)

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, lang.t("dialogs", "pgp_gen_button", default="Generate key"))
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        wx.CallAfter(self.identity_choice.SetFocus)

    def result(self):
        name, email = self.identities[max(0, self.identity_choice.GetSelection())]
        suite = "rsa3072" if self.rsa3_radio.GetValue() else "rsa4096" if self.rsa4_radio.GetValue() else "cv25519"
        return name, email, suite, (3 if self.years_radio.GetValue() else None)


class PropertiesDialog(wx.Dialog):
    """Key Properties: the details, and your acceptance of a
    correspondent's key, or whether your own key is a personal key."""

    def __init__(self, parent, entry):
        super().__init__(parent, title=lang.t("dialogs", "pgp_details", default="Key Properties"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        self.entry = entry
        sizer = wx.BoxSizer(wx.VERTICAL)
        details_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_details", default="Key Properties"))
        details = wx.TextCtrl(self, value=details_text(entry),
                              style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        details.SetName(lang.t("dialogs", "pgp_details", default="Key Properties"))
        details.SetMinSize((460, details.GetCharHeight() * 7))
        sizer.Add(details_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        sizer.Add(details, 1, wx.EXPAND | wx.ALL, 8)

        self.radios = {}
        if entry.get("secret"):
            for index, (value, label) in enumerate((
                (True, lang.t("dialogs", "pgp_personal_yes", default="Yes, treat this key as a personal key.")),
                (False, lang.t("dialogs", "pgp_personal_no", default="No, don’t use it as my personal key.")),
            )):
                radio = wx.RadioButton(self, label=label, style=wx.RB_GROUP if index == 0 else 0)
                radio.SetValue(bool(entry.get("personal")) == value)
                sizer.Add(radio, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
                self.radios[value] = radio
            if entry.get("revoked"):
                self.radios[True].Enable(False)
        else:
            acceptance_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_acceptance", default="Your Acceptance"))
            sizer.Add(acceptance_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
            for index, value in enumerate(("rejected", "undecided", "unverified", "verified")):
                radio = wx.RadioButton(self, label=_acceptance_text(value), style=wx.RB_GROUP if index == 0 else 0)
                radio.SetValue(entry.get("acceptance", "undecided") == value)
                sizer.Add(radio, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
                self.radios[value] = radio
            if entry.get("email"):
                hint = wx.StaticText(self, label=lang.t("dialogs", "pgp_verify_hint", default="Verify the fingerprint of the key using a secure communication channel other than "
                    "email to make sure that it’s really the key of {addr}.", addr=entry["email"],
                ))
                wrap_text(hint)
                sizer.Add(hint, 0, wx.EXPAND | wx.ALL, 12)

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        first = next(iter(radio for radio in self.radios.values() if radio.GetValue()), None)
        wx.CallAfter((first or details).SetFocus)

    def result(self):
        chosen = next((value for value, radio in self.radios.items() if radio.GetValue()), None)
        if self.entry.get("secret"):
            return None, bool(chosen)
        return chosen, None


class BackupPasswordDialog(wx.Dialog):
    """Choose a password to backup your OpenPGP Key."""

    def __init__(self, parent):
        super().__init__(parent, title=lang.t("dialogs", "pgp_backup_title", default="Choose a password to backup your OpenPGP Key"),
                         style=wx.DEFAULT_DIALOG_STYLE)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        sizer = wx.BoxSizer(wx.VERTICAL)
        message = wx.StaticText(self, label=lang.t("dialogs", "pgp_backup_message", default="The password you set here protects the OpenPGP secret key backup file that you are about "
            "to create. You must set this password to proceed with the backup.",
        ))
        wrap_text(message)
        sizer.Add(message, 0, wx.EXPAND | wx.ALL, 12)
        first_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_backup_pw", default="Secret Key backup password:"))
        self.first = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self.first.SetName(lang.t("dialogs", "pgp_backup_pw", default="Secret Key backup password:"))
        second_label = wx.StaticText(self, label=lang.t("dialogs", "pgp_backup_pw2", default="Secret Key backup password (again):"))
        self.second = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self.second.SetName(lang.t("dialogs", "pgp_backup_pw2", default="Secret Key backup password (again):"))
        for label, field in ((first_label, self.first), (second_label, self.second)):
            sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
            sizer.Add(field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        wx.CallAfter(self.first.SetFocus)

    def _on_ok(self, event):
        if not self.first.GetValue() or self.first.GetValue() != self.second.GetValue():
            wx.MessageBox(lang.t("dialogs", "pgp_pw_mismatch", default="The two passwords do not match."), self.GetTitle(),
                          wx.OK | wx.ICON_WARNING, self)
            self.first.SetFocus()
            return
        event.Skip()

    def password(self):
        return self.first.GetValue()


def import_key_bytes(parent, raw, store=None, paths=None, on_imported=None):
    """
    Imports the OpenPGP keys in raw, as Import OpenPGP Key File does: a
    secret key backup asks its password, the keys found are listed and
    confirmed, a locked secret key asks its passphrase. Shared by the key
    manager and a message's Import OpenPGP Key. on_imported(found) runs
    after the import and before the closing message. Returns the
    imported previews, or [].
    """
    title = lang.t("dialogs", "pgp_title", default="OpenPGP Key Manager")
    if store is None:
        if not openpgp.available():
            notice_toast.notify(
                parent,
                lang.t("dialogs", "pgp_unavailable", default="OpenPGP is not available in this copy of ZBox."),
                title,
            )
            return []
        try:
            store = openpgp.KeyStore(paths)
        except openpgp.OpenPGPError as exc:
            wx.MessageBox(
                lang.t("dialogs", "pgp_save_failed", default="The keys could not be saved: {error}", error=str(exc)),
                title, wx.OK | wx.ICON_ERROR, parent,
            )
            return []
    if store.is_backup(raw):
        dialog = wx.PasswordEntryDialog(
            parent,
            lang.t("dialogs", "pgp_backup_open", default="This file is a secret key backup. Enter its backup password."),
            lang.t("dialogs", "pgp_passphrase_title", default="Passphrase required"),
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return []
            password = dialog.GetValue()
        finally:
            dialog.Destroy()
        try:
            raw = store.open_backup(raw, password)
        except openpgp.OpenPGPError:
            wx.MessageBox(lang.t("dialogs", "pgp_unlock_failed", default="The key, or subordinate parts of the key, could not be unlocked."),
                          title, wx.OK | wx.ICON_ERROR, parent)
            return []
    try:
        found = store.preview(raw)
    except openpgp.OpenPGPError as exc:
        logger.info("Nothing importable: %s", exc)
        found = []
    if not found:
        notice_toast.notify(
            parent,
            lang.t("dialogs", "pgp_import_none", default="No keys imported."),
            title,
        )
        return []
    listing = "\n".join(row_text(dict(item["info"], personal=item["secret"] is not None)) for item in found)
    if wx.MessageBox(
        lang.t("dialogs", "pgp_import_confirm", default="Import these keys?\n\n{keys}", keys=listing),
        title, wx.YES_NO | wx.ICON_QUESTION, parent,
    ) != wx.YES:
        return []
    skip = set()
    for item in found:
        if not item["locked"]:
            continue
        name = item["info"].get("name") or item["info"].get("email") or item["fingerprint"]
        while True:
            passphrase = _ask_passphrase(parent, name)
            if passphrase is None:
                skip.add(item["fingerprint"])
                break
            if store.unlocks(item, passphrase):
                break
            wx.MessageBox(lang.t("dialogs", "pgp_unlock_failed", default="The key, or subordinate parts of the key, could not be unlocked."),
                          title, wx.OK | wx.ICON_ERROR, parent)
    try:
        store.commit(found, skip_secret=skip)
    except openpgp.OpenPGPError as exc:
        wx.MessageBox(
            lang.t("dialogs", "pgp_save_failed", default="The keys could not be saved: {error}", error=str(exc)),
            title, wx.OK | wx.ICON_ERROR, parent,
        )
        return []
    if on_imported is not None:
        on_imported(found)
    notice_toast.notify(
        parent,
        lang.t("dialogs", "pgp_import_done", default="OpenPGP Keys imported successfully!"),
        title,
    )
    return found
