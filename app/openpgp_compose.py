"""
OpenPGP stages 3 and 4: the questions and settings around sending.

- settings_for: an identity's End-to-End Encryption settings over the
  defaults (account_manager.OPENPGP_DEFAULTS).
- EncryptionDialog / edit_settings: Thunderbird's End-to-End Encryption
  page for one address, reached from Account and Identities Settings and
  from the identity dialog.
- plan_for_send: what Send asks before an OpenPGP message goes, and the
  openpgp_send.Plan it builds: your personal key, its passphrase once per
  session, recipients without an accepted key (keys collected from their
  own mail first, then a lookup online, asked each time), then Send
  Without Encryption or Cancel.
- draft_key: the key an encrypted draft is saved with.
- discover_dialog: Discover Keys Online in the key manager.

Every PySequoia call stays in openpgp.py; nothing here runs at import.
"""

import logging
import threading

import wx

import lang
import notice_toast
import openpgp
from accessible import fit_dialog, make_read_only_viewer, wrap_text

logger = logging.getLogger("zbox.openpgp")


def _t(key, default, **values):
    return lang.t("dialogs", key, default=default, **values)


def _defaults():
    from account_manager import OPENPGP_DEFAULTS

    return dict(OPENPGP_DEFAULTS)


def settings_for(account, email):
    """The OpenPGP sending settings for sending as email from account."""
    finder = getattr(account, "openpgp_settings", None) if account is not None else None
    if finder is None:
        return _defaults()
    try:
        return finder(email)
    except Exception:  # noqa: BLE001 - an odd account sends as before
        return _defaults()


def _store(paths):
    """The key list, or None when this copy has no OpenPGP or the list
    cannot be opened (logged)."""
    if not openpgp.available():
        return None
    try:
        return openpgp.KeyStore(paths)
    except openpgp.OpenPGPError as exc:
        logger.warning("The OpenPGP key list could not be opened: %s", exc)
        return None


def _find_paths(window):
    while window is not None:
        paths = getattr(window, "paths", None)
        if paths is not None:
            return paths
        window = window.GetParent()
    top = wx.GetApp().GetTopWindow() if wx.GetApp() else None
    return getattr(top, "paths", None)


def _title():
    return _t("pgp_e2e_title", "End-to-end Encryption")


# --- End-to-End Encryption settings -------------------------------------

class EncryptionDialog(wx.Dialog):
    """One address's End-to-End Encryption, as Thunderbird's page has it:
    the personal key, encryption for new messages, signing of unencrypted
    ones, and the Autocrypt header."""

    def __init__(self, parent, address, settings, keys):
        super().__init__(parent, title=_title(), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        values = _defaults()
        values.update(settings or {})
        sizer = wx.BoxSizer(wx.VERTICAL)

        if not keys:
            note = wx.StaticText(self, label=_t(
                "pgp_e2e_no_key",
                "ZBox has no personal OpenPGP key for {address}. Make one in Tools, OpenPGP Key Manager.",
                address=address,
            ))
            wrap_text(note)
            sizer.Add(note, 0, wx.EXPAND | wx.ALL, 10)

        key_text = _t("pgp_e2e_key_label", "Personal key for {address}", address=address)
        key_label = wx.StaticText(self, label=key_text)
        self._choices = ["", "none"]
        labels = [
            _t("pgp_e2e_auto", "Automatic: your personal key for this address"),
            _t("pgp_e2e_none", "Do not use OpenPGP for this identity."),
        ]
        from openpgp_key_manager import row_text

        for entry in keys:
            self._choices.append(entry["fingerprint"])
            labels.append(row_text(entry))
        chosen = str(values.get("key") or "")
        if chosen and chosen not in self._choices:
            self._choices.append(chosen)
            labels.append(_t("pgp_e2e_key_missing", "Key {keyid}, not found", keyid="0x" + openpgp.key_id(chosen)))
        self.key_choice = wx.Choice(self, choices=labels)
        self.key_choice.SetName(key_text)
        self.key_choice.SetSelection(self._choices.index(chosen) if chosen in self._choices else 0)
        sizer.Add(key_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(self.key_choice, 0, wx.EXPAND | wx.ALL, 10)

        self.encrypt_choice = wx.RadioBox(
            self,
            label=_t("pgp_e2e_new_messages", "Encryption for new messages"),
            choices=[
                _t("pgp_e2e_disable", "Disable encryption for new messages"),
                _t("pgp_e2e_enable", "Enable encryption for new messages"),
            ],
            majorDimension=1, style=wx.RA_SPECIFY_COLS,
        )
        self.encrypt_choice.SetSelection(1 if values.get("encrypt") else 0)
        sizer.Add(self.encrypt_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        enable_note = wx.StaticText(self, label=_t(
            "pgp_e2e_enable_info", "You will be able to disable encryption for individual messages.",
        ))
        wrap_text(enable_note)
        sizer.Add(enable_note, 0, wx.EXPAND | wx.ALL, 10)

        self.sign_box = wx.CheckBox(self, label=_t("pgp_e2e_sign", "Sign unencrypted messages"))
        self.sign_box.SetValue(bool(values.get("sign")))
        sizer.Add(self.sign_box, 0, wx.LEFT | wx.RIGHT, 10)
        signing_note = wx.StaticText(self, label=_t(
            "pgp_e2e_signing_info",
            "A digital signature allows recipients to verify that the message was sent by you and its "
            "content was not changed. Encrypted messages are always signed by default.",
        ))
        wrap_text(signing_note)
        sizer.Add(signing_note, 0, wx.EXPAND | wx.ALL, 10)

        self.autocrypt_box = wx.CheckBox(self, label=_t(
            "pgp_e2e_autocrypt",
            "Send OpenPGP public key(s) in the email headers for compatibility with Autocrypt",
        ))
        self.autocrypt_box.SetValue(bool(values.get("autocrypt", True)))
        sizer.Add(self.autocrypt_box, 0, wx.ALL, 10)

        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        fit_dialog(self, sizer)
        wx.CallAfter(self.key_choice.SetFocus)

    def result(self):
        index = self.key_choice.GetSelection()
        return {
            "key": self._choices[index] if 0 <= index < len(self._choices) else "",
            "encrypt": self.encrypt_choice.GetSelection() == 1,
            "sign": self.sign_box.GetValue(),
            "autocrypt": self.autocrypt_box.GetValue(),
        }


def _personal_entries(paths, address):
    """Your personal keys, those holding address first."""
    store = _store(paths) if paths is not None else None
    if store is None:
        return []
    try:
        rows = [row for row in store.entries() if row.get("personal") and row.get("secret")]
    except openpgp.OpenPGPError:
        return []
    wanted = str(address or "").strip().lower()
    rows.sort(key=lambda row: not any(
        openpgp.split_user_id(uid)[1].lower() == wanted for uid in row.get("user_ids") or []
    ))
    return rows


def edit_settings(parent, address, current):
    """Shows EncryptionDialog for address; the new settings, or None on
    Cancel. Nothing is saved here: the settings dialog holds them until
    its own Save."""
    dialog = EncryptionDialog(parent, address, current, _personal_entries(_find_paths(parent), address))
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return dialog.result()
    finally:
        dialog.Destroy()


# --- Busy while a lookup runs ---------------------------------------------

class _BusyDialog(wx.Dialog):
    """Shown modally while keys are looked up online, on a thread; closed
    by finish() when the lookup is done. A focused read-only field, so a
    screen reader says it at once (as compose_panel._SavingDraftDialog)."""

    def __init__(self, parent, text):
        super().__init__(parent, title=_title(), style=wx.CAPTION)
        if lang.is_rtl():
            self.SetLayoutDirection(wx.Layout_RightToLeft)
        sizer = wx.BoxSizer(wx.VERTICAL)
        field = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE)
        field.SetName(_title())
        make_read_only_viewer(field)
        field.SetMinSize((self.GetTextExtent("x" * 40).width, field.GetCharHeight() * 2))
        sizer.Add(field, 1, wx.EXPAND | wx.ALL, 16)
        fit_dialog(self, sizer, min_width_chars=40)

    def finish(self):
        if self.IsModal():
            self.EndModal(wx.ID_OK)


def _lookup(parent, store, addresses):
    """Every key found online for addresses, while a busy dialog shows."""
    dialog = _BusyDialog(parent, _t("pgp_searching", "Searching…"))
    outcome = {}

    def done(found):
        # On the UI thread, inside the dialog's own modal loop: CallAfter
        # only runs there, so the dialog is always shown before this ends it.
        outcome["found"] = found
        dialog.finish()

    def work():
        found = []
        for address in addresses:
            try:
                found.extend(openpgp.lookup_online(store, address))
            except Exception as exc:  # noqa: BLE001 - nothing found for this one
                logger.info("Looking up a key online failed: %s", type(exc).__name__)
        wx.CallAfter(done, found)

    threading.Thread(target=work, name="openpgp-lookup", daemon=True).start()
    dialog.ShowModal()
    dialog.Destroy()
    return outcome.get("found", [])


def _source_text(source):
    if source == "autocrypt":
        return _t("pgp_source_autocrypt", "Autocrypt header")
    if source == "wkd":
        return _t("pgp_source_wkd", "Web Key Directory")
    return _t("pgp_source_keyserver", "keyserver")


def _listing(found):
    from openpgp_key_manager import row_text

    lines = []
    for item in found:
        lines.append(row_text(dict(item["info"], personal=False)))
        lines.append("%s: %s" % (_t("pgp_fingerprint", "Fingerprint"), openpgp.fingerprint_text(item["fingerprint"])))
        lines.append(_t("pgp_source", "Source: {source}", source=_source_text(item.get("source"))))
        lines.append("")
    return "\n".join(lines).strip()


def _ask_accept(parent, intro, found):
    """Lists keys found and asks Thunderbird's question. True on Yes; the
    caller then accepts them as unverified."""
    text = "%s\n\n%s\n\n%s" % (intro, _listing(found), _t(
        "pgp_do_you_accept", "Do you accept this key for verifying digital signatures and for encrypting messages?",
    ))
    if wx.MessageBox(text, _title(), wx.YES_NO | wx.ICON_QUESTION, parent) != wx.YES:
        return False
    return True


def resolve_missing(parent, store, addresses):
    """Keys for recipients who have none: first those collected from their
    own mail, then, asked each time, a lookup online. Returns the addresses
    still without a usable, accepted key."""
    collected = []
    for address in addresses:
        collected.extend(openpgp.collected_preview(store, address))
    if collected and _ask_accept(parent, _t(
        "pgp_collected_found", "Keys for these recipients were found in mail they sent you:",
    ), collected):
        try:
            openpgp.accept_keys(store, collected)
        except openpgp.OpenPGPError as exc:
            _error(parent, exc)
    remaining = openpgp.missing_keys(store, addresses)
    if not remaining:
        return []
    question = _t(
        "pgp_lookup_q",
        "Look up keys online for these recipients?\n\n{addresses}\n\nThis sends their addresses to "
        "keys.openpgp.org and to their own mail domains.",
        addresses="\n".join(remaining),
    )
    if wx.MessageBox(question, _title(), wx.YES_NO | wx.ICON_QUESTION, parent) != wx.YES:
        return remaining
    found = _lookup(parent, store, remaining)
    if not found:
        notice_toast.notify(
            parent,
            _t("pgp_no_key_found", "We couldn’t find any usable key matching the specified search criteria."),
            _title(),
        )
        return remaining
    if _ask_accept(parent, _t("pgp_found_online", "Keys found online:"), found):
        try:
            openpgp.accept_keys(store, found)
        except openpgp.OpenPGPError as exc:
            _error(parent, exc)
    return openpgp.missing_keys(store, addresses)


def _error(parent, exc):
    wx.MessageBox(
        _t("pgp_save_failed", "The keys could not be saved: {error}", error=str(exc)),
        _title(), wx.OK | wx.ICON_ERROR, parent,
    )


def _unlock(parent, store, fingerprint):
    """Asks a locked personal key's passphrase once per session. True when
    it is open."""
    from openpgp_key_manager import _ask_passphrase

    while openpgp.needs_unlock(store, fingerprint):
        passphrase = _ask_passphrase(parent, openpgp.key_name(store, fingerprint))
        if passphrase is None:
            return False
        if openpgp.passphrase_opens(store, fingerprint, passphrase):
            openpgp.remember_passphrase(fingerprint, passphrase)
            return True
        wx.MessageBox(
            _t("pgp_unlock_failed", "The key, or subordinate parts of the key, could not be unlocked."),
            _t("pgp_passphrase_title", "Passphrase required"), wx.OK | wx.ICON_ERROR, parent,
        )
    return True


def _not_set_up(parent, email):
    wx.MessageBox(
        _t("pgp_not_set_up", "You are not set up to send end-to-end encrypted messages from {addr}.", addr=email),
        _title(), wx.OK | wx.ICON_WARNING, parent,
    )


def plan_for_send(parent, paths, account, email, recipients, encrypt, sign, attach_key):
    """The openpgp_send.Plan for one message, after any question asked, or
    None when the send is cancelled (already said). A message with none of
    Encrypt, Digitally Sign and Attach My Public Key gets only the Autocrypt
    header, and only when the identity has a personal key; with no key, or
    without OpenPGP in this copy, an empty Plan, so it is sent exactly as
    before."""
    import openpgp_send

    settings = settings_for(account, email)
    wanted = bool(encrypt or sign or attach_key)
    if not wanted and not settings.get("autocrypt", True):
        return openpgp_send.Plan()
    if not openpgp.available():
        if wanted:
            wx.MessageBox(_t("pgp_unavailable", "OpenPGP is not available in this copy of ZBox."),
                          _title(), wx.OK | wx.ICON_WARNING, parent)
            return None
        return openpgp_send.Plan()
    try:
        store = openpgp.KeyStore(paths)
    except openpgp.OpenPGPError as exc:
        if wanted:
            wx.MessageBox(_t("pgp_keys_unreadable", "The OpenPGP keys could not be opened: {error}", error=str(exc)),
                          _title(), wx.OK | wx.ICON_ERROR, parent)
            return None
        return openpgp_send.Plan()
    own = openpgp.own_key(store, email, settings.get("key", ""))
    if own is None:
        if wanted:
            _not_set_up(parent, email)
            return None
        return openpgp_send.Plan()
    if sign and not _unlock(parent, store, own):
        return None
    targets = []
    if encrypt:
        missing = openpgp.missing_keys(store, recipients)
        if missing:
            missing = resolve_missing(parent, store, missing)
        if missing:
            dialog = wx.MessageDialog(
                parent,
                _t("pgp_missing_keys",
                   "These recipients have no usable, accepted key:\n\n{addresses}\n\n"
                   "The message cannot be sent encrypted.",
                   addresses="\n".join(missing)),
                _t("pgp_cannot_encrypt", "Cannot Encrypt"),
                wx.OK | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_WARNING,
            )
            # Cancel keeps Windows' own label, as every other plain message
            # box in ZBox does; it is the default.
            dialog.SetOKLabel(lang.control_label("pgp_send_unencrypted", "Send &Without Encryption"))
            try:
                choice = dialog.ShowModal()
            finally:
                dialog.Destroy()
            if choice != wx.ID_OK:
                return None
            encrypt = False
        else:
            seen = set()
            for address in recipients:
                fingerprint = openpgp.recipient_key(store, address)
                if fingerprint and fingerprint not in seen:
                    seen.add(fingerprint)
                    targets.append(fingerprint)
            if own not in seen:
                targets.append(own)
    autocrypt = None
    if settings.get("autocrypt", True):
        try:
            autocrypt = openpgp.autocrypt_value(store, email, own)
        except openpgp.OpenPGPError as exc:
            logger.warning("No Autocrypt header: %s", exc)
    return openpgp_send.Plan(
        store=store,
        encrypt_to=targets if encrypt else [],
        sign_with=own if sign else None,
        attach_key=own if attach_key else None,
        autocrypt=autocrypt,
    )


def draft_key(paths, account, email):
    """((store, fingerprint), "") for saving an encrypted draft, or
    (None, the reason) when it cannot be encrypted. Only the public key is
    needed, so no passphrase is ever asked for a draft."""
    if not openpgp.available():
        return None, _t("pgp_unavailable", "OpenPGP is not available in this copy of ZBox.")
    try:
        store = openpgp.KeyStore(paths)
    except openpgp.OpenPGPError as exc:
        return None, _t("pgp_keys_unreadable", "The OpenPGP keys could not be opened: {error}", error=str(exc))
    own = openpgp.own_key(store, email, settings_for(account, email).get("key", ""))
    if own is None:
        return None, _t("pgp_not_set_up", "You are not set up to send end-to-end encrypted messages from {addr}.",
                        addr=email)
    return (store, own), ""


# --- Discover Keys Online, in the key manager ----------------------------

def discover_dialog(parent, store):
    """Asks an address, looks its keys up online and imports what is found
    after asking, as Import OpenPGP Key File does. Returns the imported
    previews, or []."""
    title = _t("pgp_title", "OpenPGP Key Manager")
    dialog = wx.TextEntryDialog(
        parent,
        _t("pgp_discover_prompt",
           "Enter an email address. ZBox looks for its OpenPGP keys in the Web Key Directory of its "
           "mail domain and on keys.openpgp.org."),
        title,
    )
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return []
        address = dialog.GetValue().strip()
    finally:
        dialog.Destroy()
    if not openpgp.wkd_urls(address):
        notice_toast.notify(
            parent,
            _t("pgp_discover_bad", "Enter a whole email address, such as name@example.com."),
            title,
        )
        return []
    found = _lookup(parent, store, [address])
    if not found:
        notice_toast.notify(
            parent,
            _t("pgp_no_key_found", "We couldn’t find any usable key matching the specified search criteria."),
            title,
        )
        return []
    if wx.MessageBox(
        _t("pgp_import_confirm", "Import these keys?\n\n{keys}", keys=_listing(found)),
        title, wx.YES_NO | wx.ICON_QUESTION, parent,
    ) != wx.YES:
        return []
    try:
        store.commit(found)
    except openpgp.OpenPGPError as exc:
        _error(parent, exc)
        return []
    notice_toast.notify(
        parent,
        _t("pgp_import_done", "OpenPGP Keys imported successfully!"),
        title,
    )
    return found
