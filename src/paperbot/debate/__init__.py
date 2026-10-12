"""Debate stage: advocate vs skeptic (opening + rebuttal), then a judge with veto power."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Config
from ..models import STATUS_ACCEPTED, STATUS_DEBATE_ERROR, STATUS_PASSED, STATUS_SKIM, STATUS_VETOED
from ..store import Store, now_iso
from ..triage.questions import FACET_PREFIX
from . import prompts
from .llm import LLM, cost_usd

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
    calls: list[dict[str, Any]]


def debate_paper(paper: dict[str, Any], text: str, text_kind: str, profile: dict[str, Any],
                 llm: LLM, models: dict[str, str], max_tokens: int = 16000,
                 calls: list[dict[str, Any]] | None = None) -> DebateResult:
    """Run one debate. Token usage of every LLM call is appended to `calls` as it happens,
    so a caller that passes its own list still sees the spend if a later turn raises."""
    calls = [] if calls is None else calls
    answers = paper.get("jev_answers") or {}
    if isinstance(answers, str):
        answers = json.loads(answers)
    facets = {k.removeprefix(FACET_PREFIX): v["noul"] for k, v in answers.items()
              if k.startswith(FACET_PREFIX) and "noul" in v}
    context = prompts.reader_block(profile) + "\n\n" + prompts.paper_block(paper, text, text_kind, facets)

    def ask(role: str, system: str, extra: str = "") -> str:
        try:
            return llm.complete(model=models[role], system=system,
                                prompt=context + ("\n\n" + extra if extra else ""), max_tokens=max_tokens)
        finally:
            usage = getattr(llm, "last_usage", None)  # set by AnthropicLLM; fakes may omit it
            if usage:
                calls.append({"role": role, **usage})
                llm.last_usage = None

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
    return DebateResult(judge=judge, transcript=t, text_kind=text_kind, calls=calls)


@dataclass
class DebateStats:
    debated: int = 0
    accepted: int = 0
    vetoed: int = 0
    errors: int = 0
    deferred: int = 0         # over the cap and still queued (overflow=wait, or failed debates)
    skimmed: int = 0          # over the cap and moved to the skim list (overflow=skim)
    input_tokens: int = 0     # including cache reads/writes
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0   # calls on a model missing from debate.pricing


def run_debate(cfg: Config, store: Store, llm: LLM,
               get_text: Callable[[dict], tuple[str, str]], ids: list[str] | None = None) -> DebateStats:
    """Debate the top `max_papers_per_run` queued papers. With `ids`, debate exactly those
    papers (if they are passed/debate_error) and leave the rest of the queue untouched."""
    d = cfg.section("debate")
    stats = DebateStats()
    if not d.get("enabled", True):
        return stats
    cap = int(d.get("max_papers_per_run", 8))
    models = {"advocate": d.get("advocate_model"), "skeptic": d.get("skeptic_model"), "judge": d.get("judge_model")}
    pricing = d.get("pricing") or {}
    if ids:
        rows = [store.get(i) for i in ids]
        for i, r in zip(ids, rows):
            if r is None or r["status"] not in (STATUS_PASSED, STATUS_DEBATE_ERROR):
                log.warning("skipping %s: %s", i, "no such paper" if r is None else f"status is {r['status']}")
        queue = [r for r in rows if r is not None and r["status"] in (STATUS_PASSED, STATUS_DEBATE_ERROR)]
        cap = len(queue)
    else:
        queue = store.by_status(STATUS_PASSED, STATUS_DEBATE_ERROR, order="jev_score DESC")
    # Over the cap: with overflow=skim (default) passed papers go to the digest's skim list
    # instead of queueing forever; with overflow=wait they stay `passed` for the next run.
    # Failed debates always stay queued for retry.
    for row in queue[cap:]:
        if d.get("overflow", "skim") == "skim" and row["status"] == STATUS_PASSED:
            store.update(row["id"], status=STATUS_SKIM)
            stats.skimmed += 1
        else:
            stats.deferred += 1
    for row in queue[:cap]:
        paper = dict(row)
        calls: list[dict[str, Any]] = []
        try:
            text, kind = get_text(paper)
            res = debate_paper(paper, text, kind, cfg.profile, llm, models, d.get("max_tokens", 16000), calls)
        except Exception as e:
            log.exception("debate failed for %s", paper["id"])
            stats.errors += 1
            store.update(paper["id"], status=STATUS_DEBATE_ERROR, error=repr(e)[:500])
            continue
        finally:
            # Failed debates still spent tokens; record them either way.
            paper_cost = _record_calls(store, stats, paper["id"], calls, pricing)
        keep = res.judge["verdict"] == "keep"
        store.update(paper["id"], status=STATUS_ACCEPTED if keep else STATUS_VETOED,
                     debate={"judge": res.judge, "transcript": res.transcript, "text_kind": res.text_kind,
                             "usage": {"calls": calls, "cost_usd": paper_cost}},
                     debated_at=now_iso(), error=None)
        stats.debated += 1
        stats.accepted += keep
        stats.vetoed += not keep
    note = (f"vetoed={stats.vetoed} deferred={stats.deferred} skimmed={stats.skimmed} in={stats.input_tokens} "
            f"out={stats.output_tokens} cost_usd={stats.cost_usd:.4f}")
    if stats.unpriced_calls:
        note += f" unpriced_calls={stats.unpriced_calls}"
    store.log_run("debate", fetched=len(queue), new=stats.accepted, errors=stats.errors, note=note)
    return stats


def _record_calls(store: Store, stats: DebateStats, paper_id: str, calls: list[dict[str, Any]],
                  pricing: dict[str, dict[str, float]]) -> float | None:
    """Price each call, persist it to llm_calls, and add it to the run totals.
    Returns the paper's total cost, or None if any call's model has no price."""
    total, unpriced = 0.0, False
    for c in calls:
        c["cost_usd"] = cost_usd(c, pricing)
        if c["cost_usd"] is None:
            unpriced = True
            stats.unpriced_calls += 1
            log.warning("no price for model %s; add it to debate.pricing in settings.yaml", c["model"])
        else:
            total += c["cost_usd"]
        stats.input_tokens += c["input_tokens"] + c["cache_read_input_tokens"] + c["cache_creation_input_tokens"]
        stats.output_tokens += c["output_tokens"]
        store.log_llm_call(paper_id, c)
    stats.cost_usd += total
    return None if unpriced else round(total, 6)
