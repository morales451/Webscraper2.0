import json

import httpx
import pytest

from prospector.config import Settings
from prospector.sources import GoogleMapsBrowserSource, GooglePlacesApiSource, SourceError, make_source
from prospector.sources.maps_browser import _card_to_place, _clean_website, _merge_details, parse_maps_url

MAPS_URL = (
    "https://www.google.com/maps/place/Austin+Roofing+Company/data=!4m7!3m6!1s0x8644b5b356c4eb9f:0x1904833488534c"
    "!8m2!3d30.2700743!4d-97.7402698!16s%2Fg%2F11yrwfzhw0!19sChIJn-vEVrO1RIYRTFOINIMEGQA?authuser=0&hl=en"
)


def api_place(i: int) -> dict:
    return {
        "id": f"id{i}",
        "displayName": {"text": f"Biz {i}"},
        "formattedAddress": f"{i} Main St",
        "internationalPhoneNumber": "+1 555-0100",
        "websiteUri": f"https://biz{i}.example/",
        "rating": 4.5,
        "userRatingCount": 10 + i,
        "types": ["dentist", "health", "point_of_interest", "establishment"],
        "primaryTypeDisplayName": {"text": "Dentist"},
        "googleMapsUri": f"https://maps.google.com/?cid={i}",
        "location": {"latitude": 30.1, "longitude": -97.7},
        "businessStatus": "OPERATIONAL",
    }


async def test_places_api_paginates_and_maps_fields():
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["X-Goog-Api-Key"] == "KEY"
        assert "places.websiteUri" in request.headers["X-Goog-FieldMask"]
        page = 0 if "pageToken" not in body else int(body["pageToken"])
        places = [api_place(page * 20 + i) for i in range(20)]
        data = {"places": places}
        if page < 2:
            data["nextPageToken"] = str(page + 1)
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        src = GooglePlacesApiSource("KEY", client=client)
        results = [p async for p in src.search("dentist in Austin", 45)]
    assert len(results) == 45 and len(calls) == 3
    first = results[0]
    assert first.name == "Biz 0" and first.place_id == "id0" and first.category == "Dentist"
    assert first.categories == ["Dentist", "health"]
    assert first.reviews == 10 and first.website == "https://biz0.example/" and first.source == "api"


async def test_places_api_error_is_readable():
    def handler(request):
        return httpx.Response(403, json={"error": {"message": "Places API (New) has not been used in project 1"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        src = GooglePlacesApiSource("KEY", client=client)
        with pytest.raises(SourceError, match="403.*has not been used"):
            [p async for p in src.search("x", 5)]


def test_parse_maps_url():
    ids = parse_maps_url(MAPS_URL)
    assert ids == {"place_id": "ChIJn-vEVrO1RIYRTFOINIMEGQA", "latitude": 30.2700743, "longitude": -97.7402698}
    assert parse_maps_url("https://www.google.com/maps/place/X/data=!1s0xabc:0xdef")["place_id"] == "0xabc:0xdef"


def test_card_and_details_parsing():
    card = {
        "href": MAPS_URL,
        "name": "Austin Roofing Company",
        "rating": "4.9 stars 1,234 Reviews",
        "reviews": None,
        "website": "https://www.google.com/url?q=https://austinroofingcompany.com/&sa=U",
        "phone": "+1 512-640-2658",
        "lines": [["Roofing contractor", "807 Brazos St #403"], ["Open 24 hours", "+1 512-640-2658"]],
    }
    p = _card_to_place(card, "roofer in Austin")
    assert (p.category, p.address, p.rating, p.reviews) == ("Roofing contractor", "807 Brazos St #403", 4.9, 1234)
    assert p.website == "https://austinroofingcompany.com/"
    p = _merge_details(p, {
        "name": "Austin Roofing Company", "rating": "5.0", "reviews": "(87)", "category": "Roofing contractor",
        "address": "807 Brazos St #403, Austin, TX 78701", "website": "", "phone": "", "status": "",
    })
    assert p.address.endswith("78701") and p.rating == 5.0 and p.reviews == 87
    assert p.website == "https://austinroofingcompany.com/"  # kept from the card when details lack it


def test_clean_website_passthrough():
    assert _clean_website("https://a.com/") == "https://a.com/"
    assert _clean_website(None) == ""


def test_make_source_picks_by_settings(tmp_path):
    s = Settings(data_dir=tmp_path, profiles_dir=tmp_path)
    assert isinstance(make_source(s), GoogleMapsBrowserSource)
    s.places_api_key = "KEY"
    assert isinstance(make_source(s), GooglePlacesApiSource)
    assert isinstance(make_source(s, "browser"), GoogleMapsBrowserSource)
    s.places_api_key = ""
    with pytest.raises(SourceError):
        make_source(s, "api")
