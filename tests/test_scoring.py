from prospector.enrich.website import PageContent, SiteData
from prospector.icp import Filters, ICPProfile
from prospector.scoring import compile_term, evaluate, prefilter
from prospector.sources.base import Place


def site(*texts: str, emails=(), headings=("",), chrome=("",), signals=()) -> SiteData:
    pages = []
    for i, t in enumerate(texts):
        pages.append(PageContent(f"https://x.example/{i}", "", t,
                                 headings=headings[i] if i < len(headings) else "",
                                 chrome=chrome[i] if i < len(chrome) else ""))
    return SiteData(url="https://x.example/", domain="x.example", ok=True, pages=pages,
                    emails=list(emails), signals=list(signals))


def test_term_matching_is_whole_word_with_wildcards():
    assert compile_term("implant*").search("We place Implants daily")
    assert compile_term("implant*").search("implantology")
    assert not compile_term("tpo").search("hotpot")
    assert compile_term("flat roof").search("flat\n  roof systems")
    assert compile_term("all-on-4").search("All-On-4 dentures")
    assert compile_term("c++").search("we write c++ code")


def test_synonym_groups():
    rx = compile_term("TPO / single-ply / membrane roof*")
    assert rx.search("single-ply systems") and rx.search("Membrane roofing") and rx.search("tpo")
    assert not rx.search("shingles")
    assert compile_term("24/7").search("open 24/7")  # a slash without spaces is literal


def test_placement_and_repetition_change_credit():
    profile = ICPProfile(name="t", queries=["q"], keywords=["implant*"])
    menu_only = evaluate(Place(name="A"), site("welcome", chrome=["Implants · Crowns"]), profile)
    in_text = evaluate(Place(name="A"), site("we place implants"), profile)
    headline = evaluate(Place(name="A"), site("x", headings=["Dental Implants"]), profile)
    widely = evaluate(Place(name="A"), site("implants", "implants", "implants"), profile)
    assert (menu_only.score, in_text.score, headline.score, widely.score) == (40, 70, 90, 90)
    assert menu_only.matches[0].zone == "menu/footer only" and widely.matches[0].pages == 3
    many = evaluate(Place(name="A"), site("implants " * 5, headings=["Implants"]), profile)
    assert many.score == 100


def test_score_is_weighted_credit():
    profile = ICPProfile(name="t", queries=["q"], keywords=["implant* | 3", "sedation | 2", "veneers"])
    ev = evaluate(Place(name="A"), site("Implants and veneers", "more implants"), profile)
    # implant*: body on 2 pages = 0.8 × 3; veneers: body = 0.7 × 1; sedation missing.
    assert ev.score == round(100 * (0.8 * 3 + 0.7) / 6)
    assert ev.qualified
    implant = next(m for m in ev.matches if m.term == "implant*")
    assert implant.count == 2 and len(implant.snippets) == 2 and implant.credit == 0.8
    assert ev.matches[0].term == "implant*"  # biggest contribution first


def test_maps_name_category_and_signals_are_strong_evidence():
    profile = ICPProfile(name="t", queries=["q"], keywords=["orthodont*", "wordpress"])
    ev = evaluate(Place(name="Bright Smiles", category="Orthodontist", categories=["Orthodontist"]),
                  site("hello", signals=["WordPress", "Contact form"]), profile)
    assert ev.score == 90
    assert ev.matches[0].snippets[0]["where"] in ("Google Maps category", "Website signals")


def test_required_exclude_and_min_score():
    profile = ICPProfile(
        name="t", queries=["q"], keywords=["implant*", "sedation"], required_keywords=["implant*"],
        exclude_keywords=["franchise"], min_score=60,
    )
    ev = evaluate(Place(name="A"), site("sedation dentistry, a franchise location"), profile)
    assert not ev.qualified
    assert "none of the required keywords found" in ev.reasons
    assert "excluded keyword: franchise" in ev.reasons
    assert "score 35 < 60" in ev.reasons
    assert any(m.term == "NOT: franchise" for m in ev.matches)


def test_no_keywords_means_no_score_but_still_qualifies():
    ev = evaluate(Place(name="A"), None, ICPProfile(name="t", queries=["q"]))
    assert ev.score is None and ev.qualified


def test_require_email_ignores_undeliverable_addresses():
    profile = ICPProfile(name="t", queries=["q"], filters={"require_email": True})
    assert not evaluate(Place(name="A"), site("x"), profile).qualified
    assert evaluate(Place(name="A"), site("x", emails=["a@x.example"]), profile).qualified
    dead = site("x", emails=["a@x.dead"])
    dead.email_checks = {"a@x.dead": "invalid"}
    ev = evaluate(Place(name="A"), dead, profile)
    assert not ev.qualified and "no deliverable email" in ev.reasons


def test_prefilter_rules():
    f = Filters(min_rating=4.0, min_reviews=20, max_reviews=500, require_website=True, require_phone=True,
                include_categories=["dent"], exclude_categories=["laboratory"], exclude_names=["Aspen"])
    good = Place(name="Good Dental", category="Dentist", categories=["Dentist"], rating=4.5, reviews=100,
                 website="https://g.example", phone="1")
    assert prefilter(good, f) == []
    bad = Place(name="Aspen Dental", category="Dental laboratory", categories=["Dental laboratory"],
                rating=3.5, reviews=1000, business_status="CLOSED_PERMANENTLY")
    reasons = prefilter(bad, f)
    assert "business is closed" in reasons
    assert "rating 3.5 < 4.0" in reasons
    assert "1000 reviews > 500" in reasons
    assert "no website" in reasons and "no phone" in reasons
    assert "excluded category 'laboratory'" in reasons
    assert "excluded name 'Aspen'" in reasons
    # Unknown review count (Google's limited view) doesn't disqualify.
    assert prefilter(Place(name="X", category="Dentist", rating=4.5, website="w", phone="1"), f) == []
