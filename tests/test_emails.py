from prospector.enrich.emails import decode_cfemail, extract_emails, rank_emails


def _cf_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


def test_finds_plain_and_mailto_emails():
    html = '<a href="mailto:Info@Acme.com?subject=hi">Write</a> or sales@acme.com.'
    assert extract_emails(html) == {"info@acme.com", "sales@acme.com"}


def test_decodes_cloudflare_protection():
    enc = _cf_encode("owner@shop.co")
    assert decode_cfemail(enc) == "owner@shop.co"
    html = f'<a href="/cdn-cgi/l/email-protection#{enc}"><span data-cfemail="{enc}">[email&#160;protected]</span></a>'
    assert extract_emails(html) == {"owner@shop.co"}


def test_obfuscated_and_entity_encoded():
    html = "contact: jane [at] studio [dot] io, or bob&#64;studio.io, or sam%40studio.io"
    assert extract_emails(html) == {"jane@studio.io", "bob@studio.io", "sam@studio.io"}


def test_filters_junk():
    html = (
        "logo@2x.png icon@3x.webp abc123@sentry.wixpress.com you@example.com "
        "youremail@gmail.com 0123456789abcdef0123@o1.ingest.sentry.io real@biz.com"
    )
    assert extract_emails(html) == {"real@biz.com"}


def test_ranking_prefers_site_domain_then_free_providers():
    emails = {"noreply@biz.com", "owner@gmail.com", "agency@marketingco.com", "hi@biz.com"}
    assert rank_emails(emails, "www.biz.com") == ["hi@biz.com", "owner@gmail.com", "agency@marketingco.com", "noreply@biz.com"]
