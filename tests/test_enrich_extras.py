import json

import httpx

from prospector.enrich.mx import INVALID, OK, OK_A, UNKNOWN, MxChecker, order_by_deliverability
from prospector.enrich.page import normalize_phone, parse_page, social_network
from prospector.enrich.signals import detect_signals, merge_signals
from prospector.enrich.website import SiteCrawler

PAGE = """
<html><head><title>Acme Roofing | Commercial Roofs</title>
<meta name="description" content="TPO and EPDM specialists">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "RoofingContractor", "name": "Acme Roofing",
 "email": "mailto:office@acmeroof.com", "telephone": "+1 (509) 555-0100",
 "sameAs": ["https://www.facebook.com/acmeroof", "https://www.linkedin.com/company/acme-roofing"],}
</script>
<link rel="stylesheet" href="/wp-content/themes/x/style.css">
<script src="https://assets.calendly.com/assets/external/widget.js"></script>
<script src="https://widget.podium.com/podium-widget.js"></script>
</head><body>
<header><nav><a href="/services">Services</a> <a href="tel:509-555-0199">Call</a></nav></header>
<h1>Commercial Roofing in Spokane</h1>
<p>We install single-ply membranes.</p>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a>
<a href="https://instagram.com/acme.roofing/">Instagram</a>
<a href="https://twitter.com/intent/tweet?text=hi">Tweet</a>
<form><input type="email" name="email"><textarea></textarea></form>
<footer>© Acme · Serving Eastern Washington</footer>
</body></html>"""


def test_parse_page_zones_structured_data_and_socials():
    page = parse_page(PAGE, "https://acmeroof.com/")
    assert page.title == "Acme Roofing | Commercial Roofs"
    assert page.description == "TPO and EPDM specialists"
    assert "Commercial Roofing in Spokane" in page.headings
    assert "single-ply membranes" in page.body and "Services" not in page.body
    assert "Services" in page.chrome and "Eastern Washington" in page.chrome
    assert page.emails == {"office@acmeroof.com"}  # from JSON-LD (with trailing comma tolerated)
    assert page.phones == ["+15095550100", "509-555-0199".replace("-", "")]
    assert page.socials == {
        "facebook": "https://www.facebook.com/acmeroof",
        "instagram": "https://instagram.com/acme.roofing/",
        "linkedin": "https://www.linkedin.com/company/acme-roofing",
    }
    assert {"WordPress", "Booking: Calendly", "Chat: Podium", "Contact form"} <= set(page.signals)


def test_inner_pages_drop_chrome():
    page = parse_page(PAGE, "https://acmeroof.com/about", keep_chrome=False)
    assert page.chrome == "" and "Services" not in page.text


def test_social_network_rules():
    assert social_network("https://x.com/acme") == "x"
    assert social_network("https://www.youtube.com/@acme") == "youtube"
    assert social_network("https://facebook.com/") is None
    assert social_network("https://facebook.com/sharer.php?u=1") is None
    assert social_network("https://example.com/facebook") is None


def test_normalize_phone():
    assert normalize_phone("tel:+1 (509) 555-0100") == "+15095550100"
    assert normalize_phone("123") == ""


def test_signals():
    assert detect_signals('<script src="https://cdn.shopify.com/x.js"></script>') == ["Shopify"]
    assert detect_signals("<p>hi</p>", visible_text="Book an appointment online today") == ["Booking: online form"]
    assert merge_signals(["Booking: online form"], ["Booking: Calendly"]) == ["Booking: Calendly"]


def doh_handler(answers: dict):
    def handler(request: httpx.Request):
        name, rtype = request.url.params["name"], request.url.params["type"]
        status, records = answers.get((name, rtype), (0, []))
        return httpx.Response(200, json={"Status": status, "Answer": records})
    return handler


async def test_mx_checker_statuses():
    answers = {
        ("good.com", "MX"): (0, [{"type": 15, "data": "10 mx.good.com."}]),
        ("nullmx.com", "MX"): (0, [{"type": 15, "data": "0 ."}]),
        ("gone.com", "MX"): (3, []),
        ("aonly.com", "MX"): (0, []),
        ("aonly.com", "A"): (0, [{"type": 1, "data": "1.2.3.4"}]),
        ("nothing.com", "MX"): (0, []),
        ("nothing.com", "A"): (0, []),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(doh_handler(answers))) as client:
        mx = MxChecker(client)
        checks = await mx.check_emails(["a@good.com", "b@nullmx.com", "c@gone.com", "d@aonly.com", "e@nothing.com", "f@good.com"])
    assert checks == {"a@good.com": OK, "b@nullmx.com": INVALID, "c@gone.com": INVALID,
                      "d@aonly.com": OK_A, "e@nothing.com": INVALID, "f@good.com": OK}
    assert order_by_deliverability(["c@gone.com", "a@good.com"], checks) == ["a@good.com", "c@gone.com"]


async def test_mx_checker_network_failure_is_unknown():
    def handler(request):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await MxChecker(client).check_domain("x.com") == UNKNOWN


async def test_crawler_guesses_contact_page_when_not_linked():
    pages = {
        "https://quiet.example/": "<html><head><title>Quiet Co</title></head><body><p>" + "We build things. " * 40
                                  + "</p><a href='/work'>Work</a></body></html>",
        "https://quiet.example/work": "<html><body><p>Portfolio</p></body></html>",
        "https://quiet.example/contact": "<html><body>Write to hello@quiet.example</body></html>",
    }

    def handler(request):
        url = str(request.url)
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"content-type": "text/html"})
        return httpx.Response(404, text="nope", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        data = await SiteCrawler(client, max_pages=3, respect_robots=False).crawl("https://quiet.example/")
    assert data.emails == ["hello@quiet.example"]
    assert "https://quiet.example/contact" in [p.url for p in data.pages]


def test_jsonld_list_and_microdata():
    html = """<script type="application/ld+json">[{"@type":"Dentist","telephone":"208-555-0101"},
      {"@type":"WebSite","name":"Ignore me"}]</script><span itemprop="email">front@smiles.example</span>"""
    page = parse_page(html, "https://smiles.example/")
    assert page.phones == ["2085550101"] and page.emails == {"front@smiles.example"}
    assert page.structured["types"] == ["Dentist", "WebSite"]
    json.dumps(page.structured)  # stays serialisable
