import pytest

from prospector import pipeline
from prospector.icp import ICPProfile
from prospector.pipeline import RunOptions, execute_run
from prospector.sources import SourceError
from prospector.storage import Store

from .conftest import FakeSource, sample_places, site_handler


@pytest.fixture
def patched(monkeypatch):
    """Route the pipeline's HTTP client to the mock websites and Google to the fake source."""
    import httpx

    def fake_client(timeout=15.0):
        return httpx.AsyncClient(transport=httpx.MockTransport(site_handler), follow_redirects=True)

    monkeypatch.setattr(pipeline, "make_http_client", fake_client)
    sources = []

    def install(places_by_query):
        def factory(settings, requested=None, log=None, cancel=None, details=True):
            src = FakeSource(places_by_query)
            sources.append(src)
            return src

        monkeypatch.setattr(pipeline, "make_source", factory)
        return sources

    return install


def profile(**kw) -> ICPProfile:
    base = dict(
        name="Implant dentists",
        queries=["dentist"],
        locations=["Austin, TX"],
        keywords=["implant* | 3", "sedation | 2", "invisalign"],
        exclude_keywords=["now hiring"],
        filters={"min_rating": 4.0},
    )
    base.update(kw)
    return ICPProfile(**base)


async def test_full_run(settings, patched):
    sources = patched({"dentist in Austin, TX": sample_places()})
    store = Store(settings.db_path)
    prof = profile()
    run_id = store.create_run(prof.model_dump(), None, "fake", 1)
    logs = []
    run = await execute_run(settings, store, run_id, prof, RunOptions(), on_log=logs.append)

    assert run["status"] == "done", run
    assert (run["found"], run["processed"], run["searches_done"]) == (5, 5, 1)
    assert sources[0].closed

    leads = {l["name"]: l for l in store.query_leads(run_id=run_id)[0]}
    smile = leads["Smile Dental"]
    assert smile["qualified"] and smile["score"] >= 90
    assert smile["website"] == "https://smile.example/" and smile["domain"] == "smile.example"
    assert "hello@smile.example" in smile["emails"]
    assert {m["term"] for m in smile["matches"]} == {"implant*", "sedation", "invisalign"}

    plain = leads["Plain Dental"]
    assert not plain["qualified"] and "excluded keyword: now hiring" in plain["reasons"]

    low = leads["Low Rated Dental"]
    assert not low["qualified"] and low["pages_crawled"] == 0  # filtered before crawling
    assert "rating 3.1 < 4.0" in low["reasons"]

    blocked = leads["Blocked Dental"]
    assert "bot protection" in blocked["site_error"]

    assert leads["No Site Dental"]["score"] == 0
    assert run["qualified"] == sum(1 for l in leads.values() if l["qualified"])
    assert any("Finished" in m for m in logs)


async def test_second_run_skips_seen_businesses(settings, patched):
    patched({"dentist in Austin, TX": sample_places()})
    store = Store(settings.db_path)
    prof = profile()
    first = store.create_run(prof.model_dump(), None, "fake", 1)
    await execute_run(settings, store, first, prof)
    second = store.create_run(prof.model_dump(), None, "fake", 1)
    run = await execute_run(settings, store, second, prof)
    assert run["skipped_seen"] == 5 and run["processed"] == 0

    third = store.create_run(prof.model_dump(), None, "fake", 1)
    run = await execute_run(settings, store, third, profile(skip_seen=False))
    assert run["processed"] == 5


async def test_dedupes_across_searches_and_respects_max_leads(settings, patched):
    places = sample_places()
    patched({"dentist in Austin, TX": places, "orthodontist in Austin, TX": places})
    store = Store(settings.db_path)
    prof = profile(queries=["dentist", "orthodontist"], max_leads=3)
    run_id = store.create_run(prof.model_dump(), None, "fake", 2)
    run = await execute_run(settings, store, run_id, prof)
    assert run["processed"] == 3

    prof = profile(queries=["dentist", "orthodontist"])
    run_id = store.create_run(prof.model_dump(), None, "fake", 2)
    run = await execute_run(settings, store, run_id, profile(queries=["dentist", "orthodontist"], skip_seen=False))
    assert run["found"] == 5 and run["searches_done"] == 2


async def test_social_media_websites_are_not_treated_as_the_same_business(settings, patched):
    from prospector.sources.base import Place

    fb = [Place(name=f"Shop {i}", place_id=f"fb{i}", website=f"https://facebook.com/shop{i}") for i in range(3)]
    patched({"dentist in Austin, TX": fb})
    store = Store(settings.db_path)
    prof = profile(filters={})
    run_id = store.create_run(prof.model_dump(), None, "fake", 1)
    await execute_run(settings, store, run_id, prof)
    patched({"dentist in Austin, TX": fb + [Place(name="Shop 9", place_id="fb9", website="https://facebook.com/shop9")]})
    run_id = store.create_run(prof.model_dump(), None, "fake", 1)
    run = await execute_run(settings, store, run_id, prof)
    assert run["skipped_seen"] == 3 and run["processed"] == 1  # Shop 9 is new despite sharing facebook.com
    lead = store.query_leads(run_id=run_id)[0][0]
    assert lead["domain"] == "" and "social media" in lead["site_error"]


async def test_stopping_early_closes_the_source_generator(settings, monkeypatch):
    closed = []

    class Endless:
        name = "endless"

        async def search(self, query, limit, area=None, want=None):
            from prospector.sources.base import Place
            try:
                for i in range(limit):
                    yield Place(name=f"Biz {i}", place_id=f"e{i}")
            finally:
                closed.append(query)

        async def close(self):
            pass

    monkeypatch.setattr(pipeline, "make_source", lambda *a, **k: Endless())
    store = Store(settings.db_path)
    prof = profile(max_leads=2, filters={})
    run_id = store.create_run(prof.model_dump(), None, "fake", 1)
    run = await execute_run(settings, store, run_id, prof, RunOptions(enrich=False))
    assert run["processed"] == 2 and closed == ["dentist in Austin, TX"]


async def test_source_errors_fail_the_run(settings, monkeypatch):
    class Broken:
        name = "broken"

        async def search(self, query, limit, area=None, want=None):
            raise SourceError("Google Places API error 403: API key not valid")
            yield  # pragma: no cover

        async def close(self):
            pass

    monkeypatch.setattr(pipeline, "make_source", lambda *a, **k: Broken())
    store = Store(settings.db_path)
    prof = profile()
    run_id = store.create_run(prof.model_dump(), None, "api", 1)
    run = await execute_run(settings, store, run_id, prof)
    assert run["status"] == "failed" and "API key not valid" in run["error"]
