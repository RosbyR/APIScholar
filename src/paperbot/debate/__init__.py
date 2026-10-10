"""Debate stage: advocate vs skeptic (opening + rebuttal), then a judge with veto power."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Config
from ..models import STATUS_ACCEPTED, STATUS_DEBATE_ERROR, STATUS_PASSED, STATUS_VETOED
from ..store import Store, now_iso
from ..triage.questions import FACET_PREFIX
from . import prompts
from .llm import LLM

log = logging.getLogger(__name__)

REQUIRED_KEYS = {"verdict", "relevance", "summary", "why_you_care"}


class JudgeParseError(ValueError):
    pass


def parse_judge(raw: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise JudgeParseError("no JSON object in judge output")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise JudgeParseError(str(e)) from e
    missing = REQUIRED_KEYS - data.keys()
    if missing:
        raise JudgeParseError(f"missing keys: {sorted(missing)}")
    if data["verdict"] not in ("keep", "veto"):
        raise JudgeParseError(f"bad verdict {data['verdict']!r}")
    data.setdefault("key_evidence", [])
    data.setdefault("caveats", [])
    data.setdefault("read_priority", "skim")
    data.setdefault("debate_takeaway", "")
    return data


@dataclass
class DebateResult:
    judge: dict[str, Any]
    transcript: dict[str, str]
    text_kind: str


def debate_paper(paper: dict[str, Any], text: str, text_kind: str, profile: dict[str, Any],
                 llm: LLM, models: dict[str, str], max_tokens: int = 1500) -> DebateResult:
    answers = paper.get("jev_answers") or {}
    if isinstance(answers, str):
        answers = json.loads(answers)
    facets = {k.removeprefix(FACET_PREFIX): v["noul"] for k, v in answers.items()
              if k.startswith(FACET_PREFIX) and "noul" in v}
    context = prompts.reader_block(profile) + "\n\n" + prompts.paper_block(paper, text, text_kind, facets)

    def ask(role: str, system: str, extra: str = "") -> str:
        return llm.complete(model=models[role], system=system,
                            prompt=context + ("\n\n" + extra if extra else ""), max_tokens=max_tokens)

    t: dict[str, str] = {}
    t["advocate_opening"] = ask("advocate", prompts.ADVOCATE_SYSTEM)
    t["skeptic_opening"] = ask("skeptic", prompts.SKEPTIC_SYSTEM)
    t["advocate_rebuttal"] = ask("advocate", prompts.ADVOCATE_SYSTEM,
                                 prompts.REBUTTAL_INSTRUCTIONS.format(other=t["skeptic_opening"]))
    t["skeptic_rebuttal"] = ask("skeptic", prompts.SKEPTIC_SYSTEM,
                                prompts.REBUTTAL_INSTRUCTIONS.format(other=t["advocate_opening"]))
    debate_text = "\n\n".join(f"<{k}>\n{v}\n</{k}>" for k, v in t.items())
    judge_prompt = f"<debate>\n{debate_text}\n</debate>"
    raw = ask("judge", prompts.JUDGE_SYSTEM, judge_prompt)
    try:
        judge = parse_judge(raw)
    except JudgeParseError:
        raw = ask("judge", prompts.JUDGE_SYSTEM,
                  judge_prompt + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object.")
        judge = parse_judge(raw)
    t["judge_raw"] = raw
    return DebateResult(judge=judge, transcript=t, text_kind=text_kind)


@dataclass
class DebateStats:
    debated: int = 0
    accepted: int = 0
    vetoed: int = 0
    errors: int = 0
    deferred: int = 0


def run_debate(cfg: Config, store: Store, llm: LLM,
               get_text: Callable[[dict], tuple[str, str]]) -> DebateStats:
    d = cfg.section("debate")
    stats = DebateStats()
    if not d.get("enabled", True):
        return stats
    cap = int(d.get("max_papers_per_run", 8))
    models = {"advocate": d.get("advocate_model"), "skeptic": d.get("skeptic_model"), "judge": d.get("judge_model")}
    queue = store.by_status(STATUS_PASSED, STATUS_DEBATE_ERROR, order="jev_score DESC")
    stats.deferred = max(0, len(queue) - cap)
    for row in queue[:cap]:
        paper = dict(row)
        try:
            text, kind = get_text(paper)
            res = debate_paper(paper, text, kind, cfg.profile, llm, models, d.get("max_tokens", 1500))
        except Exception as e:
            log.exception("debate failed for %s", paper["id"])
            stats.errors += 1
            store.update(paper["id"], status=STATUS_DEBATE_ERROR, error=repr(e)[:500])
            continue
        keep = res.judge["verdict"] == "keep"
        store.update(paper["id"], status=STATUS_ACCEPTED if keep else STATUS_VETOED,
                     debate={"judge": res.judge, "transcript": res.transcript, "text_kind": res.text_kind},
                     debated_at=now_iso(), error=None)
        stats.debated += 1
        stats.accepted += keep
        stats.vetoed += not keep
    store.log_run("debate", fetched=len(queue), new=stats.accepted, errors=stats.errors,
                  note=f"vetoed={stats.vetoed} deferred={stats.deferred}")
    return stats
