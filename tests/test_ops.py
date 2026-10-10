"""Regate, Claude cost logging, the run lock, and .env loading."""

import json
import os
import time
from datetime import date
from types import SimpleNamespace

import pytest

from conftest import FakeJev, FakeLLM
from paperbot.debate import run_debate
from paperbot.debate.llm import AnthropicLLM, LLMRefusal, cost_usd
from paperbot.digest import build_digest
from paperbot.lock import LockHeld, RunLock
from paperbot.models import STATUS_DEBATE_ERROR, STATUS_DIGESTED, STATUS_PASSED, STATUS_REJECTED, Paper
from paperbot.triage import run_regate, run_triage


def abstract_only(paper):
    return paper["abstract"], "abstract only"


# ---- regate -------------------------------------------------------------------------
def _triaged(cfg, store):
    # FakeJev: metrology -> 2.9 (no must-have facet), traceability -> 2.2 (+ traceability facet), else 0.3
    for i, (title, abstract) in enumerate([("Metrology x", "metrology"), ("Traceability y", "traceability"),
                                           ("Cooking", "pasta")]):
        store.upsert(Paper(title=title, abstract=abstract, source="t", arxiv_id=f"2610.2000{i}"))
    run_triage(cfg, store, FakeJev())
    return "arxiv:2610.20000", "arxiv:2610.20001", "arxiv:2610.20002"


def test_regate_after_threshold_change(cfg, store):
    metro, trace, cook = _triaged(cfg, store)
    # default 2.5: metrology passes on score, traceability rescued (2.2 within 0.5 + must-have facet)
    assert [store.get(p)["status"] for p in (metro, trace, cook)] == [STATUS_PASSED, STATUS_PASSED, STATUS_REJECTED]

    cfg.settings["triage"]["relevance_threshold"] = 3.0
    dry = run_regate(cfg, store, dry_run=True)
    assert (dry.newly_rejected, dry.newly_passed) == (2, 0)
    assert store.get(metro)["status"] == STATUS_PASSED  # dry run changes nothing

    stats = run_regate(cfg, store)
    assert stats.checked == 3 and stats.newly_rejected == 2
    assert store.get(metro)["status"] == STATUS_REJECTED and store.get(trace)["status"] == STATUS_REJECTED

    cfg.settings["triage"]["relevance_threshold"] = 2.0
    stats = run_regate(cfg, store)
    assert stats.newly_passed == 2 and store.get(cook)["status"] == STATUS_REJECTED


def test_regate_leaves_later_stages_alone_and_flags_model_mismatch(cfg, store):
    metro, trace, _ = _triaged(cfg, store)
    store.update(metro, status=STATUS_DIGESTED)
    store.update(trace, jev_model="jev-0.9.0")
    cfg.settings["triage"]["relevance_threshold"] = 3.9
    stats = run_regate(cfg, store)
    assert store.get(metro)["status"] == STATUS_DIGESTED
    assert stats.checked == 2 and stats.model_mismatch == 1


# ---- cost logging -----------------------------------------------------------------------
class MeteredLLM(FakeLLM):
    """FakeLLM that reports usage the way AnthropicLLM does."""

    def __init__(self, model_override=None, fail_on_role=None, **kw):
        super().__init__(**kw)
        self.last_usage = None
        self.model_override = model_override
        self.fail_on_role = fail_on_role

    def complete(self, *, model, system, prompt, max_tokens):
        self.last_usage = {"model": self.model_override or model, "input_tokens": 1000, "output_tokens": 200,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        if self.fail_on_role and system.startswith(self.fail_on_role):
            raise LLMRefusal("refused")
        return super().complete(model=model, system=system, prompt=prompt, max_tokens=max_tokens)


def _passed(cfg, store, titles):
    for i, t in enumerate(titles):
        store.upsert(Paper(title=t, abstract="wafer fab", source="t", arxiv_id=f"2610.3000{i}"))
    run_triage(cfg, store, FakeJev())


def test_cost_usd_math():
    pricing = {"m": {"input": 4.0, "output": 20.0, "cache_read": 0.2}}
    u = {"model": "m", "input_tokens": 1_000_000, "output_tokens": 100_000,
         "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert cost_usd(u, pricing) == pytest.approx(4.0 + 2.0 + 0.2 + 5.0)  # cache write = 1.25x input
    assert cost_usd({**u, "model": "unknown"}, pricing) is None


def test_debate_records_costs_and_digest_reports_them(cfg, store, tmp_path):
    _passed(cfg, store, ["Wafer agents"])
    stats = run_debate(cfg, store, MeteredLLM(), abstract_only)
    # 4 Sonnet turns + 1 Opus judge, each 1000 in / 200 out
    sonnet = 4 * (1000 * 2.0 + 200 * 10.0) / 1e6
    opus = (1000 * 4.0 + 200 * 20.0) / 1e6
    assert stats.cost_usd == pytest.approx(sonnet + opus)
    assert (stats.input_tokens, stats.output_tokens, stats.unpriced_calls) == (5000, 1000, 0)

    rows = store.conn.execute("SELECT role, model FROM llm_calls ORDER BY id").fetchall()
    assert [r["role"] for r in rows] == ["advocate", "skeptic", "advocate", "skeptic", "judge"]
    assert rows[-1]["model"] == "claude-opus-5-5"
    deb = json.loads(store.get("arxiv:2610.30000")["debate"])
    assert deb["usage"]["cost_usd"] == pytest.approx(sonnet + opus) and len(deb["usage"]["calls"]) == 5
    assert "cost_usd=" in store.conn.execute("SELECT note FROM runs WHERE stage='debate'").fetchone()[0]

    md = build_digest(store, tmp_path / "d", today=date(2026, 10, 8), since_iso="2000-01-01").read_text(encoding="utf-8")
    assert "- Claude: 1 paper(s) debated, 5 calls, 5,000 in / 1,000 out tokens, $0.02" in md


def test_failed_debate_still_logs_spend(cfg, store):
    _passed(cfg, store, ["Wafer agents"])
    stats = run_debate(cfg, store, MeteredLLM(fail_on_role="You are the Judge"), abstract_only)
    assert stats.errors == 1 and store.get("arxiv:2610.30000")["status"] == STATUS_DEBATE_ERROR
    # 4 debate turns + the refused judge call were all billed
    assert store.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 5
    assert stats.cost_usd > 0


def test_unpriced_model_is_flagged(cfg, store, tmp_path):
    _passed(cfg, store, ["Wafer agents"])
    stats = run_debate(cfg, store, MeteredLLM(model_override="claude-mystery-9"), abstract_only)
    assert stats.unpriced_calls == 5 and stats.cost_usd == 0
    assert json.loads(store.get("arxiv:2610.30000")["debate"])["usage"]["cost_usd"] is None
    md = build_digest(store, tmp_path / "d", today=date(2026, 10, 8), since_iso="2000-01-01").read_text(encoding="utf-8")
    assert "the cost above is an undercount" in md


# ---- Anthropic adapter ----------------------------------------------------------------
def _fake_client(stop_reason="end_turn", served_by="claude-sonnet-5-5"):
    seen = {}

    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(
            model=served_by, stop_reason=stop_reason,
            stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=None,
                                  cache_creation_input_tokens=3),
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="ok")])

    client = SimpleNamespace(messages=SimpleNamespace(create=create),
                             beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    return client, seen


def test_adapter_uses_fallbacks_and_reports_usage():
    client, seen = _fake_client(served_by="claude-opus-5")
    llm = AnthropicLLM(client=client)
    assert llm.complete(model="claude-sonnet-5-5", system="s", prompt="p", max_tokens=99) == "ok"
    assert seen["betas"] == ["server-side-fallback-2026-07-01"] and seen["fallbacks"] == "default"
    # usage is attributed to the model that actually served the request
    assert llm.last_usage == {"model": "claude-opus-5", "input_tokens": 10, "output_tokens": 5,
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 3}


def test_adapter_refusal_raises_and_fallbacks_can_be_disabled():
    client, _ = _fake_client(stop_reason="refusal")
    with pytest.raises(LLMRefusal, match="cyber"):
        AnthropicLLM(client=client).complete(model="m", system="s", prompt="p", max_tokens=9)
    client, seen = _fake_client()
    AnthropicLLM(client=client, fallbacks=False).complete(model="m", system="s", prompt="p", max_tokens=9)
    assert "fallbacks" not in seen and "betas" not in seen


# ---- run lock ----------------------------------------------------------------------------
def test_lock_excludes_second_run_and_releases(tmp_path):
    path = tmp_path / "paperbot.lock"
    with RunLock(path):
        with pytest.raises(LockHeld):
            RunLock(path).acquire()
    assert not path.exists()
    RunLock(path).acquire()  # free again


def test_stale_lock_is_taken_over(tmp_path):
    path = tmp_path / "paperbot.lock"
    path.write_text("pid=1", encoding="utf-8")
    old = time.time() - 7 * 3600
    os.utime(path, (old, old))
    lock = RunLock(path, stale_after_s=6 * 3600)
    lock.acquire()
    assert "pid=" in path.read_text(encoding="utf-8") and lock._held
    lock.release()


def test_cli_run_skips_when_locked(cfg, capsys):
    from paperbot.cli import main
    path = cfg.db_path.parent / "paperbot.lock"
    with RunLock(path):
        assert main(["--root", str(cfg.root), "run"]) == 2
    assert "skipping" in capsys.readouterr().err


# ---- .env --------------------------------------------------------------------------------
def test_dotenv_loads_without_overriding(tmp_path, monkeypatch):
    from paperbot.config import load_dotenv
    monkeypatch.delenv("PB_TEST_A", raising=False)
    monkeypatch.setenv("PB_TEST_B", "from-shell")
    monkeypatch.delenv("PB_TEST_C", raising=False)
    (tmp_path / ".env").write_text('# comment\nPB_TEST_A="quoted"\nPB_TEST_B=from-file\nPB_TEST_C=\n',
                                   encoding="utf-8")
    assert load_dotenv(tmp_path / ".env") == ["PB_TEST_A"]
    assert os.environ["PB_TEST_A"] == "quoted" and os.environ["PB_TEST_B"] == "from-shell"
    assert "PB_TEST_C" not in os.environ
    monkeypatch.delenv("PB_TEST_A")
