import json
import shutil
from pathlib import Path

import pytest

from paperbot.config import load_config
from paperbot.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIX


@pytest.fixture
def cfg(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return load_config(tmp_path)


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


class FakeJev:
    """Scores by keyword so tests are deterministic. Mirrors the documented answer shapes."""

    model = "jev-1.13.0"

    def __init__(self, fail_titles=()):
        self.calls = 0
        self.fail_titles = set(fail_titles)

    def evaluate(self, state, questions):
        from paperbot.triage.jev import JevError
        self.calls += 1
        text = (state["title"] + " " + state["abstract"]).lower()
        if state["title"] in self.fail_titles:
            raise JevError("HTTP 529: overloaded")
        score = 0.3
        if "wafer" in text or "fab" in text:
            score = 3.6
        elif "traceability" in text:
            score = 2.2
        elif "metrology" in text:
            score = 2.9
        answers = {
            "relevance": {"type": "score", "score": score, "legend": {}, "probabilities": {}, "confidence": 0.7},
        }
        for k in questions:
            if k.startswith("facet_"):
                hit = k.removeprefix("facet_").split("_")[0] in text
                answers[k] = {"type": "noul", "noul": 0.95 if hit else 0.05}
        return {"model": self.model, "answers": answers, "usage": {"input_tokens": 250, "output_tokens": 10}}


class FakeLLM:
    """Returns canned debate turns; judge vetoes anything with 'veto-me' in the title."""

    def __init__(self, bad_json_first=False):
        self.calls = []
        self.bad_json_first = bad_json_first

    def complete(self, *, model, system, prompt, max_tokens):
        self.calls.append((model, system[:20]))
        if system.startswith("You are the Judge"):
            if self.bad_json_first:
                self.bad_json_first = False
                return "Sure! Here is my verdict: keep it."
            veto = "veto-me" in prompt
            return "Here you go:\n" + json.dumps({
                "verdict": "veto" if veto else "keep",
                "relevance": 2 if veto else 5,
                "read_priority": "file away" if veto else "read now",
                "summary": "The paper builds a genealogy graph and an agent that queries it.",
                "why_you_care": "" if veto else "It is traceability-driven root cause on real fab data.",
                "key_evidence": [{"claim": "41% faster root cause", "where": "Table 3"}],
                "caveats": ["single fab"],
                "debate_takeaway": "The skeptic's single-site concern stands but does not sink it.",
            })
        return f"[{model}] argument"


@pytest.fixture
def fake_jev():
    return FakeJev()


@pytest.fixture
def fake_llm():
    return FakeLLM()
