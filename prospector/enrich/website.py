"""Crawl a business website: home page plus its most useful inner pages (contact, about,
services...). Returns the visible text of each page for keyword matching, and any email
addresses found along the way."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .emails import extract_emails, rank_emails

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_HTML_BYTES = 3_000_000
MAX_TEXT_CHARS = 200_000
THIN_TEXT_CHARS = 400  # below this, the page is probably rendered by JavaScript

# Interstitials served by bot-protection services instead of the real page. When a site
# shows one, we respect it: the lead is flagged for a manual look and not crawled further.
CHALLENGE_RE = re.compile(
    r"just a moment|attention required|robot challenge|are you a robot|access denied|"
    r"security check|ddos-guard|verify you are human|checking your browser|captcha",
    re.I,
)

SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "yelp.com", "pinterest.com", "nextdoor.com", "linktr.ee",
)

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".mp3", ".doc",
    ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".ics", ".xml", ".json", ".css", ".js",
)

# Inner pages worth reading, best first. Matched against the URL path and the link text.
PAGE_HINTS = [
    (re.compile(r"contact|get-in-touch|reach-us|kontakt", re.I), 10),
    (re.compile(r"about|who-we-are|our-story|company|team|staff|leadership", re.I), 8),
    (re.compile(r"service|what-we-do|solutions|offerings|capabilities|treatments|procedures", re.I), 7),
    (re.compile(r"product|shop|menu|pricing|plans|packages", re.I), 5),
    (re.compile(r"industr|specialt|expertise|portfolio|projects|work|case-stud", re.I), 4),
    (re.compile(r"location|areas?-we-serve|service-area|faq", re.I), 3),
]
SKIP_PAGE_RE = re.compile(
    r"login|log-in|signin|sign-in|register|cart|checkout|account|privacy|terms|cookie|policy|"
    r"wp-admin|wp-login|feed|tag/|category/|author/|page/\d|\?replytocom|blog/\d{4}|/\d{4}/\d{2}/",
    re.I,
)


@dataclass
class PageContent:
    url: str
    title: str
    text: str


@dataclass
class SiteData:
    url: str
    domain: str = ""
    ok: bool = False
    error: str = ""
    title: str = ""
    description: str = ""
    pages: list[PageContent] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    rendered: bool = False

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


# --------------------------------------------------------------------------- URL helpers
def normalize_url(url: str) -> str:
    """Add a scheme, drop fragments and tracking parameters (utm_*, gclid...)."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    url, _ = urldefrag(url)
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("gclid", "fbclid", "y_source", "msclkid")]
    path = parts.path or "/"
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, "", urlencode(query), ""))


def site_domain(url: str) -> str:
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_social_url(url: str) -> bool:
    d = site_domain(url)
    return any(d == s or d.endswith("." + s) for s in SOCIAL_HOSTS)


def is_challenge_page(title: str, text: str) -> bool:
    return bool(CHALLENGE_RE.search(title or "")) or (len(text) < 600 and bool(CHALLENGE_RE.search(text[:600])))


def _same_site(url: str, domain: str) -> bool:
    d = site_domain(url)
    return d == domain or d.endswith("." + domain) or domain.endswith("." + d)


# --------------------------------------------------------------------------- HTML parsing
def parse_html(html: str, base_url: str, strip_chrome: bool = False) -> tuple[str, str, str, list[tuple[str, str]]]:
    """Return (title, meta description, visible text, [(absolute link, link text)]).

    ``strip_chrome`` drops the header/nav/footer, which repeat on every page of a site —
    used for inner pages so the same menu text isn't counted once per page."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""

    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "sms:")):
            continue
        links.append((urljoin(base_url, href), a.get_text(" ", strip=True)[:80]))

    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe", "head"]):
        tag.decompose()
    if strip_chrome:
        for tag in soup(["header", "nav", "footer"]):
            tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)[:MAX_TEXT_CHARS]
    if description and description not in text:
        text = f"{description} {text}"
    return title, description, text, links


def pick_inner_pages(links: list[tuple[str, str]], domain: str, home_url: str, limit: int) -> list[str]:
    scored: dict[str, int] = {}
    home = normalize_url(home_url).rstrip("/")
    for url, label in links:
        if not url.startswith(("http://", "https://")) or not _same_site(url, domain):
            continue
        clean = normalize_url(url)
        path = urlparse(clean).path.lower()
        if clean.rstrip("/") == home or path.endswith(SKIP_EXTENSIONS) or SKIP_PAGE_RE.search(clean):
            continue
        hay = f"{path} {label.lower()}"
        score = max((w for rx, w in PAGE_HINTS if rx.search(hay)), default=0)
        if score == 0:
            # Shallow pages are often service pages ("/roof-repair") — keep them as a fallback.
            depth = len([p for p in path.split("/") if p])
            score = 1 if depth == 1 else 0
        if score and scored.get(clean, 0) < score:
            scored[clean] = score
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0])))
    return [u for u, _ in ranked[:limit]]


# --------------------------------------------------------------------------- JS renderer
class Renderer:
    """Renders JavaScript-heavy pages with Chromium. Shared across all sites in a run."""

    def __init__(self, chromium_path: str | None = None, concurrency: int = 2, timeout: float = 25.0):
        self.chromium_path = chromium_path or None
        self.timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._context = None
        self.available = True

    async def _ensure(self):
        async with self._lock:
            if self._context is not None or not self.available:
                return self._context
            try:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                kwargs = {"headless": True}
                if self.chromium_path:
                    kwargs["executable_path"] = self.chromium_path
                self._browser = await self._pw.chromium.launch(**kwargs)
                self._context = await self._browser.new_context(user_agent=USER_AGENT, locale="en-US")

                async def _route(route):
                    if route.request.resource_type in ("image", "media", "font"):
                        await route.abort()
                    else:
                        await route.continue_()

                await self._context.route("**/*", _route)
            except Exception:
                self.available = False
                if self._pw is not None:
                    await self._pw.stop()
                    self._pw = None
            return self._context

    async def render(self, url: str) -> tuple[str, str] | None:
        """Return (final_url, html) or None if rendering isn't possible."""
        context = await self._ensure()
        if context is None:
            return None
        async with self._sem:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                return page.url, await page.content()
            except Exception:
                return None
            finally:
                await page.close()

    async def close(self):
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self._pw = self._browser = self._context = None


# --------------------------------------------------------------------------- crawler
def make_http_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )


class SiteCrawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        max_pages: int = 5,
        respect_robots: bool = True,
        renderer: Renderer | None = None,
    ):
        self.client = client
        self.max_pages = max(1, max_pages)
        self.respect_robots = respect_robots
        self.renderer = renderer

    async def _fetch(self, url: str) -> tuple[str, str]:
        """GET a page; return (final_url, html). Raises on errors / non-HTML responses."""
        async with self.client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
            ctype = resp.headers.get("content-type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype:
                raise ValueError(f"not an HTML page ({ctype.split(';')[0]})")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_HTML_BYTES:
                    break
            raw = b"".join(chunks)
            encoding = resp.charset_encoding or "utf-8"
            try:
                html = raw.decode(encoding, errors="replace")
            except LookupError:
                html = raw.decode("utf-8", errors="replace")
            return str(resp.url), html

    async def _robots(self, home_url: str) -> RobotFileParser | None:
        if not self.respect_robots:
            return None
        parts = urlparse(home_url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = await self.client.get(robots_url, timeout=8)
        except Exception:
            return None
        if resp.status_code >= 400 or "html" in resp.headers.get("content-type", ""):
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp

    async def crawl(self, url: str) -> SiteData:
        url = normalize_url(url)
        data = SiteData(url=url, domain=site_domain(url))
        if not url:
            data.error = "no website"
            return data
        if is_social_url(url):
            data.error = "website is a social media profile"
            return data

        # Home page (retry over plain http:// if https fails to connect).
        attempts = [url]
        if url.startswith("https://"):
            attempts.append("http://" + url[len("https://"):])
        home_html, last_error = None, ""
        for attempt in attempts:
            try:
                final_url, home_html = await self._fetch(attempt)
                break
            except Exception as exc:
                last_error = _describe_error(exc)
                home_html = None
        if home_html is None:
            if self.renderer is not None:
                rendered = await self.renderer.render(url)
                if rendered:
                    final_url, home_html = rendered
                    data.rendered = True
            if home_html is None:
                data.error = last_error or "could not load website"
                return data

        data.url = final_url
        data.domain = site_domain(final_url)

        robots = await self._robots(final_url)

        def allowed(u: str) -> bool:
            return robots is None or robots.can_fetch("*", u)

        title, description, text, links = parse_html(home_html, final_url)
        if is_challenge_page(title, text):
            data.error = "website blocks automated visitors (bot protection) — check it manually"
            return data
        if len(text) < THIN_TEXT_CHARS and self.renderer is not None and not data.rendered:
            rendered = await self.renderer.render(final_url)
            if rendered:
                r_title, r_desc, r_text, r_links = parse_html(rendered[1], rendered[0])
                if len(r_text) > len(text) and not is_challenge_page(r_title, r_text):
                    title, description, text, links = r_title or title, r_desc or description, r_text, r_links
                    home_html = rendered[1]
                    data.rendered = True

        data.title, data.description = title, description
        data.pages.append(PageContent(final_url, title, text))
        emails = extract_emails(home_html)

        inner = [u for u in pick_inner_pages(links, data.domain, final_url, self.max_pages - 1) if allowed(u)]
        sem = asyncio.Semaphore(3)

        async def fetch_inner(u: str):
            async with sem:
                try:
                    f_url, html = await self._fetch(u)
                except Exception:
                    return None
                t, _d, txt, _l = parse_html(html, f_url, strip_chrome=True)
                if is_challenge_page(t, txt):
                    return None
                if len(txt) < THIN_TEXT_CHARS and data.rendered and self.renderer is not None:
                    rendered = await self.renderer.render(u)
                    if rendered:
                        html = rendered[1]
                        t, _d, txt, _l = parse_html(html, rendered[0], strip_chrome=True)
                return PageContent(f_url, t, txt), extract_emails(html)

        seen_urls = {normalize_url(final_url).rstrip("/")}
        for result in await asyncio.gather(*(fetch_inner(u) for u in inner)):
            if result is None:
                continue
            page, page_emails = result
            key = normalize_url(page.url).rstrip("/")
            if key in seen_urls:
                continue
            seen_urls.add(key)
            data.pages.append(page)
            emails |= page_emails

        data.emails = rank_emails(emails, data.domain)
        data.ok = True
        return data


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "website timed out"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc)
        if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
            return "website has an SSL/certificate problem"
        return "could not connect to website"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"website error: {exc.__class__.__name__}"
