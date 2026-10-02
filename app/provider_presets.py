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
        "oauth": "microsoft",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "note": (
            "Outlook.com, Hotmail, Live and MSN accounts sign in with a "
            "Microsoft account. Choose Microsoft account as the sign-in "
            "method on the Login page."
        ),
    },
    "hotmail.com": {
        "display_name": "Outlook.com",
        "oauth": "microsoft",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "note": (
            "Outlook.com, Hotmail, Live and MSN accounts sign in with a "
            "Microsoft account. Choose Microsoft account as the sign-in "
            "method on the Login page."
        ),
    },
    "live.com": {
        "display_name": "Outlook.com",
        "oauth": "microsoft",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_encryption": "start-tls",
        "note": (
            "Outlook.com, Hotmail, Live and MSN accounts sign in with a "
            "Microsoft account. Choose Microsoft account as the sign-in "
            "method on the Login page."
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
        "smtp_host": "mail.gmx.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
        "note": None,
    },
    "gmx.net": {
        "display_name": "GMX",
        "imap_host": "imap.gmx.net",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "mail.gmx.net",
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


def _preset(display_name, imap_host, smtp_host, smtp_port=465,
            smtp_encryption="tls", imap_port=993, imap_encryption="tls",
            note=None):
    return {
        "display_name": display_name,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "imap_encryption": imap_encryption,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_encryption": smtp_encryption,
        "note": note,
    }


_LOCALPART_NOTE = (
    "%s signs in with the part of your address before the @. "
    "Enter only that part as the login email."
)

# European providers, from Thunderbird's ISPDB
# (autoconfig.thunderbird.net/v1.1/<domain>), read on 2026-09-27.
# Outgoing mail uses implicit SSL/TLS on 465 wherever the provider
# offers it. tiscali.it is left out: it only accepts encrypted
# password authentication, which ZBox does not send.
PROVIDER_PRESETS.update({
    "kpnmail.nl": _preset("KPN Mail", "imap.kpnmail.nl", "smtp.kpnmail.nl"),
    "ziggo.nl": _preset("Ziggo Mail", "imap.ziggo.nl", "smtp.ziggo.nl",
                        smtp_port=587, smtp_encryption="start-tls"),
    "web.de": _preset(
        "WEB.DE", "imap.web.de", "smtp.web.de",
        note=(_LOCALPART_NOTE % "WEB.DE") + " POP3/IMAP access must be "
        "turned on in the WEB.DE web mail settings first.",
    ),
    "t-online.de": _preset(
        "Telekom Mail", "secureimap.t-online.de", "securesmtp.t-online.de",
        note="Telekom needs a separate email password, set in the "
        "Telekom Mail web settings, not your Telekom login password.",
    ),
    "posteo.de": _preset("Posteo", "posteo.de", "posteo.de"),
    "orange.fr": _preset("Mail Orange", "imap.orange.fr", "smtp.orange.fr"),
    "free.fr": _preset("Free", "imap.free.fr", "smtp.free.fr",
                       note=_LOCALPART_NOTE % "Free"),
    "sfr.fr": _preset("SFR Mail", "imap.sfr.fr", "smtp.sfr.fr"),
    "laposte.net": _preset("La Poste", "imap.laposte.net", "smtp.laposte.net",
                           note=_LOCALPART_NOTE % "La Poste"),
    "libero.it": _preset("Libero Mail", "imapmail.libero.it", "smtp.libero.it"),
    "virgilio.it": _preset("Virgilio Mail", "in.virgilio.it", "out.virgilio.it"),
    "alice.it": _preset("Alice Mail", "in.alice.it", "out.alice.it",
                        smtp_port=587, smtp_encryption="start-tls",
                        imap_port=143, imap_encryption="start-tls"),
    "email.it": _preset("Email.it", "in.email.it", "out.email.it"),
    "seznam.cz": _preset("Seznam", "imap.seznam.cz", "smtp.seznam.cz"),
    "wp.pl": _preset("Poczta WP", "imap.wp.pl", "smtp.wp.pl",
                     note=_LOCALPART_NOTE % "WP"),
    "o2.pl": _preset("Poczta o2", "poczta.o2.pl", "poczta.o2.pl",
                     note=_LOCALPART_NOTE % "o2"),
    "onet.pl": _preset("Poczta Onet", "imap.poczta.onet.pl", "smtp.poczta.onet.pl"),
    "yandex.ru": _preset("Yandex Mail", "imap.yandex.com", "smtp.yandex.com",
                         note="Yandex requires an app password."),
    "mail.ru": _preset("Mail.ru", "imap.mail.ru", "smtp.mail.ru",
                       note="Mail.ru requires a password for external apps, "
                       "created in its security settings."),
    "btinternet.com": _preset("BT Mail", "mail.btinternet.com", "mail.btinternet.com"),
    "virginmedia.com": _preset("Virgin Media Mail", "imap.virginmedia.com",
                               "smtp.virginmedia.com"),
    "sky.com": _preset("Sky Mail", "imap.tools.sky.com", "smtp.tools.sky.com"),
    "bluewin.ch": _preset("Bluewin", "imaps.bluewin.ch", "smtpauths.bluewin.ch"),
    "proximus.be": _preset("Proximus", "imap.proximus.be", "relay.proximus.be",
                           smtp_port=587, smtp_encryption="start-tls"),
})


def _unsupported(preset, note):
    return dict(preset, unsupported=True, note=note)


_APP_PASSWORD_NOTE = "%s mail now runs on %s's servers and needs an app password, not your normal password."

# North American providers, from Thunderbird's ISPDB
# (autoconfig.thunderbird.net/v1.1/<domain>) where it has them, read
# on 2026-09-28, and otherwise from each provider's own help pages.
# Cox, Frontier, Rogers, Verizon and Telus mail now run on Yahoo,
# AOL or Google servers. Charter's 587 with implicit SSL is what
# ISPDB lists. Videotron, SaskTel, Eastlink and Telmex are left out:
# no reliable current settings were found for them.
PROVIDER_PRESETS.update({
    "charter.net": _preset("Spectrum (Charter)", "mobile.charter.net", "mobile.charter.net",
                           smtp_port=587, smtp_encryption="tls"),
    "twc.com": _preset("Spectrum (Time Warner)", "mail.twc.com", "mail.twc.com",
                       smtp_port=587, smtp_encryption="start-tls"),
    "brighthouse.com": _preset("Spectrum (Bright House)", "mail.brighthouse.com",
                               "mail.brighthouse.com", smtp_port=587,
                               smtp_encryption="start-tls"),
    "comcast.net": _preset("Xfinity (Comcast)", "imap.comcast.net", "smtp.comcast.net"),
    "att.net": _preset(
        "AT&T Mail", "imap.mail.att.net", "outbound.att.net",
        note="AT&T needs a secure mail key, created in your AT&T profile, "
        "instead of your normal password.",
    ),
    "aol.com": _preset("AOL Mail", "imap.aol.com", "smtp.aol.com",
                       note="AOL needs an app password, not your normal password."),
    "verizon.net": _preset("Verizon (AOL Mail)", "imap.aol.com", "smtp.aol.com",
                           note=_APP_PASSWORD_NOTE % ("Verizon", "AOL")),
    "cox.net": _preset("Cox (Yahoo Mail)", "imap.mail.yahoo.com", "smtp.mail.yahoo.com",
                       note=_APP_PASSWORD_NOTE % ("Cox", "Yahoo")),
    "frontier.com": _preset("Frontier (Yahoo Mail)", "imap.mail.yahoo.com",
                            "smtp.mail.yahoo.com",
                            note=_APP_PASSWORD_NOTE % ("Frontier", "Yahoo")),
    "optimum.net": _preset("Optimum", "mail.optimum.net", "mail.optimum.net",
                           note="Optimum signs in with your Optimum ID, the part of "
                           "your address before the @. Enter only that part as the "
                           "login email."),
    "centurylink.net": _preset("CenturyLink", "mail.centurylink.net",
                               "smtp.centurylink.net", smtp_port=587,
                               smtp_encryption="start-tls"),
    "q.com": _preset("Q.com", "mail.q.com", "smtp.q.com",
                     smtp_port=587, smtp_encryption="start-tls"),
    "centurytel.net": _unsupported(
        _preset("CenturyTel", "pop.centurytel.net", "smtpauth.centurytel.net",
                imap_port=995, smtp_port=587, smtp_encryption="start-tls"),
        "CenturyTel addresses only offer POP, which downloads mail to a single "
        "device. ZBox works with IMAP, which keeps your mail on the server, so "
        "this account will not work in ZBox. You can ask CenturyLink whether your "
        "address can use their IMAP mail instead.",
    ),
    "earthlink.net": _preset("EarthLink", "imap.earthlink.net", "smtpauth.earthlink.net",
                             smtp_port=587, smtp_encryption="start-tls"),
    "windstream.net": _preset("Windstream", "imap.windstream.net", "smtp.windstream.net"),
    "mediacombb.net": _preset("Mediacom", "mail.mediacombb.net", "smtp.mediacombb.net",
                              smtp_port=587, smtp_encryption="start-tls"),
    "juno.com": _unsupported(
        _preset("Juno", "imap.juno.com", "smtp.juno.com", imap_port=143,
                imap_encryption="none"),
        "Free Juno and NetZero accounts only offer POP, which ZBox does not use. "
        "Paid accounts offer IMAP, but only without encryption, which would send "
        "your password over the internet in plain text. ZBox does not set up an "
        "account that way, so this account will not work in ZBox.",
    ),
    "rogers.com": _preset("Rogers (Yahoo Mail)", "imap.mail.yahoo.com",
                          "smtp.mail.yahoo.com",
                          note=_APP_PASSWORD_NOTE % ("Rogers", "Yahoo")),
    "bell.net": _preset("Bell", "imap.bell.net", "smtphm.sympatico.ca",
                        smtp_port=587, smtp_encryption="start-tls"),
    "shaw.ca": _preset("Shaw", "imap.shaw.ca", "mail.shaw.ca",
                       smtp_port=587, smtp_encryption="start-tls",
                       note="Shaw signs in with the part of your address before "
                       "@shaw.ca. Enter only that part as the login email. Shaw "
                       "Webmail must also be set to Classic mode, on a computer."),
    "telus.net": _preset("Telus (Google)", "imap.gmail.com", "smtp.gmail.com",
                         note=_APP_PASSWORD_NOTE % ("Telus", "Google")),
    "cogeco.ca": _preset("Cogeco", "imap.cogeco.ca", "smtp.cogeco.ca",
                         smtp_port=587, smtp_encryption="start-tls"),
    "cgocable.ca": _preset("Cogeco (Quebec)", "imap.cgocable.ca", "smtp.cgocable.ca",
                           smtp_port=587, smtp_encryption="start-tls"),
})

# Road Runner's regional addresses that Spectrum serves from Bright
# House's servers. Every other address ending in .rr.com goes to
# Time Warner's (see preset_for_domain).
_BRIGHTHOUSE_RR = (
    "bak.rr.com", "bham.rr.com", "cfl.rr.com", "emore.rr.com", "eufala.rr.com",
    "indy.rr.com", "mi.rr.com", "panhandle.rr.com", "tampabay.rr.com",
)


def preset_is_unsupported(preset):
    """
    True when a preset's server settings cannot actually sign in, for
    a provider ZBox cannot work with, such as one that offers only POP
    or no encryption. Pure and wx-free so it stays unit-testable
    without a real ServerPage.
    """
    return bool(preset and preset.get("unsupported"))


def preset_oauth(preset):
    """The sign-in service a preset's accounts use instead of a
    password: "microsoft", or "" for a password. Pure and wx-free."""
    return str((preset or {}).get("oauth") or "")


# Server settings for every account that uses Microsoft sign-in:
# Outlook.com, Hotmail, Live and MSN addresses, and Microsoft 365 work
# or school accounts on their own domains. One token serves both.
MICROSOFT_SERVERS = {
    "imap_host": "outlook.office365.com",
    "imap_port": 993,
    "imap_encryption": "tls",
    "smtp_host": "smtp.office365.com",
    "smtp_port": 587,
    "smtp_encryption": "start-tls",
}


# Other domains served by the same servers as a preset domain, from
# Thunderbird's own ISPDB entry for yahoo.com
# (autoconfig.thunderbird.net/v1.1/yahoo.com). cox.net is in that
# entry too but is left out: it is not a Yahoo address to the person
# typing it. yahoo.co.jp is a separate service and not listed there.
DOMAIN_ALIASES = {
    domain: "yahoo.com"
    for domain in (
        "yahoo.ca", "yahoo.de", "yahoo.it", "yahoo.fr", "yahoo.es",
        "yahoo.se", "yahoo.co.in", "yahoo.co.uk", "yahoo.co.nz",
        "yahoo.com.au", "yahoo.com.ar", "yahoo.com.br", "yahoo.com.mx",
        "ymail.com", "myyahoo.com", "rocketmail.com",
    )
}

# Other domains served by the European presets above, from the same
# ISPDB entries. home.nl is Ziggo's, not KPN's.
_EUROPE_ALIASES = {
    "kpnmail.nl": (
        "kpnplanet.nl", "planet.nl", "wxs.nl", "hetnet.nl", "freeler.nl",
        "snelnet.net", "on.nl", "onsbrabantnet.nl", "onsmail.nl", "onsnet.nu",
        "onsneteindhoven.nl", "onsnetnuenen.nl", "xs4all.nl", "telfort.nl",
        "tip.nl", "tiscali.nl", "tiscalimail.nl",
    ),
    "ziggo.nl": (
        "hahah.nl", "ziggomail.com", "casema.nl", "zinders.nl", "zeggis.nl",
        "zeggis.com", "razcall.nl", "razcall.com", "upcmail.nl", "chello.nl",
        "multiweb.nl", "home.nl", "quicknet.nl",
    ),
    "gmx.net": (
        "gmx.de", "gmx.at", "gmx.ch", "gmx.eu", "gmx.biz", "gmx.org", "gmx.info",
    ),
    "gmx.com": (
        "gmx.us", "gmx.co.uk", "gmx.es", "gmx.fr", "gmx.ca", "gmx.ie", "gmx.pt",
        "gmx.se", "gmx.it", "gmx.li", "gmx.com.tr", "gmx.ru", "gmx.tm",
    ),
    "t-online.de": ("magenta.de",),
    "posteo.de": tuple("posteo." + tld for tld in (
        "at", "be", "ca", "ch", "cl", "co", "co.uk", "com", "com.br", "cr",
        "cz", "dk", "ee", "es", "eu", "fi", "gl", "gr", "hn", "hr", "hu",
        "ie", "in", "is", "it", "jp", "la", "li", "lt", "lu", "me", "mx",
        "my", "net", "nl", "no", "nz", "org", "pe", "pl", "pm", "pt", "ro",
        "se", "sg", "si", "tn", "uk", "us",
    )),
    "orange.fr": ("wanadoo.fr",),
    "sfr.fr": ("neuf.fr", "club-internet.fr"),
    "libero.it": ("iol.it", "blu.it", "inwind.it", "giallo.it"),
    "seznam.cz": ("email.cz", "post.cz", "spoluzaci.cz"),
    "o2.pl": ("go2.pl", "tlen.pl", "prokonto.pl"),
    "onet.pl": (
        "onet.eu", "op.pl", "vp.pl", "autograf.pl", "buziaczek.pl", "amorki.pl",
        "republika.pl", "adres.pl", "cyberia.pl", "onet.com.pl", "opoczta.pl",
        "pseudonim.pl", "spoko.pl",
    ),
    "yandex.ru": (
        "yandex.com", "yandex.net", "yandex.by", "yandex.kz", "yandex.ua",
        "ya.ru", "narod.ru",
    ),
    "mail.ru": ("inbox.ru", "list.ru", "bk.ru"),
    "btinternet.com": ("btopenworld.com", "talk21.com"),
    "bluewin.ch": ("bluemail.ch",),
    "proximus.be": ("skynet.be", "belgacom.net"),
}
for _target, _domains in _EUROPE_ALIASES.items():
    for _domain in _domains:
        DOMAIN_ALIASES[_domain] = _target

# Other domains served by the North American presets above.
_NORTH_AMERICA_ALIASES = {
    "charter.net": ("spectrum.net",),
    "twc.com": ("rr.com",),
    "brighthouse.com": _BRIGHTHOUSE_RR,
    "att.net": (
        "sbcglobal.net", "bellsouth.net", "pacbell.net", "swbell.net",
        "ameritech.net", "flash.net", "prodigy.net", "snet.net", "nvbell.net",
        "wans.net",
    ),
    "aol.com": ("aim.com",),
    "frontier.com": ("frontiernet.net",),
    "optimum.net": ("optonline.net",),
    "centurylink.net": ("embarqmail.com",),
    "earthlink.net": ("mindspring.com",),
    "mediacombb.net": ("mchsi.com",),
    "juno.com": ("netzero.net",),
    "bell.net": ("sympatico.ca",),
    "outlook.com": ("msn.com",),
    "icloud.com": ("mac.com",),
}
for _target, _domains in _NORTH_AMERICA_ALIASES.items():
    for _domain in _domains:
        DOMAIN_ALIASES[_domain] = _target


def preset_for_domain(email_address):
    """
    Returns the preset dict for the domain in email_address, or None
    if the domain isn't in the bundled list. A domain in
    DOMAIN_ALIASES gets the preset of the domain it is served by.
    """
    if "@" not in email_address:
        return None
    domain = email_address.rsplit("@", 1)[1].strip().lower()
    preset = PROVIDER_PRESETS.get(domain)
    if preset is None and domain in DOMAIN_ALIASES:
        preset = PROVIDER_PRESETS.get(DOMAIN_ALIASES[domain])
    if preset is None and domain.endswith(".rr.com"):
        # Road Runner has dozens of regional forms (nc.rr.com,
        # austin.rr.com, ...); all but the Bright House ones above are
        # on Time Warner's servers.
        preset = PROVIDER_PRESETS.get("twc.com")
    return preset


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


# An account whose server keeps its trash under another name (Deleted
# Items, Deleted Messages, or any folder carrying the \Trash attribute)
# is remembered here for this session by himalaya_client's
# ensure_trash_folder, so Delete and every other Trash user go there.
# Nothing is saved to disk; the next start finds it again.

import threading as _threading

_REMEMBERED_TRASH = {}
_REMEMBERED_TRASH_LOCK = _threading.Lock()


def remember_trash_folder(account_id, name):
    """Records the account's own trash folder for this session."""
    if not account_id or not name:
        return
    with _REMEMBERED_TRASH_LOCK:
        _REMEMBERED_TRASH[account_id] = name


def remembered_trash_folder(account_id):
    """The account's remembered trash folder, or None."""
    with _REMEMBERED_TRASH_LOCK:
        return _REMEMBERED_TRASH.get(account_id)


def forget_trash_folders():
    """Clears every remembered trash folder. For tests."""
    with _REMEMBERED_TRASH_LOCK:
        _REMEMBERED_TRASH.clear()
