import httpx

from prospector.enrich.website import (
    SiteCrawler,
    is_challenge_page,
    normalize_url,
    pick_inner_pages,
    site_domain,
)


def test_normalize_url():
    assert normalize_url("Example.com/path?utm_source=gmb&id=3#top") == "https://example.com/path?id=3"
    assert normalize_url("http://x.com") == "http://x.com/"
    assert normalize_url("") == ""


def test_site_domain():
    assert site_domain("https://www.Shop.co.uk:443/a") == "shop.co.uk"
    assert site_domain("shop.com") == "shop.com"


def test_pick_inner_pages_prefers_contact_and_about():
    links = [
        ("https://a.com/blog/2021/05/post", "Old post"),
        ("https://a.com/services/roof-repair", "Roof repair"),
        ("https://a.com/contact", "Contact"),
        ("https://a.com/privacy-policy", "Privacy"),
        ("https://other.com/contact", "Partner"),
        ("https://a.com/brochure.pdf", "Brochure"),
        ("https://a.com/about-us", "About"),
        ("https://a.com/", "Home"),
        ("https://a.com/gallery", "Gallery"),
    ]
    assert pick_inner_pages(links, "a.com", "https://a.com/", 3) == [
        "https://a.com/contact", "https://a.com/about-us", "https://a.com/services/roof-repair",
    ]


def test_challenge_detection():
    assert is_challenge_page("Just a moment...", "")
    assert is_challenge_page("", "Please verify you are human")
    assert not is_challenge_page("Roofing in Austin", "We fix roofs. " * 100)


async def test_crawl_collects_pages_and_emails(mock_http):
    data = await SiteCrawler(mock_http, max_pages=5).crawl("smile.example/?utm_source=gmb")
    assert data.ok and data.error == ""
    assert data.title == "Smile Dental"
    assert [p.url for p in data.pages] == [
        "https://smile.example/", "https://smile.example/contact-us", "https://smile.example/about",
    ]
    assert data.emails == ["billing@smile.example", "hello@smile.example"] or set(data.emails) == {
        "hello@smile.example", "billing@smile.example"}
    assert data.pages[0].description == "Family and implant dentistry"
    assert data.pages[0].chrome == "Contact About Old post"  # nav text kept apart from body text
    assert data.socials == {"facebook": "https://facebook.com/smile"}


async def test_crawl_respects_robots(mock_http):
    data = await SiteCrawler(mock_http, max_pages=5, respect_robots=True).crawl("https://plain.example/")
    assert [p.url for p in data.pages] == ["https://plain.example/"]
    data = await SiteCrawler(mock_http, max_pages=5, respect_robots=False).crawl("https://plain.example/")
    assert len(data.pages) == 2 and data.emails == ["office@gmail.com"]


async def test_crawl_flags_bot_protection(mock_http):
    data = await SiteCrawler(mock_http).crawl("https://blocked.example/")
    assert not data.ok and "bot protection" in data.error and data.pages == []


async def test_crawl_social_and_missing_sites(mock_http):
    assert "social media" in (await SiteCrawler(mock_http).crawl("https://facebook.com/biz")).error
    missing = await SiteCrawler(mock_http).crawl("https://nope.example/")
    assert not missing.ok and "404" in missing.error


async def test_crawl_skips_non_html():
    def handler(request):
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data = await SiteCrawler(client, respect_robots=False).crawl("https://pdf.example/")
    assert not data.ok and "not an HTML page" in data.error
