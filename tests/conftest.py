from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest

from prospector.config import Settings
from prospector.sources.base import Place


class FakeSource:
    """Listing source that returns canned places instead of hitting Google."""

    name = "fake"

    def __init__(self, places_by_query: dict[str, list[Place]]):
        self.places_by_query = places_by_query
        self.closed = False
        self.calls: list = []

    async def search(self, query: str, limit: int, area=None, want=None) -> AsyncIterator[Place]:
        self.calls.append((query, area))
        for p in self.places_by_query.get(query, [])[:limit]:
            yield Place(**{**p.to_dict(), "query": query})

    async def close(self) -> None:
        self.closed = True


SITES = {
    "https://smile.example/": """
        <html><head><title>Smile Dental</title><meta name="description" content="Family and implant dentistry"></head>
        <body><nav><a href="/contact-us">Contact</a> <a href="/about">About</a> <a href="/blog/2020/01/x">Old post</a></nav>
        <h1>Dental implants and sedation dentistry in Austin</h1>
        <p>We offer Invisalign, veneers and same-day crowns.</p>
        <a href="https://facebook.com/smile">Facebook</a></body></html>""",
    "https://smile.example/contact-us": """
        <html><head><title>Contact</title></head><body>
        <p>Email us: <a href="mailto:hello@smile.example">hello@smile.example</a></p>
        <p>Billing: billing [at] smile [dot] example</p>
        <img src="logo@2x.png"></body></html>""",
    "https://smile.example/about": "<html><body><p>Dr. Smith has placed 2,000 implants.</p></body></html>",
    "https://plain.example/": """
        <html><head><title>Plain Dental</title></head><body>
        <p>General dentistry and cleanings for the whole family. Now hiring hygienists.</p>
        <a href="/contact">Contact</a></body></html>""",
    "https://plain.example/contact": "<html><body>Call us at 555-0100. office@gmail.com</body></html>",
    "https://blocked.example/": "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>",
}


def site_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/robots.txt"):
        if "plain.example" in url:
            return httpx.Response(200, text="User-agent: *\nDisallow: /contact\n", headers={"content-type": "text/plain"})
        return httpx.Response(404)
    body = SITES.get(url)
    if body is None:
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


class FakeMx:
    """Offline stand-in for the DNS email check: *.dead domains can't receive mail."""

    async def check_emails(self, emails):
        return {e: ("invalid" if e.endswith(".dead") else "ok") for e in emails}

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def offline_mx(monkeypatch):
    from prospector import pipeline

    monkeypatch.setattr(pipeline, "MxChecker", FakeMx)


@pytest.fixture
def mock_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(site_handler), follow_redirects=True)


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", profiles_dir=tmp_path / "profiles")
    s.data_dir.mkdir()
    s.profiles_dir.mkdir()
    s.render_js = False
    s.site_concurrency = 3
    s.search_delay = 0
    return s


def sample_places() -> list[Place]:
    return [
        Place(name="Smile Dental", place_id="p1", category="Dentist", categories=["Dentist"],
              address="1 Main St, Austin, TX", phone="+1 512-000-0001", website="https://smile.example/?utm_source=gmb",
              rating=4.8, reviews=210, maps_url="https://maps.google.com/?cid=1"),
        Place(name="Plain Dental", place_id="p2", category="Dentist", categories=["Dentist"],
              address="2 Main St, Austin, TX", website="https://plain.example/", rating=4.5, reviews=40),
        Place(name="Low Rated Dental", place_id="p3", category="Dentist", rating=3.1, reviews=12,
              website="https://smile.example/"),
        Place(name="Blocked Dental", place_id="p4", category="Dentist", rating=4.9, reviews=99,
              website="https://blocked.example/"),
        Place(name="No Site Dental", place_id="p5", category="Dentist", rating=4.7, reviews=5),
    ]
