"""
The tracker and ad domain list, and its daily refresh.

This is the second layer of ZBox's blocking. The first
(content_blocker.py) strips every remote reference out of a message
before it renders, which needs no list at all and is what protects
you by default. This list is what still protects you once you press
"Show remote content" on a message, or turn remote content back on
for a sender you trust: the pictures load, the beacons do not.

Design notes
------------
Domain-level matching only. Email is not a web app -- there are no
XHRs, no cookies worth reading, no script to filter -- so the path
half of a filter rule buys nothing here and the domain half does all
the work. That is also what keeps the parser small enough to be
stdlib-only, which ZBox requires.

Two source formats are read. Hosts files ("0.0.0.0 ads.example.com")
cover ad and malware serving. Adblock-syntax lists contribute their
domain-anchored rules ("||tracker.example.com^"), which is the part
of EasyPrivacy aimed at tracking rather than cosmetics. Rules with a
path, a wildcard, an exception (@@), an element-hiding selector, or a
$domain= context restriction are skipped: they cannot be honoured
faithfully at domain level, and a rule half-honoured over-blocks.

Failure is always non-fatal. A refresh that cannot reach the network
leaves the previous list in place and records why; a first run with
no network falls back to SEED_DOMAINS, a small hand-kept list of
email-tracking hosts, so "blocklist on" is never a no-op.
"""

import json
import logging
import os
import re
import time
import urllib.request
import lang

logger = logging.getLogger("zbox.blocklist")


UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
USER_AGENT = "ZBox (accessible mail client; blocklist updater)"

# (label, url, format). Order is cosmetic; results are merged.
EASYPRIVACY_BASE = (
    "https://raw.githubusercontent.com/easylist/easylist/master/easyprivacy/"
)

SOURCES = (
    # Ads and malware serving. The unified list already folds in Peter
    # Lowe's ad-server list, so that is not fetched separately.
    ("StevenBlack unified hosts",
     "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
     "hosts"),
    # EasyPrivacy, taken from the upstream repository rather than the
    # assembled easylist.to download. Only the sections that are
    # domain-anchored rules are fetched -- the tracking-server and
    # third-party files -- because those are what survives this
    # parser. easyprivacy_general.txt is path and regex rules (2
    # usable domains out of 3,000 lines) and the "specific" files are
    # site-context rules, which mean nothing inside an email. One host
    # serves all of these, which is one fewer thing for a corporate
    # proxy to block.
    ("EasyPrivacy tracking servers",
     EASYPRIVACY_BASE + "easyprivacy_trackingservers.txt", "adblock"),
    ("EasyPrivacy tracking servers (general)",
     EASYPRIVACY_BASE + "easyprivacy_trackingservers_general.txt", "adblock"),
    ("EasyPrivacy tracking servers (international)",
     EASYPRIVACY_BASE + "easyprivacy_trackingservers_international.txt",
     "adblock"),
    ("EasyPrivacy third party",
     EASYPRIVACY_BASE + "easyprivacy_thirdparty.txt", "adblock"),
    ("EasyPrivacy third party (international)",
     EASYPRIVACY_BASE + "easyprivacy_thirdparty_international.txt", "adblock"),
)

# Enough to be useful before the first successful download, and on a
# machine that never gets one. Email open-tracking endpoints, not ads.
SEED_DOMAINS = frozenset((
    "click.mailchimp.com", "list-manage.com", "mailchimp.com",
    "sendgrid.net", "sendgrid.com", "ct.sendgrid.net",
    "mandrillapp.com", "mailgun.org", "email.mailgun.org",
    "sparkpostmail.com", "postmarkapp.com", "pstmrk.it",
    "constantcontact.com", "rs6.net", "cmail19.com", "cmail20.com",
    "createsend.com", "campaign-archive.com",
    "hubspot.com", "hs-sites.com", "hubspotemail.net",
    "marketo.com", "mktoresp.com", "marketo.net",
    "salesforce.com", "exacttarget.com", "et.exacttarget.com",
    "pardot.com", "eloqua.com", "en25.com",
    "mailtrack.io", "streak.com", "bananatag.com", "yesware.com",
    "getnotify.com", "didtheyreadit.com", "contactmonkey.com",
    "sidekickopen.com", "toutapp.com", "mixmax.com",
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "scorecardresearch.com", "branch.io", "adjust.com", "appsflyer.com",
    "omtrdc.net", "demdex.net", "everesttech.net",
    "klclick.com", "klclick1.com", "klaviyomail.com",
    "braze.com", "iterable.com", "links.iterable.com",
    "customeriomail.com", "e.customeriomail.com",
))

_HOSTS_SKIP = frozenset((
    "localhost", "localhost.localdomain", "local", "broadcasthost",
    "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
    "ip6-allnodes", "ip6-allrouters", "0.0.0.0",
))

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_ABP_RULE_RE = re.compile(r"^\|\|([^\^/\*\$]+)(\^|\$|$)")


def _valid_domain(candidate):
    candidate = (candidate or "").strip().strip(".").lower()
    if not candidate or len(candidate) > 253:
        return None
    if candidate in _HOSTS_SKIP:
        return None
    if not _DOMAIN_RE.match(candidate):
        return None
    return candidate


def parse_hosts(text):
    """Domains from a hosts-format list. Only lines that actually map
    a name to a null address count; a bare name is ambiguous."""
    found = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        if fields[0] not in ("0.0.0.0", "127.0.0.1", "::", "::1"):
            continue
        for name in fields[1:]:
            domain = _valid_domain(name)
            if domain:
                found.add(domain)
    return found


def parse_adblock(text):
    """The domain-anchored subset of an Adblock Plus filter list.

    Kept: ||domain^ and ||domain^$options. Skipped: exceptions (@@),
    element hiding (##, #@#, #?#), wildcards, rules with a path, and
    rules restricted by $domain= -- none of which can be applied
    faithfully with only a hostname to match against.
    """
    found = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "![":
            continue
        if line.startswith("@@"):
            continue
        if "##" in line or "#@#" in line or "#?#" in line or "#$#" in line:
            continue
        if not line.startswith("||"):
            continue
        match = _ABP_RULE_RE.match(line)
        if not match:
            continue
        options = line.split("$", 1)[1].lower() if "$" in line else ""
        if "domain=" in options or "~third-party" in options:
            continue
        domain = _valid_domain(match.group(1))
        if domain:
            found.add(domain)
    return found


def parse_domains(text):
    """A plain list, one domain per line, as phishing feeds publish
    them. Comment lines (# or !) and anything that is not a valid
    domain are skipped."""
    found = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#!":
            continue
        domain = _valid_domain(line.split()[0])
        if domain:
            found.add(domain)
    return found


PARSERS = {"hosts": parse_hosts, "adblock": parse_adblock, "domains": parse_domains}


def _download(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        raw = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise ValueError("list is larger than %d bytes" % MAX_DOWNLOAD_BYTES)
    return raw.decode("utf-8", "replace")


class Blocklist:
    """Owns the merged domain set on disk and in memory.

    Not thread-safe to update from two threads at once, which nothing
    does: refreshes run one at a time on the low-priority sync worker
    and only swap the in-memory set once a full parse has succeeded.

    The class attributes below are what PhishingList changes; the
    loading, matching and refresh machinery is shared.
    """

    DIRECTORY = "blocklists"
    LABEL = "Blocklist"
    SOURCE_LIST = SOURCES
    SEED = SEED_DOMAINS

    def label(self):
        """This list's name in the chosen language, for messages the
        reader sees. LABEL stays English for the log."""
        return lang.t("dialogs", "bl_label_" + self.DIRECTORY, default=self.LABEL)
    # Listed entries that are dropped rather than matched. Empty for
    # the ad and tracker list.
    NEVER_LISTED = frozenset()

    def __init__(self, paths):
        self.paths = paths
        self.directory = os.path.join(paths.userdata, self.DIRECTORY)
        self.domains_file = os.path.join(self.directory, "domains.txt")
        self.meta_file = os.path.join(self.directory, "meta.json")
        self.domains = set()
        self.meta = {}
        self.load()

    # -- disk ---------------------------------------------------------

    def load(self):
        self.meta = self._read_meta()
        domains = set()
        try:
            with open(self.domains_file, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        domains.add(line)
        except OSError:
            domains = set()
        if not domains:
            # Nothing downloaded yet (or the file was lost): the seed
            # list keeps the feature meaningful in the meantime.
            domains = set(self.SEED)
            if self.SEED:
                self.meta.setdefault("source", "built-in seed list")
        self.domains = domains - self.NEVER_LISTED
        return len(self.domains)

    def _read_meta(self):
        try:
            with open(self.meta_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_meta(self):
        os.makedirs(self.directory, exist_ok=True)
        temp = self.meta_file + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.meta, handle, indent=2)
        os.replace(temp, self.meta_file)

    # -- matching -----------------------------------------------------

    def is_blocked(self, host):
        """True if the host, or any parent domain of it, is listed.
        ads.tracker.com is blocked by a rule for tracker.com; the
        reverse is not true."""
        host = (host or "").strip().strip(".").lower()
        if not host or not self.domains:
            return False
        if host in self.domains:
            return True
        parts = host.split(".")
        for index in range(1, len(parts) - 1):
            if ".".join(parts[index:]) in self.domains:
                return True
        return False

    # -- state --------------------------------------------------------

    @property
    def last_updated(self):
        try:
            return float(self.meta.get("last_updated") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def count(self):
        return len(self.domains)

    def needs_update(self, interval=UPDATE_INTERVAL_SECONDS):
        return (time.time() - self.last_updated) >= interval

    def status_text(self):
        """One line for Settings, written to be read aloud."""
        if not self.last_updated:
            if not self.SEED:
                # PhishingList has no built-in list, so "0 domains
                # from the built-in list" would be wrong.
                line = lang.t('dialogs', 'bl_not_downloaded', default="Not downloaded yet.")
                error = self.meta.get("last_error")
                if error:
                    line += (' ' + lang.t('dialogs', 'bl_last_failed', default='Last attempt failed: %s')) % error
                return line
            return (
                lang.t('dialogs', 'bl_seed_only', default="%s domains from the built-in list. Never updated from the "
                "internet yet.") % f"{self.count:,}"
            )
        age = time.time() - self.last_updated
        if age < 3600:
            when = "less than an hour ago"
        elif age < UPDATE_INTERVAL_SECONDS:
            hours = int(age // 3600)
            when = "%d hour%s ago" % (hours, "" if hours == 1 else "s")
        else:
            days = int(age // UPDATE_INTERVAL_SECONDS)
            when = (
                lang.t("dialogs", "bl_day_ago", default="1 day ago")
                if days == 1
                else lang.t("dialogs", "bl_days_ago", default="{days} days ago", days=days)
            )
        line = lang.t('dialogs', 'bl_status_line', default="%s domains. Last updated %s.") % (f"{self.count:,}", when)
        error = self.meta.get("last_error")
        if error:
            # A partial success still updates the list, so this is
            # "some sources are missing", not "the update failed".
            line += (' ' + lang.t('dialogs', 'bl_some_unreachable', default='Some sources could not be reached: %s')) % error
        return line

    # -- refresh ------------------------------------------------------

    def update(self, force=False):
        """Downloads and merges every source. Runs on a worker thread;
        never call it from the UI thread. Returns (changed, message)."""
        if not force and not self.needs_update():
            return False, lang.t('dialogs', 'bl_up_to_date', default="%s is already up to date.") % self.label()

        merged = set()
        counts = {}
        errors = []
        for label, url, kind in self.SOURCE_LIST:
            try:
                text = _download(url)
                found = PARSERS[kind](text)
                if not found:
                    raise ValueError("no usable rules found")
                counts[label] = len(found)
                merged |= found
                logger.info("%s source %s: %d domains", self.LABEL, label, len(found))
            except Exception as exc:
                errors.append("%s (%s)" % (label, exc))
                logger.warning("%s source %s failed: %s", self.LABEL, label, exc)

        if not merged:
            self.meta["last_error"] = "; ".join(errors) or "no sources reachable"
            self.meta["last_attempt"] = time.time()
            try:
                self._write_meta()
            except OSError:
                pass
            # Whatever was already loaded stays loaded.
            return False, lang.t('dialogs', 'bl_update_failed', default="Could not update the %s: %s") % (
                self.label().lower(), self.meta["last_error"],
            )

        merged |= set(self.SEED)
        merged -= self.NEVER_LISTED

        try:
            os.makedirs(self.directory, exist_ok=True)
            temp = self.domains_file + ".tmp"
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(sorted(merged)))
                handle.write("\n")
            os.replace(temp, self.domains_file)
        except OSError as exc:
            logger.warning("Could not write the %s: %s", self.LABEL.lower(), exc)
            return False, lang.t('dialogs', 'bl_save_failed', default="Downloaded the %s but could not save it: %s") % (self.label().lower(), exc)

        self.domains = merged
        self.meta = {
            "last_updated": time.time(),
            "last_attempt": time.time(),
            "total": len(merged),
            "counts": counts,
            "last_error": "; ".join(errors),
        }
        try:
            self._write_meta()
        except OSError:
            pass

        message = lang.t('dialogs', 'bl_updated', default="%s updated: %s domains.") % (self.label(), f"{len(merged):,}")
        if errors:
            message += (' ' + lang.t('dialogs', 'bl_some_failed', default='Some sources failed: %s')) % "; ".join(errors)
        return True, message


# Phishing domains, for the junk rules (junk_rules.py). Openly
# licensed sources only: Phishing.Database is MIT. URLhaus was dropped
# on 16 September 2026 -- the abuse.ch Terms of Use of 4 November 2025
# limit it to authenticated, not-for-profit use, and it added only 395
# of 391,734 domains.
PHISHING_SOURCES = (
    ("Phishing.Database active domains",
     "https://raw.githubusercontent.com/Phishing-Database/Phishing.Database/"
     "master/phishing-domains-ACTIVE.txt",
     "domains"),
)

# Shared platforms a feed names outright because one page on them was
# abused. Matching them would flag every Google Docs or GitHub link in
# ordinary mail, so the bare entry is dropped; a listed subdomain of
# one (someone.github.io) still matches. Checked 16 September 2026:
# Phishing.Database listed docs.google.com and sites.google.com.
PHISHING_NEVER_LISTED = frozenset((
    "google.com", "docs.google.com", "sites.google.com", "drive.google.com",
    "forms.gle", "storage.googleapis.com", "firebasestorage.googleapis.com",
    "microsoft.com", "live.com", "outlook.com", "office.com",
    "onedrive.live.com", "1drv.ms", "sharepoint.com",
    "github.com", "github.io", "raw.githubusercontent.com",
    "dropbox.com", "amazonaws.com", "s3.amazonaws.com",
    "apple.com", "icloud.com", "yahoo.com", "groups.io",
    "bit.ly", "t.co", "tinyurl.com",
))


class PhishingList(Blocklist):
    """Known phishing and malware domains, matched against the links in
    new Inbox mail. No seed list: until the first download it matches
    nothing, which is the honest state. Loaded lazily by main_frame
    only while the junk rules are on -- a few hundred thousand domains
    is real memory."""

    DIRECTORY = "phishing"
    LABEL = "Phishing list"
    SOURCE_LIST = PHISHING_SOURCES
    SEED = frozenset()
    NEVER_LISTED = PHISHING_NEVER_LISTED
