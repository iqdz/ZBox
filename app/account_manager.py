"""
Owns account records for ZBox and keeps Himalaya's own config file in
sync with them.

Each account record has two email-related fields that are allowed to
differ:
  login_email    the mailbox actually authenticated against
  identity_email the address used in the From header on send

This is what lets someone log into disroot.org as login@example.net
but send and receive as you@example.org once the provider has that
custom domain linked, matching how Thunderbird's Account Settings
lets you edit the Email field independently of the login.
"""

import json
import logging
import os
import sys
import tempfile
import time
import uuid
from types import SimpleNamespace

import data_crypto
import dpapi_secret_store
import graph_client
import himalaya_client
import legacy_keyring_store
import ms_oauth
import provider_presets
from filter_rules import FilterRule
import lang


class AccountsLocked(RuntimeError):
    """The account list could not be opened this session.

    Raised by save_accounts rather than letting a write go ahead.
    Once accounts.json is encrypted, "could not read it" and "there
    are no accounts" look identical in memory, and saving over a file
    that was merely locked would turn a recoverable state into a lost
    one.
    """


IDENTITY_ENCRYPTIONS = ("tls", "start-tls", "none")


def new_identity_id():
    """A short stable id for an extra identity that needs one: it keys
    that identity's outgoing password and its own Himalaya section, so
    renaming or reordering identities never breaks either."""
    return uuid.uuid4().hex[:8]


def _clean_identity_id(value):
    value = (value or "").strip() if isinstance(value, str) else ""
    if value and value.isalnum():
        return value
    return new_identity_id()


def _clean_port(value, default):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


# OpenPGP sending settings for one address (stage 3), as Thunderbird's
# End-to-End Encryption page has them: the personal key ("" is the personal
# key for the address, "none" no OpenPGP, else a fingerprint), encryption
# for new messages, signing of unencrypted ones, and the Autocrypt header.
OPENPGP_DEFAULTS = {"key": "", "encrypt": False, "sign": False, "autocrypt": True}


def _normalize_openpgp(value):
    """The stored form of OpenPGP settings: only what differs from
    OPENPGP_DEFAULTS, so an address without any stays exactly as before."""
    if not isinstance(value, dict):
        return {}
    clean = {}
    key = str(value.get("key") or "").strip().lower()
    if key == "none" or (40 <= len(key) <= 64 and all(c in "0123456789abcdef" for c in key)):
        clean["key"] = key
    for name in ("encrypt", "sign", "autocrypt"):
        if name in value and bool(value[name]) != OPENPGP_DEFAULTS[name]:
            clean[name] = bool(value[name])
    return clean


def _normalize_identities(entries):
    """
    Normalizes a raw extra_identities list -- from disk (from_dict)
    or from the identities editor -- to a clean list of dicts, dropping
    anything with no usable email address. Audit finding 49: additional
    identities/aliases an account can send and reply as, beyond its
    primary display_name/identity_email.

    Every entry has "display_name" and "email". The per-identity
    settings are each optional and present only when switched on, so
    an identity with none of them is exactly the two-key dict older
    builds wrote, and an accounts file from before they existed loads
    unchanged:
      own_signature, signature, signature_html   its own signature
      reply_to, always_cc, always_bcc            address fields
      own_folders, drafts_folder, sent_folder    its own folders
      own_smtp, smtp_host, smtp_port, smtp_encryption, smtp_auth,
      smtp_login, identity_id                    its own outgoing server
    identity_id is kept only with own_smtp, the one setting that needs
    a stable key (the stored password and the Himalaya section). No
    password is ever part of an entry.
    """
    result = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        email = (entry.get("email") or "").strip()
        if not email:
            continue
        clean = {
            "display_name": (entry.get("display_name") or "").strip(),
            "email": email,
        }
        if entry.get("own_signature"):
            clean["own_signature"] = True
            clean["signature"] = entry.get("signature") or ""
            clean["signature_html"] = entry.get("signature_html") or ""
        for key in ("reply_to", "always_cc", "always_bcc"):
            value = (entry.get(key) or "").strip()
            if value:
                clean[key] = value
        if entry.get("own_folders"):
            drafts = (entry.get("drafts_folder") or "").strip()
            sent = (entry.get("sent_folder") or "").strip()
            if drafts or sent:
                clean["own_folders"] = True
                if drafts:
                    clean["drafts_folder"] = drafts
                if sent:
                    clean["sent_folder"] = sent
        smtp_host = (entry.get("smtp_host") or "").strip()
        if entry.get("own_smtp") and smtp_host:
            encryption = entry.get("smtp_encryption")
            clean["own_smtp"] = True
            clean["smtp_host"] = smtp_host
            clean["smtp_port"] = _clean_port(entry.get("smtp_port"), 465)
            clean["smtp_encryption"] = encryption if encryption in IDENTITY_ENCRYPTIONS else "tls"
            clean["smtp_auth"] = "login" if entry.get("smtp_auth") == "login" else "plain"
            clean["smtp_login"] = (entry.get("smtp_login") or "").strip() or email
            clean["identity_id"] = _clean_identity_id(entry.get("identity_id"))
        openpgp = _normalize_openpgp(entry.get("openpgp"))
        if openpgp:
            clean["openpgp"] = openpgp
        result.append(clean)
    return result


def check_smtp_login(host, port, encryption, username, password, timeout=20):
    """Signs in to an outgoing server and signs out again, for an
    identity's Test Outgoing Server button. Raises on any failure;
    returns nothing on success. Python's own smtplib rather than
    Himalaya, the same way the IMAP pool connects directly: this only
    proves the host, port, encryption and login work, and needs no
    Himalaya config written first."""
    import smtplib
    import ssl

    context = ssl.create_default_context()
    if encryption == "tls":
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        server.ehlo()
        if encryption == "start-tls":
            server.starttls(context=context)
            server.ehlo()
        server.login(username, password)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - already failing or done
            server.close()


def all_identities(accounts):
    """
    Flattens every account's identities() (see Account.identities
    below) into one list of (account, display_name, email) triples,
    in account order then identity order. This is what compose_
    panel.py's From list is built from (audit finding 49): choosing
    an entry tells Send both which account to send through (and
    which SMTP config/Drafts folder) and which name/address to send
    as.
    """
    result = []
    for account in accounts:
        for display_name, email in account.identities():
            result.append((account, display_name, email))
    return result


class Account:
    def __init__(
        self,
        account_id=None,
        display_name="",
        login_email="",
        identity_email="",
        password="",
        imap_host="",
        imap_port=993,
        imap_encryption="tls",
        smtp_host="",
        smtp_port=465,
        smtp_encryption="tls",
        smtp_auth="plain",
        signature="",
        signature_html="",
        filters=None,
        junk_folder="",
        new_mail_sound_enabled=True,
        new_mail_announce_enabled=False,
        notifications_muted=False,
        extra_identities=None,
        enabled=True,
        auto_check=True,
        auth_method="password",
        openpgp=None,
    ):
        self.account_id = account_id or uuid.uuid4().hex[:12]
        self.display_name = display_name
        self.login_email = login_email
        # identity_email defaults to login_email until the person
        # sets a custom From address, exactly like Thunderbird.
        self.identity_email = identity_email or login_email
        self.password = password
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_encryption = imap_encryption
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_encryption = smtp_encryption
        # SASL mechanism for the outgoing server: "plain" (default) or
        # "login". Some servers refuse AUTH PLAIN with 504 5.5.4; the
        # send path then switches the account to LOGIN once and keeps it
        # (compose_panel._switch_smtp_to_login).
        self.smtp_auth = "login" if smtp_auth == "login" else "plain"
        # Plain text, appended to New/Reply/Forward compose bodies via
        # envelope_format._append_signature -- audit finding 41. Empty
        # by default; edited per-account in Account Settings.
        self.signature = signature
        # The same signature as HTML, written by the signature editor
        # (app/signature_dialog.py). Empty means there is no formatted
        # version, and everything falls back to converting the plain
        # text above, which is exactly what happened before this field
        # existed -- so an account saved by an older build needs no
        # migration.
        self.signature_html = signature_html or ""
        # Ordered rules that test From/To/Subject and mark
        # read/flag/move on a match -- audit finding 45. Applied
        # automatically to genuinely-new Inbox arrivals
        # (mail_fetch.py) and manually via Tools > Message
        # Filters' Run Now (filter_rules.matching_rules either
        # way). Empty by default.
        self.filters = filters if filters is not None else []
        # Per-account override of which real folder Mark as
        # Junk moves messages to (audit finding 46) -- empty
        # means fall back to the generic Junk mapping every
        # other special folder uses (see
        # envelope_format._junk_folder_for_account).
        self.junk_folder = junk_folder or ""
        # Per-account new-mail notification granularity (audit
        # finding 47) -- Settings > Sound & Notifications'
        # sounds_enabled is still the master switch (off there
        # means no sound for any account), but this account can
        # additionally opt out of just its own new-mail sound,
        # and independently opt in to a spoken announcement
        # (off by default: unlike the sound, speech interrupts).
        self.new_mail_sound_enabled = bool(new_mail_sound_enabled)
        self.new_mail_announce_enabled = bool(new_mail_announce_enabled)
        # Mute Account: a single master override for this account's
        # new-mail notifications, on top of the two flags above and
        # the desktop-notifications toggle in Settings. Muted means
        # no sound, no spoken announcement and no Windows toast for
        # this account's new mail, regardless of how those other
        # settings are configured -- unmuting restores whatever they
        # were already set to rather than forcing them back on.
        # Edited via Account Settings' "Mute notifications for this
        # account" checkbox, or the Accounts menu / account context
        # menu's "Mute Account" quick toggle. Off by default.
        self.notifications_muted = bool(notifications_muted)
        # Other addresses this same mailbox can send and reply as --
        # audit finding 49 (Gmail's own "Send As" aliases, or another
        # domain routed to the same account). Each entry is a
        # {"display_name", "email"} dict; identities() below is the
        # single place that turns this plus the primary identity into
        # the ordered list ComposePanel's From choice and the reply-
        # from-address matching in envelope_format both use.
        self.extra_identities = _normalize_identities(extra_identities)
        # Disabled accounts stay configured. The password, the
        # Himalaya config section and the tree entry all remain, and
        # the account can be switched back on at any time -- it is
        # simply never contacted: no folder listing, no unified
        # fetch, no Refresh All, no IDLE watcher, no search. That is
        # the whole difference between this and Remove Account, and
        # it is what lets an unreachable server be parked rather than
        # deleted. Every network path reads enabled_accounts() below;
        # self.accounts stays complete for anything that manages
        # accounts rather than uses them.
        self.enabled = bool(enabled)
        # Thunderbird's "Check for new messages automatically". Off
        # means nothing contacts this account on its own: no IDLE, no
        # timed or silent refresh, no folder counts, no prefetch, no
        # offline sync. Its offline copy and cache still open, and
        # anything the reader asks for (F5, opening a folder, send,
        # delete) still goes to the server. For an unreliable server
        # that should not be parked outright. On by default.
        self.auto_check = bool(auto_check)
        # How this account signs in: "password" (the stored password,
        # through --get-secret) or "microsoft" (Microsoft sign-in,
        # through --get-token; see ms_oauth). Anything else is a
        # password, so an account saved by an older build is unchanged.
        self.auth_method = "microsoft" if auth_method == "microsoft" else "password"
        # The primary address's OpenPGP sending settings (stage 3), only
        # what differs from OPENPGP_DEFAULTS; extra identities keep theirs
        # in their own entry.
        self.openpgp = _normalize_openpgp(openpgp)
        # Transient, never saved: outgoing passwords typed for extra
        # identities in the settings dialog ({identity_id: password}),
        # and the secret-store keys of identities that no longer have
        # their own outgoing server. AccountManager.update_account
        # stores or deletes them and empties both, the same way it
        # handles self.password above.
        self.identity_passwords = {}
        self.identity_secret_removals = []

    def identities(self):
        """
        All identities this account can send/reply as: the primary
        (display_name, identity_email) first, then each configured
        extra identity in order -- audit finding 49.
        """
        result = [(self.display_name, self.identity_email)]
        for extra in self.extra_identities:
            result.append((extra["display_name"], extra["email"]))
        return result

    def identity_settings(self, email):
        """The extra identity entry that sends as `email`, the live dict
        inside extra_identities, or None for the primary address or an
        address this account has no identity for. None means: use the
        account's own signature, folders and outgoing server."""
        target = (email or "").strip().lower()
        if not target or target == (self.identity_email or "").strip().lower():
            return None
        for extra in self.extra_identities:
            if extra["email"].lower() == target:
                return extra
        return None

    def openpgp_settings(self, email):
        """The OpenPGP sending settings for email, over OPENPGP_DEFAULTS:
        the extra identity's own, or the account's for its primary address."""
        extra = self.identity_settings(email)
        stored = extra.get("openpgp") if extra is not None else self.openpgp
        result = dict(OPENPGP_DEFAULTS)
        result.update(stored or {})
        return result

    def identity_secret_key(self, extra):
        """Where an identity's own outgoing password is stored."""
        return f"{self.account_id}.{extra['identity_id']}"

    def identity_section_name(self, extra):
        """The Himalaya section an identity with its own outgoing
        server sends through."""
        return f"acct_{self.account_id}_{extra['identity_id']}"

    def own_smtp_identities(self):
        """The extra identities that have their own outgoing server."""
        return [
            extra for extra in self.extra_identities
            if extra.get("own_smtp") and extra.get("identity_id")
        ]

    def toml_section_name(self):
        """
        Stable TOML table name Himalaya identifies this account by
        via -a on the command line. Based on account_id, not
        display_name, so renaming an account never breaks the link
        between ZBox's records and Himalaya's config.
        """
        return f"acct_{self.account_id}"

    def to_dict(self):
        data = {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "login_email": self.login_email,
            "identity_email": self.identity_email,
            # The password itself is not written to accounts.json;
            # see note in AccountManager.save_accounts.
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "imap_encryption": self.imap_encryption,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_encryption": self.smtp_encryption,
            "smtp_auth": self.smtp_auth,
            "signature": self.signature,
            "signature_html": self.signature_html,
            "filters": [rule.to_dict() for rule in self.filters],
            "junk_folder": self.junk_folder,
            "new_mail_sound_enabled": self.new_mail_sound_enabled,
            "new_mail_announce_enabled": self.new_mail_announce_enabled,
            "notifications_muted": self.notifications_muted,
            "extra_identities": list(self.extra_identities),
            "enabled": self.enabled,
            "auto_check": self.auto_check,
        }
        # Saved only for Microsoft sign-in, so a password account's
        # record and export stay exactly as they were, with no word
        # "password" anywhere in them.
        if self.auth_method == "microsoft":
            data["auth_method"] = "microsoft"
        # Only when set, so an account without OpenPGP settings is saved
        # exactly as before.
        if self.openpgp:
            data["openpgp"] = dict(self.openpgp)
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(
            account_id=data.get("account_id"),
            display_name=data.get("display_name", ""),
            login_email=data.get("login_email", ""),
            identity_email=data.get("identity_email", ""),
            imap_host=data.get("imap_host", ""),
            imap_port=data.get("imap_port", 993),
            imap_encryption=data.get("imap_encryption", "tls"),
            smtp_host=data.get("smtp_host", ""),
            smtp_port=data.get("smtp_port", 465),
            smtp_encryption=data.get("smtp_encryption", "tls"),
            smtp_auth=data.get("smtp_auth", "plain"),
            signature=data.get("signature", ""),
            signature_html=data.get("signature_html", ""),
            filters=[
                FilterRule.from_dict(entry)
                for entry in (data.get("filters") or [])
                if isinstance(entry, dict)
            ],
            junk_folder=data.get("junk_folder", ""),
            new_mail_sound_enabled=data.get("new_mail_sound_enabled", True),
            new_mail_announce_enabled=data.get("new_mail_announce_enabled", False),
            notifications_muted=data.get("notifications_muted", False),
            extra_identities=data.get("extra_identities"),
            enabled=data.get("enabled", True),
            auto_check=data.get("auto_check", True),
            auth_method=data.get("auth_method", "password"),
            openpgp=data.get("openpgp"),
        )


class AccountManager:
    def __init__(self, paths):
        self.paths = paths
        self.accounts = []
        # Set when accounts.json exists but could not be opened.
        # save_accounts refuses to write while it is set.
        self.load_error = None
        # Optional callable(message) the GUI sets so a refusal is
        # spoken and shown rather than only logged. Kept as a hook
        # instead of importing wx here: this module is reached by
        # get_secret.py, which runs with no GUI at all.
        self.notify_error = None
        self.load_accounts()

    # --- ZBox's own account list --------------------------------------

    def load_accounts(self):
        """
        Reads accounts.json into self.accounts.

        Two failure modes are handled separately, on purpose: the
        whole file being unreadable/corrupt, versus one single
        account entry inside it being malformed. Previously both
        collapsed into "the entire account list is empty, no log, no
        warning" -- which looks exactly like an account silently
        vanishing, with nothing in the debug log to explain why.

        A whole-file read failure gets one retry after a brief pause:
        Windows file locks from antivirus scanning, a sync-folder
        client (OneDrive/Dropbox), or ZBox itself mid-write are
        usually gone within a fraction of a second, and giving up
        immediately turned a passing lock into every account
        disappearing for the rest of that session.
        """
        self.accounts = []
        self.load_error = None
        if not os.path.isfile(self.paths.accounts_file):
            return

        data = None
        last_error = None
        for attempt in range(2):
            if attempt:
                time.sleep(0.3)
            try:
                data = data_crypto.read_json(
                    self.paths.config, self.paths.accounts_file,
                )
                break
            except data_crypto.EncryptedDataUnavailable as exc:
                # Locked on this machine, or damaged. Neither is a
                # passing file lock, so the retry has nothing to wait
                # for and would only delay saying so.
                last_error = exc
                break
            except (OSError, json.JSONDecodeError) as exc:
                last_error = exc

        if data is None:
            self.load_error = str(last_error) or "unreadable"
            logging.getLogger("zbox.account").error(
                "Could not read %s: %s. Starting this session with no "
                "accounts loaded; the file has not been modified and "
                "will not be written to until it can be read.",
                self.paths.accounts_file, last_error,
            )
            return

        accounts = []
        for item in data.get("accounts", []):
            try:
                accounts.append(Account.from_dict(item))
            except Exception as exc:
                # One malformed entry no longer takes every other
                # account down with it.
                logging.getLogger("zbox.account").error(
                    "Skipped one unreadable account entry in %s: %s",
                    self.paths.accounts_file, exc,
                )
        self.accounts = accounts
        self._ensure_encrypted()

    def _ensure_encrypted(self):
        """Converts a plaintext accounts.json to the encrypted form,
        once, on the first launch after this feature arrived.

        Runs from load_accounts rather than from main.py because the
        ordering matters and this is the point where it is already
        satisfied: AccountManager is built by the main frame, which
        exists only after the startup unlock has run, so the key is
        available by the time this is reached.

        A folder carried to a new computer has no key yet, and the
        conversion is skipped with a note rather than failing the
        launch. It happens on a later launch, after the master
        password has been entered once.
        """
        try:
            if data_crypto.encrypt_in_place(
                self.paths.config, self.paths.accounts_file,
            ):
                logging.getLogger("zbox.account").info(
                    "accounts.json is now encrypted at rest."
                )
        except data_crypto.EncryptedDataUnavailable as exc:
            logging.getLogger("zbox.account").warning(
                "accounts.json is still in plain text: %s", exc,
            )

    def save_accounts(self):
        # Passwords are never written here. They live only in
        # data/config/secrets.dat, encrypted via dpapi_secret_store, keyed
        # by account_id. Everything else in the record -- addresses,
        # server names, signatures, filters -- is personal data, and
        # is encrypted here with a key derived from the same keyset.
        if self.load_error:
            raise AccountsLocked(
                "The account list could not be opened this session (%s), "
                "so it will not be saved over. Restart ZBox, and enter "
                "your master password if you are asked for it."
                % self.load_error
            )
        data = {"accounts": [account.to_dict() for account in self.accounts]}
        data_crypto.write_json(
            self.paths.config, self.paths.accounts_file, data,
        )

    def save_changes(self, action="save this change"):
        """The returning form of save_accounts, for GUI callers.

        save_accounts raises, which is right for the three methods
        below: they are mid-way through a change and must not carry
        on. It is wrong for code that edits an Account in place and
        then asks for the list to be written, because the raise
        leaves a menu handler with an unhandled exception where every
        neighbouring path returns False and says why.

        Same refusal, same message, same False. Does not regenerate
        himalaya.toml -- callers changing server settings, addresses
        or passwords want update_account, which does.
        """
        if self.load_error:
            return self._report_locked(action)
        self.save_accounts()
        return True

    def _report_locked(self, action):
        """Refuses an account change and says why, returning False.

        Checked before anything is written rather than catching
        AccountsLocked afterwards, so a refused add does not leave a
        password sitting in the secret store for an account that was
        never saved.
        """
        message = (
            lang.t('errors', 'account_locked_action', default="ZBox cannot %s. The account list could not be opened this "
            "session (%s), and nothing has been changed. Close ZBox and "
            "start it again, and enter your master password if you are "
            "asked for it.") % (action, self.load_error)
        )
        logging.getLogger("zbox.account").error(message)
        if self.notify_error:
            self.notify_error(message)
        return False

    def enabled_accounts(self):
        """The accounts ZBox may contact.

        Every network path goes through this rather than
        self.accounts, so a disabled account costs nothing at all
        instead of timing out in the background -- which is the
        entire point of disabling one. Anything that manages accounts
        rather than uses them keeps reading self.accounts, so a
        disabled account is still listed, still editable and still
        removable.
        """
        return [account for account in self.accounts if account.enabled]

    def is_locked(self):
        """True when the account list could not be opened this session.

        Public because the GUI needs to ask before it opens a dialog,
        not only before it saves one. See refuse_if_locked.
        """
        return bool(self.load_error)

    def refuse_if_locked(self, action):
        """Entry-point guard: refuses and reports, returning True.

        The three methods below check load_error immediately before
        writing, which is the last line and stays. This is the first
        line, for menu handlers: New Account currently opens the whole
        wizard on a locked list and only refuses at add_account, so
        someone can type a display name, both addresses, four server
        fields and a password before being told nothing will be saved
        (open_issues V4).

        Returns True when the caller should stop. Same message and
        same logging as a refusal at the save, so the user does not
        get two different explanations for one condition.
        """
        if self.load_error:
            self._report_locked(action)
            return True
        return False

    def add_account(self, account):
        if self.load_error:
            return self._report_locked("add an account")
        if account.auth_method != "microsoft":
            # A Microsoft account has no password: its tokens were
            # stored by the sign-in itself, under the same account id.
            dpapi_secret_store.set_password(self.paths.config, account.account_id, account.password)
        account.password = ""  # never keep the raw secret in memory longer than needed
        self.accounts.append(account)
        self.save_accounts()
        self.write_himalaya_config()
        return True

    def remove_account(self, account_id):
        if self.load_error:
            return self._report_locked("remove an account")
        dpapi_secret_store.delete_password(self.paths.config, account_id)
        # Its Microsoft sign-in, if it had one, goes with it.
        ms_oauth.delete_record(self.paths.config, account_id)
        # Its identities' own outgoing passwords go with it.
        for account in self.accounts:
            if account.account_id == account_id:
                for extra in account.own_smtp_identities():
                    dpapi_secret_store.delete_password(
                        self.paths.config, account.identity_secret_key(extra),
                    )
        # Also clean up a pre-migration legacy entry, if any, so
        # removing an account doesn't leave its password sitting in
        # the OS credential store indefinitely.
        legacy_keyring_store.delete_password(account_id)
        self.accounts = [a for a in self.accounts if a.account_id != account_id]
        self.save_accounts()
        self.write_himalaya_config()
        return True

    def _store_identity_secrets(self, account):
        """Stores the outgoing passwords typed for this account's extra
        identities and deletes those of identities that no longer have
        their own server, then empties both transient lists. A blank
        password leaves the stored one as it is, like the account's."""
        for key in getattr(account, "identity_secret_removals", None) or []:
            dpapi_secret_store.delete_password(self.paths.config, key)
        account.identity_secret_removals = []
        passwords = getattr(account, "identity_passwords", None) or {}
        for extra in account.own_smtp_identities():
            password = passwords.get(extra["identity_id"])
            if password:
                dpapi_secret_store.set_password(
                    self.paths.config, account.identity_secret_key(extra), password,
                )
        account.identity_passwords = {}

    def stored_secret(self, key):
        """The stored secret under key, or None. Lets the identity
        dialog test an outgoing server whose password was left blank
        because one is already stored."""
        try:
            return dpapi_secret_store.get_password(self.paths.config, key)
        except Exception:  # noqa: BLE001 - unreadable means none here
            return None

    def update_account(self, account):
        if self.load_error:
            return self._report_locked("save account settings")
        if account.password and account.auth_method != "microsoft":
            # A new password was explicitly provided, replace the
            # stored secret. If blank, the previously stored
            # password for this account_id is left untouched.
            dpapi_secret_store.set_password(self.paths.config, account.account_id, account.password)
            account.password = ""
        account.password = ""
        if account.auth_method != "microsoft":
            # Signs in with a password: Microsoft tokens left from an
            # earlier Microsoft sign-in go.
            ms_oauth.delete_record(self.paths.config, account.account_id)
        self._store_identity_secrets(account)
        for index, existing in enumerate(self.accounts):
            if existing.account_id == account.account_id:
                self.accounts[index] = account
                break
        self.save_accounts()
        self.write_himalaya_config()
        return True

    def set_default_account(self, account_id):
        """
        Moves the account with this id to the front of self.accounts,
        which is the whole feature: the account tree lists accounts in
        self.accounts order (account_tree_panel.refresh_accounts), a
        brand-new compose window with no explicit from_identity
        preselects From choice index 0 (ComposePanel.__init__, built
        from all_identities(self.accounts)), and
        write_himalaya_config's own "default = true" line already
        follows self.accounts[0]. No new persisted field needed --
        reordering the one list already drives all three.

        Reply and Forward are untouched by this: both always resolve
        their own From via envelope_format._reply_from_identity against
        the message being replied to, never this ordering.

        Returns False (refused, or already the default -- nothing to
        do) without writing anything; True once moved and saved.
        """
        if self.load_error:
            return self._report_locked("set the default account")
        for index, account in enumerate(self.accounts):
            if account.account_id == account_id:
                if index == 0:
                    return False
                self.accounts.insert(0, self.accounts.pop(index))
                self.save_accounts()
                self.write_himalaya_config()
                return True
        return False

    def test_connection(self, account):
        """
        Audit finding 10: runs one real 'envelope list' against this
        account's own server settings and returns the envelopes
        (may be empty for an empty Inbox) on success, or raises
        himalaya_client.HimalayaError (or another exception from a
        bad host/timeout) on failure. Used by the account wizard's
        Test button so a wrong password or host is caught here
        instead of surfacing later as an opaque Himalaya error on
        the account's first real fetch.

        Fully isolated from the real account list: writes a
        throwaway one-account config to its own temp directory and
        points Himalaya at that instead of self.paths.
        himalaya_config_file, so a failed (or successful) test never
        touches write_himalaya_config's output or self.accounts, and
        works equally for a brand-new account that was never added.
        The password itself still only ever reaches Himalaya through
        password.command / --get-secret, same as every real account
        (see write_himalaya_config) -- it is stored under this
        account's id in the real encrypted secret store for the
        duration of the call and removed again in a finally block
        whether the test passes or fails.
        """
        if graph_client.uses_graph(account):
            # A personal Microsoft account works through Microsoft
            # Graph: the test lists one Inbox message there.
            return graph_client.test_connection(self.paths, account)
        section = account.toml_section_name()
        secret_cmd = _toml_string_array(self._get_secret_argv(account.account_id))
        imap_scheme = "imaps" if account.imap_encryption == "tls" else "imap"

        lines = [
            f"[accounts.{section}]",
            f"email = {_toml_string(account.identity_email)}",
            "default = true",
            f'imap.server = {_toml_string(f"{imap_scheme}://{account.imap_host}:{account.imap_port}")}',
        ]
        if account.imap_encryption == "start-tls":
            lines.append("imap.starttls = true")
        lines.extend(self._imap_auth_lines(account, secret_cmd))

        # Whatever (if anything) is already stored under this
        # account_id, so the finally block can put it back. The wizard
        # always tests a brand-new uuid, where this is None and the
        # cleanup is a plain delete -- but a test run for an account
        # that already exists would otherwise delete that account's
        # real password on the way out, whether the test passed or
        # failed, and it would only show up later as an authentication
        # failure with no obvious cause.
        try:
            previous_password = dpapi_secret_store.get_password(
                self.paths.config, account.account_id,
            )
        except Exception:  # noqa: BLE001 -- an unreadable store is the
            # store's own problem; treat it as "nothing to restore"
            # rather than blocking the test on it.
            previous_password = None

        # A Microsoft account signs in with the tokens its sign-in
        # already stored, so there is no password to put in and take
        # back out.
        microsoft = getattr(account, "auth_method", "password") == "microsoft"
        if not microsoft:
            dpapi_secret_store.set_password(self.paths.config, account.account_id, account.password)
        try:
            with tempfile.TemporaryDirectory(prefix="zbox_test_") as temp_dir:
                config_path = os.path.join(temp_dir, "himalaya.toml")
                with open(config_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines))
                test_paths = SimpleNamespace(
                    himalaya_binary=self.paths.himalaya_binary,
                    himalaya_config_file=config_path,
                    # The REAL config directory, not the temp one.
                    # Only the Himalaya TOML is throwaway; the
                    # password still lives in the real encrypted
                    # store (see the set_password call above), so
                    # anything asking this object where the secrets
                    # are has to be told the truth.
                    #
                    # Its absence was a live bug, caught in the log
                    # on 9 September: list_envelopes tried the pooled
                    # IMAP path first, that path reads paths.config
                    # to fetch the password, and the AttributeError
                    # ('types.SimpleNamespace' object has no
                    # attribute 'config') was retried once and only
                    # then fell back to the subprocess. Two wasted
                    # connection attempts in front of a Test button
                    # someone is waiting on.
                    config=self.paths.config,
                )
                return himalaya_client.list_envelopes(
                    test_paths, account, folder="INBOX", page_size=1,
                    backend="imap",
                    # Deliberately the subprocess. This test's whole
                    # purpose is to prove the config written above
                    # works end to end through himalaya.exe; the
                    # pooled path connects with imapclient directly
                    # and never reads the TOML, so a pass from it
                    # would not mean what the Test button says. It
                    # would also leave a pooled connection cached
                    # under the id of an account that may never be
                    # added.
                    allow_pooled=False,
                )
        finally:
            if microsoft:
                pass
            elif previous_password is None:
                dpapi_secret_store.delete_password(self.paths.config, account.account_id)
            else:
                dpapi_secret_store.set_password(
                    self.paths.config, account.account_id, previous_password,
                )

    # --- Himalaya's own config ------------------------------------------

    def _get_token_argv(self, account_id):
        """The command Himalaya runs to fetch a Microsoft account's
        access token. Same frozen and from-source shapes as
        _get_secret_argv below."""
        if getattr(sys, "frozen", False):
            return [sys.executable, "--get-token", account_id]
        return [sys.executable, self.paths.main_script, "--get-token", account_id]

    def _imap_auth_lines(self, account, secret_cmd):
        """The incoming sign-in lines of one Himalaya section: XOAUTH2
        with a token command for a Microsoft account, PLAIN with the
        password command otherwise."""
        if getattr(account, "auth_method", "password") == "microsoft":
            token_cmd = _toml_string_array(self._get_token_argv(account.account_id))
            return [
                f"imap.sasl.xoauth2.username = {_toml_string(account.login_email)}",
                f"imap.sasl.xoauth2.token.command = {token_cmd}",
            ]
        return [
            f"imap.sasl.plain.username = {_toml_string(account.login_email)}",
            f"imap.sasl.plain.password.command = {secret_cmd}",
        ]

    def _smtp_auth_lines(self, account, secret_cmd):
        """The outgoing sign-in lines of an account's own section.
        The same Microsoft token serves IMAP and SMTP."""
        if getattr(account, "auth_method", "password") == "microsoft":
            token_cmd = _toml_string_array(self._get_token_argv(account.account_id))
            return [
                f"smtp.sasl.xoauth2.username = {_toml_string(account.login_email)}",
                f"smtp.sasl.xoauth2.token.command = {token_cmd}",
            ]
        smtp_mech = "login" if getattr(account, "smtp_auth", "plain") == "login" else "plain"
        return [
            f"smtp.sasl.{smtp_mech}.username = {_toml_string(account.login_email)}",
            f"smtp.sasl.{smtp_mech}.password.command = {secret_cmd}",
        ]

    def _get_secret_argv(self, account_id):
        """The command Himalaya runs to fetch one account's password.

        Frozen and from-source need different shapes, and getting
        this wrong is invisible from source. Under PyInstaller
        sys.executable is ZBox.exe, so the old
        [sys.executable, app\\get_secret.py, id] became
        "ZBox.exe app\\get_secret.py id" -- a second copy of the GUI,
        no password, and every account failing to authenticate in the
        built app only.

        Frozen, the executable handles the flag itself. From source,
        the interpreter needs main.py named explicitly first.
        """
        if getattr(sys, "frozen", False):
            return [sys.executable, "--get-secret", account_id]
        return [sys.executable, self.paths.main_script, "--get-secret", account_id]

    def _graph_section_tail(self, account):
        """The rest of a Graph account's sections (write_himalaya_config):
        its offline maildir, then a section per identity with its own
        outgoing server, SMTP only."""
        lines = []
        maildir_root = os.path.join(self.paths.cache_dir(account.account_id), "offline_mail")
        lines.append(f"maildir.root = {_toml_string(maildir_root)}")
        lines.append("")
        for extra in account.own_smtp_identities():
            identity_cmd = _toml_string_array(
                self._get_secret_argv(account.identity_secret_key(extra))
            )
            identity_scheme = "smtps" if extra["smtp_encryption"] == "tls" else "smtp"
            identity_mech = "login" if extra.get("smtp_auth") == "login" else "plain"
            lines.append(f"[accounts.{account.identity_section_name(extra)}]")
            lines.append(f'email = {_toml_string(extra["email"])}')
            lines.append(
                f'display-name = {_toml_string(extra["display_name"] or account.display_name)}'
            )
            lines.append("default = false")
            lines.append("message.send.save-copy = false")
            lines.append("")
            identity_url = "%s://%s:%s" % (
                identity_scheme, extra["smtp_host"], extra["smtp_port"],
            )
            lines.append(f"smtp.server = {_toml_string(identity_url)}")
            if extra["smtp_encryption"] == "start-tls":
                lines.append("smtp.starttls = true")
            lines.append(
                f'smtp.sasl.{identity_mech}.username = {_toml_string(extra["smtp_login"])}'
            )
            lines.append(f"smtp.sasl.{identity_mech}.password.command = {identity_cmd}")
            lines.append("")
        return lines

    def write_himalaya_config(self):
        """
        Regenerates Himalaya's TOML config from the current account
        list, using the schema Himalaya 2.1.0 actually reads:
        imap.server / smtp.server as scheme://host:port URLs, plus
        imap.sasl.plain.* / smtp.sasl.plain.* for credentials. This
        replaced the older backend.type/auth.cmd style config in a
        recent Himalaya release.

        login_email authenticates; identity_email fills the From
        header, so the two can differ per account. The password
        itself is never written here: password.command invokes ZBox
        with --get-secret, which reads the real secret from the
        encrypted store at connection time (see _get_secret_argv).
        """
        lines = []
        for account in self.accounts:
            # The special folders learned from the server on an earlier run,
            # so the aliases below use them from launch on.
            provider_presets.load_learned_folders(self.paths, account.account_id)
            section = account.toml_section_name()
            secret_cmd = _toml_string_array(
                self._get_secret_argv(account.account_id)
            )

            imap_scheme = "imaps" if account.imap_encryption == "tls" else "imap"
            smtp_scheme = "smtps" if account.smtp_encryption == "tls" else "smtp"

            lines.append(f"[accounts.{section}]")
            lines.append(f'email = {_toml_string(account.identity_email)}')
            lines.append(f'display-name = {_toml_string(account.display_name)}')
            lines.append(f'default = {"true" if account is self.accounts[0] else "false"}')
            # Explicit folder aliases, matching the folders ZBox shows
            # per account (Inbox/Sent/Drafts/Trash/Junk/Archive), so
            # Himalaya has an unambiguous mapping instead of guessing
            # which real folder counts as "sent" when saving a copy
            # of an outgoing message. Resolved per-provider so Gmail
            # gets its real "[Gmail]/..." names (and "[Gmail]/All
            # Mail" for archive) instead of names that don't exist on
            # its server; every other provider keeps today's plain
            # names since they have no override in provider_presets.
            sent_folder = provider_presets.himalaya_folder_name(account.imap_host, "Sent", account.account_id)
            drafts_folder = provider_presets.himalaya_folder_name(account.imap_host, "Drafts", account.account_id)
            trash_folder = provider_presets.himalaya_folder_name(account.imap_host, "Trash", account.account_id)
            junk_folder = provider_presets.himalaya_folder_name(account.imap_host, "Junk", account.account_id)
            archive_folder = provider_presets.himalaya_folder_name(account.imap_host, "Archive", account.account_id)
            lines.append('folder.aliases.inbox = "INBOX"')
            lines.append(f'folder.aliases.sent = {_toml_string(sent_folder)}')
            lines.append(f'folder.aliases.drafts = {_toml_string(drafts_folder)}')
            lines.append(f'folder.aliases.trash = {_toml_string(trash_folder)}')
            lines.append(f'folder.aliases.junk = {_toml_string(junk_folder)}')
            lines.append(f'folder.aliases.archive = {_toml_string(archive_folder)}')
            # Himalaya 3.x renamed the alias keys to mailbox.alias.*
            # (singular) and its 'message delete' command refuses to
            # run without mailbox.alias.trash. Emit the new key next
            # to the legacy folder.aliases.* so both 2.x and 3.x
            # builds can resolve the trash mailbox.
            lines.append(f'mailbox.alias.trash = {_toml_string(trash_folder)}')
            # Explicit rather than relying on Himalaya's own default,
            # so a sent message reliably gets a copy saved to Sent.
            lines.append("message.send.save-copy = true")
            lines.append("")
            if graph_client.uses_graph(account):
                # A personal Microsoft account works through Microsoft
                # Graph: no IMAP and no SMTP here, only its offline copy
                # and any identity with its own outgoing server.
                lines.extend(self._graph_section_tail(account))
                continue
            lines.append(
                f'imap.server = {_toml_string(f"{imap_scheme}://{account.imap_host}:{account.imap_port}")}'
            )
            if account.imap_encryption == "start-tls":
                lines.append("imap.starttls = true")
            lines.extend(self._imap_auth_lines(account, secret_cmd))
            lines.append("")
            lines.append(
                f'smtp.server = {_toml_string(f"{smtp_scheme}://{account.smtp_host}:{account.smtp_port}")}'
            )
            if account.smtp_encryption == "start-tls":
                lines.append("smtp.starttls = true")
            lines.extend(self._smtp_auth_lines(account, secret_cmd))
            lines.append("")
            # Local offline copy. Presence of this key doesn't change
            # anything about the live IMAP path: himalaya_client.py
            # forces --backend imap on every online call precisely so
            # adding this second backend to the account can't shift
            # what Himalaya's own 'auto' backend pick would have
            # chosen. Nothing reads from here unless a caller asks
            # for backend="maildir" explicitly.
            maildir_root = os.path.join(self.paths.cache_dir(account.account_id), "offline_mail")
            lines.append(f"maildir.root = {_toml_string(maildir_root)}")
            lines.append("")

            # One more section per extra identity with its own outgoing
            # server. Only Send uses it (compose_panel passes its name
            # to send_message_raw). It repeats the account's incoming
            # server so it is a complete account like every other
            # section. Himalaya's own Sent copy is off here: ZBox's
            # ensure_sent_copy saves it, to the identity's own Sent
            # folder when it has one.
            for extra in account.own_smtp_identities():
                identity_cmd = _toml_string_array(
                    self._get_secret_argv(account.identity_secret_key(extra))
                )
                identity_scheme = "smtps" if extra["smtp_encryption"] == "tls" else "smtp"
                identity_mech = "login" if extra.get("smtp_auth") == "login" else "plain"
                lines.append(f"[accounts.{account.identity_section_name(extra)}]")
                lines.append(f'email = {_toml_string(extra["email"])}')
                lines.append(
                    f'display-name = {_toml_string(extra["display_name"] or account.display_name)}'
                )
                lines.append("default = false")
                lines.append("message.send.save-copy = false")
                lines.append("")
                lines.append(
                    f'imap.server = {_toml_string(f"{imap_scheme}://{account.imap_host}:{account.imap_port}")}'
                )
                if account.imap_encryption == "start-tls":
                    lines.append("imap.starttls = true")
                lines.extend(self._imap_auth_lines(account, secret_cmd))
                lines.append("")
                identity_url = "%s://%s:%s" % (
                    identity_scheme, extra["smtp_host"], extra["smtp_port"],
                )
                lines.append(f"smtp.server = {_toml_string(identity_url)}")
                if extra["smtp_encryption"] == "start-tls":
                    lines.append("smtp.starttls = true")
                lines.append(
                    f'smtp.sasl.{identity_mech}.username = {_toml_string(extra["smtp_login"])}'
                )
                lines.append(f"smtp.sasl.{identity_mech}.password.command = {identity_cmd}")
                lines.append("")

        with open(self.paths.himalaya_config_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))


def _toml_escape(value):
    """Escapes backslashes and quotes for a TOML basic string. Needed
    for every Windows path (backslashes) written into the config."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_string(value):
    return f'"{_toml_escape(value)}"'


def _toml_string_array(items):
    return "[" + ", ".join(_toml_string(item) for item in items) + "]"
