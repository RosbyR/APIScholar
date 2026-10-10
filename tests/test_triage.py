import json

import httpx
import pytest
import respx

from conftest import FakeJev
from paperbot.models import STATUS_PASSED, STATUS_REJECTED, STATUS_TRIAGE_ERROR, Paper
from paperbot.triage import run_triage
from paperbot.triage.gate import decide
from paperbot.triage.jev import ENDPOINT, JevClient, JevError, JevFatalError
from paperbot.triage.questions import build_questions, build_state, must_have_keys


def test_build_questions_from_profile(cfg):
    q = build_questions(cfg.profile)
    assert q["relevance"]["type"] == "score"
    assert 2 <= len(q["relevance"]["criteria"]) <= 10
    nouls = [k for k, v in q.items() if v["type"] == "noul"]
    assert "facet_traceability" in nouls and "facet_semiconductor" in nouls
    assert set(must_have_keys(cfg.profile)) == {"facet_semiconductor", "facet_traceability"}
    assert build_state("T", "", None) == {"title": "T", "abstract": "(no abstract available)"}


def test_gate():
    ans = lambda s, sem=0.0: {"relevance": {"score": s}, "facet_semiconductor": {"type": "noul", "noul": sem}}
    must = ["facet_semiconductor"]
    assert decide(ans(2.6), 2.5, must).passed
    assert not decide(ans(2.2), 2.5, must).passed
    d = decide(ans(2.2, sem=0.95), 2.5, must)
    assert d.passed and "rescued" in d.reason
    assert not decide(ans(1.9, sem=0.99), 2.5, must).passed  # outside rescue margin


@respx.mock
def test_jev_client_success_and_request_shape():
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={
        "model": "jev-1.13.0", "answers": {"x": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 5}}))
    c = JevClient("key", model="jev-1.13.0")
    out = c.evaluate({"title": "t"}, {"x": {"type": "noul", "instructions": "?"}})
    assert out["answers"]["x"]["noul"] == 0.9
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer key"
    assert json.loads(req.content) == {"state": {"title": "t"}, "model": "jev-1.13.0",
                                       "questions": {"x": {"type": "noul", "instructions": "?"}}}


@respx.mock
def test_jev_client_retries_then_fatal():
    respx.post(ENDPOINT).mock(side_effect=[httpx.Response(529), httpx.Response(429),
                                           httpx.Response(200, json={"answers": {}})])
    slept = []
    assert JevClient("k", sleep=slept.append).evaluate("s", {}) == {"answers": {}}
    assert len(slept) == 2

    respx.post(ENDPOINT).mock(return_value=httpx.Response(422, json={"detail": "bad"}))
    with pytest.raises(JevFatalError):
        JevClient("k", sleep=lambda s: None).evaluate("s", {})

    respx.post(ENDPOINT).mock(return_value=httpx.Response(529))
    with pytest.raises(JevError):
        JevClient("k", max_retries=1, sleep=lambda s: None).evaluate("s", {})


def _seed(store):
    store.upsert(Paper(title="Wafer excursion agents", abstract="fab data", source="t", arxiv_id="2610.00001"))
    store.upsert(Paper(title="Card game agents", abstract="games", source="t", arxiv_id="2610.00002"))
    store.upsert(Paper(title="Supply chain traceability", abstract="semiconductor traceability ledger",
                       source="t", arxiv_id="2610.00003"))
    store.upsert(Paper(title="Flaky", abstract="", source="t", arxiv_id="2610.00004"))


def test_run_triage(cfg, store):
    _seed(store)
    jev = FakeJev(fail_titles={"Flaky"})
    stats = run_triage(cfg, store, jev)
    assert (stats.scored, stats.passed, stats.rejected, stats.errors) == (3, 2, 1, 1)
    assert store.get("arxiv:2610.00001")["status"] == STATUS_PASSED
    assert store.get("arxiv:2610.00002")["status"] == STATUS_REJECTED
    # 2.2 is under 2.5 but within rescue margin and semiconductor/traceability Nouls fire
    assert store.get("arxiv:2610.00003")["status"] == STATUS_PASSED
    assert store.get("arxiv:2610.00004")["status"] == STATUS_TRIAGE_ERROR
    answers = json.loads(store.get("arxiv:2610.00001")["jev_answers"])
    assert answers["relevance"]["score"] == 3.6
    # retry on next run picks up only the failed one
    stats2 = run_triage(cfg, store, FakeJev())
    assert stats2.scored == 1


def test_run_triage_fatal_raises_after_recording_others(cfg, store):
    _seed(store)

    class Fatal(FakeJev):
        def evaluate(self, state, questions):
            if state["title"] == "Card game agents":
                raise JevFatalError("HTTP 401")
            return super().evaluate(state, questions)

    with pytest.raises(JevFatalError):
        run_triage(cfg, store, Fatal())
    assert store.get("arxiv:2610.00001")["status"] == STATUS_PASSED
