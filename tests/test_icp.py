import pytest
from pydantic import ValidationError

from prospector.icp import ICPProfile, ProfileStore, slugify


def test_keyword_formats_are_normalised():
    p = ICPProfile(
        name="x",
        queries=["dentist"],
        keywords=["implant* | 3", "sedation = 2", "invisalign", {"veneers": 2}, {"term": "crowns", "weight": 1.5}, ""],
    )
    assert [(k.term, k.weight) for k in p.keywords] == [
        ("implant*", 3), ("sedation", 2), ("invisalign", 1), ("veneers", 2), ("crowns", 1.5)
    ]


def test_lists_accept_multiline_strings_and_dedupe():
    p = ICPProfile(name="x", queries="dentist\n\nDentist\northodontist", locations=" Austin, TX \n")
    assert p.queries == ["dentist", "orthodontist"]
    assert p.locations == ["Austin, TX"]


def test_search_terms_cross_product():
    p = ICPProfile(name="x", queries=["a", "b"], locations=["X", "Y"])
    assert p.search_terms() == ["a in X", "b in X", "a in Y", "b in Y"]
    assert ICPProfile(name="x", queries=["a"]).search_terms() == ["a"]


def test_requires_a_query_and_name():
    with pytest.raises(ValidationError):
        ICPProfile(name="x", queries=[])
    with pytest.raises(ValidationError):
        ICPProfile(name="  ", queries=["a"])


def test_store_roundtrip(tmp_path):
    store = ProfileStore(tmp_path)
    p = ICPProfile(name="Roofers TX!", queries=["roofer"], keywords=["tpo | 3", "flat roof"],
                   filters={"min_rating": 4.2, "exclude_names": ["Chain Co"]})
    slug = store.save(p)
    assert slug == "roofers-tx"
    text = (tmp_path / "roofers-tx.yaml").read_text()
    assert "tpo: 3" in text and "- flat roof" in text
    assert "require_phone" not in text  # defaults are omitted to keep files short
    loaded = store.get(slug)
    assert loaded == p
    assert [x["id"] for x in store.list()] == ["roofers-tx"]
    store.delete(slug)
    assert store.list() == []


def test_store_rejects_path_tricks(tmp_path):
    store = ProfileStore(tmp_path)
    with pytest.raises(ValueError):
        store.get("../etc/passwd")


def test_slugify():
    assert slugify("  Dentists — Implants (TX) ") == "dentists-implants-tx"
    assert slugify("!!!") == "profile"


def test_broken_yaml_is_reported_not_hidden(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: only-a-name\n")
    items = ProfileStore(tmp_path).list()
    assert items[0]["id"] == "bad" and "error" in items[0]
