import json
from datetime import datetime, timezone

import httpx
import respx

from paperbot.harvest import run_harvest
from paperbot.harvest.arxiv import ArxivAPI, ArxivRSS, parse_api, parse_rss
from paperbot.harvest.openalex import OpenAlex, parse_work, rebuild_abstract
from paperbot.http import RateLimiter
from paperbot.models import Paper, normalize_arxiv_id, normalize_doi


def test_id_normalization():
    assert normalize_arxiv_id("oai:arXiv.org:2610.04211v1") == "2610.04211"
    assert normalize_arxiv_id("http://arxiv.org/abs/2610.05555v2") == "2610.05555"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2610.05555v2.pdf") == "2610.05555"
    assert normalize_arxiv_id("10.48550/arxiv.2610.04211") == "2610.04211"
    assert normalize_arxiv_id("hep-th/9901001v2") == "hep-th/9901001"
    assert normalize_doi("https://doi.org/10.1016/J.JMSY.2026.09.004") == "10.1016/j.jmsy.2026.09.004"
    p = Paper(title="x", abstract="", source="t", doi="https://doi.org/10.48550/arXiv.2610.04211")
    assert p.arxiv_id == "2610.04211" and p.canonical_id == "arxiv:2610.04211"


def test_parse_rss_filters_announce_types(fixtures):
    papers = parse_rss((fixtures / "arxiv_rss.xml").read_bytes(), {"new", "cross"})
    assert [p.arxiv_id for p in papers] == ["2610.04211", "2610.03999"]
    p = papers[0]
    assert p.abstract.startswith("We present a multi-agent LLM system")
    assert "Announce Type" not in p.abstract
    assert p.authors == ["Ana Ruiz", "Bo Chen", "Carla Diaz", "Dev Patel"]
    assert set(p.categories) == {"cs.MA", "cs.AI"}
    assert p.published == "2026-10-08"
    assert p.pdf_url == "https://arxiv.org/pdf/2610.04211"


def test_parse_api(fixtures):
    papers = parse_api((fixtures / "arxiv_api.xml").read_bytes())
    p = papers[0]
    assert p.arxiv_id == "2610.05555"
    assert p.title == "Virtual Metrology with Physics-Informed Gaussian Processes for Plasma Etch"
    assert p.doi == "10.1109/tsm.2026.1234567"
    assert p.venue.startswith("IEEE Trans. Semicond. Manuf.")
    assert p.authors == ["Gina Park", "Hugo Lin"]


def test_openalex_abstract_rebuild(fixtures):
    data = json.loads((fixtures / "openalex.json").read_text())
    assert rebuild_abstract(data["results"][1]["abstract_inverted_index"]) == \
        "Traceability across assembly steps enables faster assembly investigations."
    assert rebuild_abstract(None) == ""
    p = parse_work(data["results"][1])
    assert p.venue == "Journal of Manufacturing Systems"
    assert p.openalex_id == "W4400000002"
    assert p.doi == "10.1016/j.jmsy.2026.09.004"
    assert parse_work(data["results"][0]).arxiv_id == "2610.04211"


@respx.mock
def test_sources_over_http_and_dedup(cfg, store, fixtures):
    respx.get("https://rss.arxiv.org/rss/cs.MA").mock(
        return_value=httpx.Response(200, content=(fixtures / "arxiv_rss.xml").read_bytes()))
    oa = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=json.loads((fixtures / "openalex.json").read_text())))
    sleeps = []
    with httpx.Client() as client:
        sources = [
            ArxivRSS(client, RateLimiter(3.0, sleep=sleeps.append), ["cs.MA"]),
            OpenAlex(client, RateLimiter(0.0), ["semiconductor traceability"], api_key="k"),
        ]
        stats = run_harvest(cfg, store, sources)
    assert stats.per_source["arxiv_rss"] == {"fetched": 2, "new": 2, "errors": 0}
    # OpenAlex: erratum skipped; the arXiv paper is a duplicate (matched via arXiv DOI)
    assert stats.per_source["openalex"] == {"fetched": 2, "new": 1, "errors": 0}
    row = store.get("arxiv:2610.04211")
    assert set(json.loads(row["sources"])) == {"arxiv_rss", "openalex"}
    assert row["openalex_id"] == "W4400000001"
    # longer abstract (from RSS) was kept over OpenAlex's shorter one
    assert "41%" in row["abstract"]
    q = oa.calls.last.request.url.params
    assert q["api_key"] == "k" and "title_and_abstract.search:semiconductor traceability" in q["filter"]
    assert "from_publication_date:" in q["filter"]
    assert store.get_watermark("arxiv_rss") and store.get_watermark("openalex")


def test_title_hash_dedup_when_no_shared_ids(store):
    a = Paper(title="Digital Thread Traceability: A Survey", abstract="a", source="x", doi="10.1/a")
    b = Paper(title="Digital thread traceability -- a survey", abstract="longer abstract", source="y")
    pid_a, new_a = store.upsert(a)
    pid_b, new_b = store.upsert(b)
    assert new_a and not new_b and pid_a == pid_b
    assert store.get(pid_a)["abstract"] == "longer abstract"


def test_failing_source_isolated_and_watermark_not_advanced(cfg, store):
    class Boom:
        name = "boom"

        def fetch(self, since=None):
            yield Paper(title="Partial", abstract="", source="boom")
            raise RuntimeError("API down")

    class Ok:
        name = "ok"

        def fetch(self, since=None):
            yield Paper(title="Fine", abstract="", source="ok")

    stats = run_harvest(cfg, store, [Boom(), Ok()])
    assert stats.per_source["boom"]["errors"] == 1
    assert stats.per_source["ok"] == {"fetched": 1, "new": 1, "errors": 0}
    assert store.get_watermark("boom") is None
    assert store.get_watermark("ok") is not None


def test_watermark_overlap(cfg, store):
    seen = []

    class Rec:
        name = "rec"

        def fetch(self, since=None):
            seen.append(since)
            return iter(())

    store.set_watermark("rec", "2026-10-08T06:00:00+00:00")
    run_harvest(cfg, store, [Rec()], now=datetime(2026, 10, 9, 6, tzinfo=timezone.utc))
    assert seen[0] == datetime(2026, 10, 6, 6, tzinfo=timezone.utc)  # watermark minus 2-day overlap


@respx.mock
def test_arxiv_api_backfill_stops_at_window(fixtures):
    route = respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, content=(fixtures / "arxiv_api.xml").read_bytes()))
    with httpx.Client() as client:
        src = ArxivAPI(client, RateLimiter(0.0), ["eess.SY"])
        papers = list(src.fetch(datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert [p.arxiv_id for p in papers] == ["2610.05555"]
    assert route.call_count == 1  # stopped once results fell outside the window


def test_rate_limiter_spacing():
    t = [0.0]
    slept = []
    rl = RateLimiter(3.0, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    rl.wait(); t[0] += 1.0; rl.wait(); rl.wait()
    assert slept == [2.0, 3.0]


@respx.mock
def test_get_with_retry_backs_off_on_503():
    from paperbot.http import get_with_retry
    route = respx.get("https://example.org/x").mock(side_effect=[
        httpx.Response(503), httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, text="ok")])
    slept = []
    with httpx.Client() as c:
        r = get_with_retry(c, "https://example.org/x", sleep=slept.append)
    assert r.text == "ok" and route.call_count == 3 and len(slept) == 2 and slept[1] == 1.0
