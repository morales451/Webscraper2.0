"""Parse one HTML page into the pieces the rest of the app needs.

Text is split into *zones* so scoring can tell a keyword in the page title or a heading
(a strong signal) from one that only appears in the navigation menu or footer (weak).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .emails import EMAIL_RE, extract_emails
from .signals import detect_signals

MAX_TEXT_CHARS = 200_000

# network -> host suffixes. Share/intent links are not profiles and are skipped.
SOCIAL_NETWORKS = {
    "facebook": ("facebook.com", "fb.com"),
    "instagram": ("instagram.com",),
    "linkedin": ("linkedin.com",),
    "x": ("twitter.com", "x.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
    "pinterest": ("pinterest.com",),
    "yelp": ("yelp.com",),
}
_SOCIAL_SKIP = re.compile(r"/(sharer|share|intent|plugins|dialog|tr|embed|watch|hashtag|search|login|home\.php)\b|[?&]u=", re.I)


@dataclass
class ParsedPage:
    url: str
    title: str = ""
    description: str = ""
    headings: str = ""
    body: str = ""
    chrome: str = ""  # header / nav / footer
    links: list[tuple[str, str]] = field(default_factory=list)
    emails: set[str] = field(default_factory=set)
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    structured: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(x for x in (self.description, self.headings, self.body, self.chrome) if x)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _walk_jsonld(node, out: dict) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out)
        return
    if not isinstance(node, dict):
        return
    t = node.get("@type")
    for typ in (t if isinstance(t, list) else [t]):
        if isinstance(typ, str):
            out.setdefault("types", []).append(typ)
    if isinstance(node.get("name"), str) and "name" not in out and node.get("@type") not in ("WebPage", "WebSite", "BreadcrumbList"):
        out["name"] = node["name"]
    for key, target in (("email", "emails"), ("telephone", "phones"), ("sameAs", "same_as")):
        val = node.get(key)
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            if isinstance(v, str) and v.strip():
                out.setdefault(target, []).append(v.strip())
    for v in node.values():
        if isinstance(v, (dict, list)):
            _walk_jsonld(v, out)


def parse_structured_data(soup: BeautifulSoup) -> dict:
    """schema.org JSON-LD + microdata: business name, emails, phones, social profiles."""
    out: dict = {}
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites concatenate objects or leave trailing commas; salvage what we can.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        _walk_jsonld(data, out)
    for el in soup.select("[itemprop=email]"):
        out.setdefault("emails", []).append(el.get("content") or el.get_text(" ", strip=True))
    for el in soup.select("[itemprop=telephone]"):
        out.setdefault("phones", []).append(el.get("content") or el.get_text(" ", strip=True))
    return out


def normalize_phone(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.lower().startswith("tel:"):
        raw = raw[4:]
    digits = re.sub(r"[^\d+]", "", raw)
    return digits if 7 <= len(digits.lstrip("+")) <= 15 else ""


def social_network(url: str) -> str | None:
    host = urlparse(url).netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    for network, hosts in SOCIAL_NETWORKS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            path = urlparse(url).path.strip("/")
            if not path or _SOCIAL_SKIP.search(url):
                return None
            return network
    return None


def parse_page(html: str, url: str, keep_chrome: bool = True) -> ParsedPage:
    soup = BeautifulSoup(html or "", "lxml")
    page = ParsedPage(url=url)
    page.title = _clean(soup.title.get_text(" ")) if soup.title else ""
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    page.description = _clean(desc.get("content")) if desc else ""

    page.structured = parse_structured_data(soup)

    phones: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("tel:"):
            phones.append(href)
            continue
        if low.startswith(("mailto:", "javascript:", "#", "sms:")):
            continue
        absolute = urljoin(url, href)
        page.links.append((absolute, a.get_text(" ", strip=True)[:80]))
        network = social_network(absolute)
        if network and network not in page.socials:
            page.socials[network] = absolute.split("?")[0]
    for same in page.structured.get("same_as", []):
        network = social_network(same)
        if network and network not in page.socials:
            page.socials[network] = same.split("?")[0]
    # The phone declared in structured data is the business's official one: list it first.
    phones = page.structured.get("phones", []) + phones
    seen: set[str] = set()
    for p in phones:
        n = normalize_phone(p)
        if n and n[-10:] not in seen:  # "+1 208…" and "208…" are the same number
            seen.add(n[-10:])
            page.phones.append(n)

    page.emails = extract_emails(html)
    for e in page.structured.get("emails", []):
        e = e.replace("mailto:", "").strip()
        if EMAIL_RE.fullmatch(e):
            page.emails |= extract_emails(e)

    has_form = any(
        f.find("textarea") or f.find("input", attrs={"type": "email"})
        for f in soup.find_all("form")
    )

    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe", "head"]):
        tag.decompose()
    chrome_parts = []
    for tag in soup(["header", "nav", "footer"]):
        if keep_chrome:
            chrome_parts.append(tag.get_text(" ", strip=True))
        tag.decompose()
    page.chrome = _clean(" ".join(chrome_parts))[:20_000]
    page.headings = _clean(" · ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])))[:5_000]
    page.body = _clean(soup.get_text(" "))[:MAX_TEXT_CHARS]
    page.signals = detect_signals(html, has_form=has_form, visible_text=page.body)
    return page
