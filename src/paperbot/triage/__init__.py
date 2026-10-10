"""Triage stage: score every new paper with Jev, then gate on the threshold."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

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
    threshold = float(t.get("relevance_threshold", 2.5))
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
            d = decide(answers, threshold, must, t.get("rescue_margin", 0.5), t.get("rescue_noul", 0.9))
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
