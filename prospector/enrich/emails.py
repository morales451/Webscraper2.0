"""Find email addresses in web pages, including common obfuscations."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import unquote

EMAIL_RE = re.compile(
    r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
    re.I,
)

# "name [at] domain [dot] com", "name (at) domain (dot) com", "name at domain dot com"
OBFUSCATED_RE = re.compile(
    r"([a-z0-9._%+-]{1,64})\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([a-z0-9-]+(?:\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*[a-z0-9-]+)+)",
    re.I,
)
DOT_RE = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I)

CFEMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
CF_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")

ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".pdf", ".avif",
)

# Placeholder, tracking and platform addresses that are never a business contact.
JUNK_DOMAINS = {
    "example.com", "example.org", "domain.com", "yourdomain.com", "email.com", "mysite.com",
    "yoursite.com", "website.com", "company.com", "sentry.io", "sentry-next.wixpress.com",
    "sentry.wixpress.com", "wixpress.com", "wix.com", "godaddy.com", "squarespace.com",
    "schema.org", "w3.org", "googleapis.com", "google.com", "gstatic.com", "cloudflare.com",
    "jquery.com", "wordpress.org", "wordpress.com", "sentry.zendesk.com", "test.com",
    "latofonts.com", "address.com", "mail.com", "sample.com", "ingest.sentry.io", "doe.com",
    "yourcompany.com", "yourbusiness.com", "companyname.com", "mydomain.com",
}
JUNK_LOCAL_PARTS = {"you", "your", "name", "yourname", "johndoe", "janedoe", "user", "username", "email", "firstname.lastname", "first.last"}

FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com",
    "msn.com", "comcast.net", "att.net", "verizon.net", "protonmail.com", "proton.me", "me.com",
    "ymail.com", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net",
}

LOW_PRIORITY_LOCALS = ("noreply", "no-reply", "donotreply", "do-not-reply", "privacy", "abuse", "webmaster", "postmaster")


def decode_cfemail(encoded: str) -> str:
    """Decode Cloudflare's email obfuscation (data-cfemail / email-protection#...)."""
    try:
        data = bytes.fromhex(encoded)
    except ValueError:
        return ""
    if not data:
        return ""
    key = data[0]
    return "".join(chr(b ^ key) for b in data[1:])


def _valid(email: str) -> bool:
    email = email.lower()
    local, _, domain = email.partition("@")
    if not local or not domain or ".." in email:
        return False
    if email.endswith(ASSET_SUFFIXES) or re.search(r"@\d+x\.", email):  # logo@2x.png
        return False
    if domain in JUNK_DOMAINS or any(domain.endswith("." + d) for d in JUNK_DOMAINS):
        return False
    if local in JUNK_LOCAL_PARTS or local.startswith(("your", "youremail", "someone", "example")):
        return False
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # hashes (e.g. Sentry DSNs)
        return False
    return True


def extract_emails(html: str) -> set[str]:
    """Return every plausible email address found in a page's HTML."""
    found: set[str] = set()
    if not html:
        return found

    for enc in CFEMAIL_RE.findall(html) + CF_HREF_RE.findall(html):
        decoded = decode_cfemail(enc)
        if EMAIL_RE.fullmatch(decoded or ""):
            found.add(decoded)

    text = html_lib.unescape(unquote(html.replace("%40", "@")))
    for m in EMAIL_RE.finditer(text):
        found.add(m.group(1))
    for m in OBFUSCATED_RE.finditer(text):
        candidate = f"{m.group(1)}@{DOT_RE.sub('.', m.group(2))}"
        if EMAIL_RE.fullmatch(candidate):
            found.add(candidate)

    return {e.lower().strip(".") for e in found if _valid(e)}


def rank_emails(emails: set[str] | list[str], site_domain: str = "") -> list[str]:
    """Best contact first: same domain as the website, then free providers, then others."""
    site_domain = (site_domain or "").lower()
    if site_domain.startswith("www."):
        site_domain = site_domain[4:]

    def score(email: str) -> tuple:
        local, _, domain = email.partition("@")
        same = bool(site_domain) and (domain == site_domain or domain.endswith("." + site_domain) or site_domain.endswith("." + domain))
        low = local.startswith(LOW_PRIORITY_LOCALS)
        return (
            low,
            0 if same else (1 if domain in FREE_PROVIDERS else 2),
            len(email),
            email,
        )

    return sorted(set(emails), key=score)
