"""Crawl a business website: home page plus its most useful inner pages (contact, about,
services...). Returns each page's text (split into zones for scoring), plus the emails,
phone numbers, social profiles and website signals found along the way."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .emails import rank_emails
from .page import ParsedPage, parse_page
from .signals import merge_signals

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_HTML_BYTES = 3_000_000
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
# Tried directly when a site's links don't lead to a contact page (or no email turned up).
GUESS_PATHS = ("/contact", "/contact-us", "/about", "/about-us")
CONTACT_RE = re.compile(r"contact|get-in-touch|reach-us", re.I)

SKIP_PAGE_RE = re.compile(
    r"login|log-in|signin|sign-in|register|cart|checkout|account|privacy|terms|cookie|policy|"
    r"wp-admin|wp-login|feed|tag/|category/|author/|page/\d|\?replytocom|blog/\d{4}|/\d{4}/\d{2}/",
    re.I,
)


@dataclass
class PageContent:
    url: str
    title: str
    text: str  # main body text
    headings: str = ""
    description: str = ""
    chrome: str = ""  # header/nav/footer — only kept for the home page (it repeats on every page)


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
    email_checks: dict[str, str] = field(default_factory=dict)
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
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


# --------------------------------------------------------------------------- page selection
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

    async def _load(self, url: str, inner: bool, use_renderer: bool) -> ParsedPage | None:
        """Fetch and parse one inner page (None on errors, 404s and bot walls)."""
        try:
            final, html = await self._fetch(url)
        except Exception:
            return None
        page = parse_page(html, final, keep_chrome=not inner)
        if is_challenge_page(page.title, page.text):
            return None
        if len(page.text) < THIN_TEXT_CHARS and use_renderer and self.renderer is not None:
            rendered = await self.renderer.render(url)
            if rendered:
                page = parse_page(rendered[1], rendered[0], keep_chrome=not inner)
        return page

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

        home = parse_page(home_html, final_url)
        if is_challenge_page(home.title, home.text):
            data.error = "website blocks automated visitors (bot protection) — check it manually"
            return data
        if len(home.text) < THIN_TEXT_CHARS and self.renderer is not None and not data.rendered:
            rendered = await self.renderer.render(final_url)
            if rendered:
                r_home = parse_page(rendered[1], rendered[0])
                if len(r_home.text) > len(home.text) and not is_challenge_page(r_home.title, r_home.text):
                    home = r_home
                    data.rendered = True

        data.title, data.description = home.title, home.description
        pages: list[ParsedPage] = [home]

        origin = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
        inner = pick_inner_pages(home.links, data.domain, final_url, self.max_pages - 1)
        if not any(CONTACT_RE.search(u) for u in inner):
            # No contact link we could follow (JS menus, image links...): try the usual address.
            inner = [origin + GUESS_PATHS[0], *inner][: max(self.max_pages - 1, 1)]
        inner = [u for u in inner if allowed(u)]

        sem = asyncio.Semaphore(3)
        seen_urls = {normalize_url(final_url).rstrip("/")}

        async def load(u: str) -> ParsedPage | None:
            async with sem:
                return await self._load(u, inner=True, use_renderer=data.rendered)

        def add(page: ParsedPage | None) -> None:
            if page is None:
                return
            key = normalize_url(page.url).rstrip("/")
            if key in seen_urls:
                return
            seen_urls.add(key)
            pages.append(page)

        for page in await asyncio.gather(*(load(u) for u in inner)):
            add(page)

        # Still no email? Try the usual contact/about addresses we haven't read yet.
        if not any(p.emails for p in pages):
            tried = set(seen_urls) | {normalize_url(u).rstrip("/") for u in inner}
            for path in GUESS_PATHS:
                guess = origin + path
                if normalize_url(guess).rstrip("/") in tried or not allowed(guess):
                    continue
                tried.add(normalize_url(guess).rstrip("/"))
                page = await load(guess)
                add(page)
                if page is not None and page.emails:
                    break

        emails: set[str] = set()
        for page in pages:
            data.pages.append(PageContent(page.url, page.title, page.body, page.headings, page.description, page.chrome))
            emails |= page.emails
            for p in page.phones:
                if all(p[-10:] != q[-10:] for q in data.phones):
                    data.phones.append(p)
            for network, link in page.socials.items():
                data.socials.setdefault(network, link)
        data.signals = merge_signals(*(p.signals for p in pages))
        if final_url.startswith("http://"):
            data.signals.append("No SSL")
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
