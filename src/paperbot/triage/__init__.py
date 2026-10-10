"""Triage stage: score every new paper with Jev, then gate on the threshold."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..config import Config
from ..models import STATUS_NEW, STATUS_PASSED, STATUS_REJECTED, STATUS_TRIAGE_ERROR
from ..store import Store, now_iso
from .gate import decide
from .jev import JevClient, JevError, JevFatalError
from .questions import RELEVANCE_KEY, build_questions, build_state, must_have_keys

log = logging.getLogger(__name__)


@dataclass
class TriageStats:
    scored: int = 0
    passed: int = 0
    rejected: int = 0
    errors: int = 0
    input_tokens: int = 0


@dataclass
class RegateStats:
    checked: int = 0
    newly_passed: int = 0      # rejected -> passed
    newly_rejected: int = 0    # passed -> rejected
    model_mismatch: int = 0    # answers came from a different jev_model than configured
    changes: list[tuple[str, str, str, str]] = field(default_factory=list)  # (id, old, new, reason)


def _decide(t: dict, answers: dict, must: list[str]):
    return decide(answers, float(t.get("relevance_threshold", 2.5)), must,
                  t.get("rescue_margin", 0.5), t.get("rescue_noul", 0.9))


def run_regate(cfg: Config, store: Store, dry_run: bool = False) -> RegateStats:
    """Re-apply the gate to `rejected`/`passed` papers from stored jev_answers (no Jev calls).

    Use after changing relevance_threshold / rescue settings. Papers already debated or
    digested are left alone. Answers from a different jev_model than the configured one are
    still re-gated, but counted, because the threshold was calibrated for the configured model.
    """
    t = cfg.section("triage")
    must = must_have_keys(cfg.profile)
    pinned = t.get("jev_model")
    stats = RegateStats()
    for row in store.by_status(STATUS_REJECTED, STATUS_PASSED):
        if not row["jev_answers"]:
            continue
        stats.checked += 1
        if pinned and row["jev_model"] and row["jev_model"] != pinned:
            stats.model_mismatch += 1
        d = _decide(t, json.loads(row["jev_answers"]), must)
        new = STATUS_PASSED if d.passed else STATUS_REJECTED
        if new == row["status"]:
            continue
        stats.changes.append((row["id"], row["status"], new, d.reason))
        if new == STATUS_PASSED:
            stats.newly_passed += 1
        else:
            stats.newly_rejected += 1
        if not dry_run:
            store.update(row["id"], status=new)
    if not dry_run:
        store.log_run("regate", fetched=stats.checked, new=stats.newly_passed,
                      note=f"newly_rejected={stats.newly_rejected} model_mismatch={stats.model_mismatch}")
    return stats


def make_jev_client(cfg: Config) -> JevClient:
    t = cfg.section("triage")
    return JevClient(
        api_key=Config.env("TYPESAFE_API_KEY", required=True),
        model=t.get("jev_model", "jev-latest"),
        timeout_s=t.get("timeout_s", 20),
    )


def run_triage(cfg: Config, store: Store, jev: JevClient, limit: int | None = None) -> TriageStats:
    t = cfg.section("triage")
    questions = build_questions(cfg.profile)
    must = must_have_keys(cfg.profile)
    rows = store.by_status(STATUS_NEW, STATUS_TRIAGE_ERROR, limit=limit)
    stats = TriageStats()
    if not rows:
        return stats

    def work(row):
        return row, jev.evaluate(build_state(row["title"], row["abstract"], row["venue"]), questions)

    fatal: JevFatalError | None = None
    with ThreadPoolExecutor(max_workers=int(t.get("concurrency", 8))) as pool:
        # Write results by paper id (never by position) so reordering can't mislabel papers.
        futures = {pool.submit(work, r): r["id"] for r in rows}
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                _, resp = fut.result()
            except JevFatalError as e:
                fatal = fatal or e
                continue
            except JevError as e:
                stats.errors += 1
                store.update(pid, status=STATUS_TRIAGE_ERROR, error=str(e)[:500])
                continue
            answers = resp["answers"]
            d = _decide(t, answers, must)
            store.update(
                pid,
                status=STATUS_PASSED if d.passed else STATUS_REJECTED,
                jev_score=d.score,
                jev_confidence=answers[RELEVANCE_KEY].get("confidence"),
                jev_answers=answers,
                jev_model=resp.get("model"),
                triaged_at=now_iso(),
                error=None,
            )
            stats.scored += 1
            stats.input_tokens += (resp.get("usage") or {}).get("input_tokens", 0)
            if d.passed:
                stats.passed += 1
            else:
                stats.rejected += 1
    store.log_run("triage", fetched=len(rows), new=stats.passed, errors=stats.errors,
                  note=f"scored={stats.scored} tokens={stats.input_tokens}")
    if fatal:
        raise fatal
    return stats
