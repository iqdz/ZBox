"""
Bundled server presets for well-known email providers, keyed by
email domain. The account wizard matches the domain typed by the
person and pre-fills these; every field stays editable afterward,
and anything not matched falls back to a blank custom entry.

encryption is one of: "tls" (implicit SSL/TLS), "start-tls", "none".
"""

# A preset with "unsupported": True is one ZBox cannot actually make
# work (item 32 of the 2026-09-03 audit): the wizard still applies its
# server settings for editing, but AccountWizard requires the person to
# explicitly acknowledge the note before Finish, rather than silently
# handing them a setup that cannot sign in.
PROVIDER_PRESETS = {
    "gmail.com": {
        "display_name": "Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": "Google requires an app password or OAuth2, not your normal password.",
    },
    "googlemail.com": {
        "display_name": "Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": "Google requires an app password or OAuth2, not your normal password.",
    },
    "outlook.com": {
        "display_name": "Outlook.com",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "unsupported": True,
        "note": (
            "Microsoft has withdrawn password sign-in for Outlook.com/"
            "Office 365 accounts -- only OAuth2 works, and ZBox does not "
            "support OAuth2 yet. This account will not be able to sign in."
        ),
    },
    "hotmail.com": {
        "display_name": "Outlook.com",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "unsupported": True,
        "note": (
            "Microsoft has withdrawn password sign-in for Outlook.com/"
            "Office 365 accounts -- only OAuth2 works, and ZBox does not "
            "support OAuth2 yet. This account will not be able to sign in."
        ),
    },
    "live.com": {
        "display_name": "Outlook.com",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "unsupported": True,
        "note": (
            "Microsoft has withdrawn password sign-in for Outlook.com/"
            "Office 365 accounts -- only OAuth2 works, and ZBox does not "
            "support OAuth2 yet. This account will not be able to sign in."
        ),
    },
    "yahoo.com": {
        "display_name": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": "Yahoo requires an app password.",
    },
    "icloud.com": {
        "display_name": "iCloud Mail",
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "note": "Apple requires an app-specific password.",
    },
    "me.com": {
        "display_name": "iCloud Mail",
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "note": "Apple requires an app-specific password.",
    },
    "disroot.org": {
        "display_name": "Disroot",
        "imap_host": "disroot.org",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "disroot.org",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "note": None,
    },
    "fastmail.com": {
        "display_name": "Fastmail",
        "imap_host": "imap.fastmail.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.fastmail.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": "Fastmail requires an app password.",
    },
    "zoho.com": {
        "display_name": "Zoho Mail",
        "imap_host": "imap.zoho.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.zoho.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": None,
    },
    "gmx.com": {
        "display_name": "GMX",
        "imap_host": "imap.gmx.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.gmx.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": None,
    },
    "gmx.net": {
        "display_name": "GMX",
        "imap_host": "imap.gmx.net",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.gmx.net",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": None,
    },
    "protonmail.com": {
        "display_name": "ProtonMail",
        "imap_host": "127.0.0.1",
        "imap_port": 1143,
        "imap_encryption": "start-tls",
        "smtp_host": "127.0.0.1",
        "smtp_port": 1025,
        "smtp_encryption": "start-tls",
        "note": "ProtonMail requires the ProtonMail Bridge app running locally.",
    },
    "proton.me": {
        "display_name": "ProtonMail",
        "imap_host": "127.0.0.1",
        "imap_port": 1143,
        "imap_encryption": "start-tls",
        "smtp_host": "127.0.0.1",
        "smtp_port": 1025,
        "smtp_encryption": "start-tls",
        "note": "ProtonMail requires the ProtonMail Bridge app running locally.",
    },
}


def preset_is_unsupported(preset):
    """
    True when a preset's server settings cannot actually sign in
    (item 32 of the 2026-09-03 audit: Microsoft has withdrawn
    password sign-in for outlook.com/hotmail.com/live.com and ZBox
    has no OAuth2). Pure and wx-free so it stays unit-testable
    without a real ServerPage.
    """
    return bool(preset and preset.get("unsupported"))


def preset_for_domain(email_address):
    """
    Returns the preset dict for the domain in email_address, or None
    if the domain isn't in the bundled list.
    """
    if "@" not in email_address:
        return None
    domain = email_address.rsplit("@", 1)[1].strip().lower()
    return PROVIDER_PRESETS.get(domain)


# Real IMAP folder names for ZBox's generic display folders, keyed by
# imap_host, for providers whose server-side names don't match those
# generic labels. Gmail is the main case: it exposes special-use
# folders under a "[Gmail]/" prefix, and its real archive concept is
# "All Mail" (a message loses the Inbox label but keeps living there)
# rather than a folder literally named "Archive". This mirrors what
# Thunderbird does -- it relabels "[Gmail]/All Mail" as "Archives" in
# its folder pane instead of showing Gmail's own folder name.
#
# "Inbox" is deliberately never remapped here (and never looked up
# via this table) -- INBOX is a real, universal IMAP mailbox name, and
# only messages actually inside it should ever appear as ZBox Inbox
# content, for every provider including Gmail.
SPECIAL_FOLDER_NAMES = {
    "imap.gmail.com": {
        "Sent": "[Gmail]/Sent Mail",
        "Drafts": "[Gmail]/Drafts",
        "Trash": "[Gmail]/Trash",
        "Junk": "[Gmail]/Spam",
        "Archive": "[Gmail]/All Mail",
    },
}


def himalaya_folder_name(imap_host, display_name):
    """
    Resolves one of ZBox's generic display folder names ("Sent",
    "Archive", etc.) to the real IMAP folder name Himalaya should use
    for the given account's server, via SPECIAL_FOLDER_NAMES. Falls
    back to the display name unchanged for "Inbox" and for any
    provider without an override, which preserves today's behavior
    for every account that isn't Gmail.
    """
    if display_name == "Inbox":
        return "INBOX"
    overrides = SPECIAL_FOLDER_NAMES.get((imap_host or "").strip().lower(), {})
    return overrides.get(display_name, display_name)
