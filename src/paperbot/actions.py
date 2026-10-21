"""Owner actions started from the website: fetch papers, Jev triage, friends' scoring, debates,
and writing a digest. They run in a background thread, one at a time, under the same run lock
as `paperbot run`, so a button press can never overlap a scheduled or command-line run.

Debates from the website always name their papers (selected, or the current top N), which is
`paperbot debate --id`: the rest of the queue is left alone. A plain `paperbot debate` would
move everything over the cap to the skim list, which a "debate a few" button must not do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .lock import LockHeld, RunLock
from .models import STATUS_DEBATE_ERROR, STATUS_PASSED
from .store import Store, now_iso

log = logging.getLogger(__name__)

ACTIONS = ("harvest", "triage", "friends", "debate", "digest")
MAX_DEBATE = 20  # per button press


@dataclass
class Deps:
    """External clients, built lazily so tests can pass fakes and missing keys only fail the
    action that needs them."""
    jev: Callable[[], Any] | None = None
    llm: Callable[[], Any] | None = None
    get_text: Callable[[], Callable] | None = None
    sources: Callable[[Store], list] | None = None


def debate_ids(store: Store, ids: list[str] | None, top: int | None) -> list[str]:
    queue = [r["id"] for r in store.by_status(STATUS_PASSED, STATUS_DEBATE_ERROR, order="jev_score DESC")]
    if ids:
        wanted = set(ids)
        chosen = [i for i in queue if i in wanted]  # queue order, only papers actually waiting
    else:
        chosen = queue[: max(0, top or 0)]
    return chosen[:MAX_DEBATE]


def run_action(cfg: Config, action: str, report: Callable[[str], None], ids: list[str] | None = None,
               top: int | None = None, deps: Deps | None = None) -> None:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    deps = deps or Deps()
    lock = RunLock(cfg.db_path.parent / "paperbot.lock",
                   stale_after_s=float(cfg.settings.get("lock_stale_hours", 6)) * 3600)
    try:
        lock.acquire()
    except LockHeld:
        report("Another run is in progress (a button or `paperbot run`). Try again when it finishes.")
        return
    store = Store(cfg.db_path)
    try:
        globals()[f"_{action}"](cfg, store, report, deps, ids=ids, top=top)
    finally:
        store.close()
        lock.release()


def _harvest(cfg, store, report, deps, **_):
    from .harvest import build_sources, make_client, run_harvest
    report("Fetching new papers from arXiv and OpenAlex…")
    if deps.sources:
        stats = run_harvest(cfg, store, deps.sources(store))
    else:
        with make_client(cfg) as client:
            stats = run_harvest(cfg, store, build_sources(cfg, client, store))
    parts = [f"{name}: {s['new']} new of {s['fetched']}" + (" (failed)" if s["errors"] else "")
             for name, s in stats.per_source.items()]
    report(f"Fetched. {'; '.join(parts) or 'no sources enabled'}. Next: Score with Jev.")


def _triage(cfg, store, report, deps, **_):
    from .triage import make_jev_client, run_triage
    report("Scoring new papers with Jev…")
    stats = run_triage(cfg, store, deps.jev() if deps.jev else make_jev_client(cfg))
    cost = stats.input_tokens * 0.042 / 1e6
    report(f"Jev scored {stats.scored} papers: {stats.passed} passed the gate, {stats.rejected} rejected"
           + (f", {stats.errors} failed (retried next time)" if stats.errors else "") + f". About ${cost:.4f}.")


def _friends(cfg, store, report, deps, **_):
    from .friends import run_friends
    from .triage import make_jev_client
    report("Scoring new papers for friends with Jev…")
    out = run_friends(store, deps.jev() if deps.jev else make_jev_client(cfg),
                      int(cfg.section("friends").get("max_papers_per_run", 1500)),
                      int(cfg.section("triage").get("concurrency", 8)))
    if not out:
        return report("No friends are set up yet.")
    report("Friends scored: " + "; ".join(f"{n} {s.scored}" + (f" ({s.errors} failed)" if s.errors else "")
                                          for n, s in out.items())
           + f". About ${sum(s.cost_usd for s in out.values()):.4f}.")


def _debate(cfg, store, report, deps, ids=None, top=None):
    from .debate import run_debate
    chosen = debate_ids(store, ids, top)
    if not chosen:
        return report("Nothing to debate: none of those papers are waiting in the queue.")
    if deps.llm:
        llm, get_text = deps.llm(), deps.get_text()
    else:
        from .cli import _text_getter
        from .debate.llm import AnthropicLLM
        Config.env("ANTHROPIC_API_KEY", required=True)
        llm, get_text = AnthropicLLM(), _text_getter(cfg)
    started = now_iso()
    kept = vetoed = failed = 0
    for i, pid in enumerate(chosen, 1):
        title = (store.get(pid)["title"] or pid)[:90]
        report(f"Debating {i} of {len(chosen)}: {title}")
        st = run_debate(cfg, store, llm, get_text, ids=[pid])
        kept, vetoed, failed = kept + st.accepted, vetoed + st.vetoed, failed + st.errors
    cost = store.llm_cost_since(started)["cost_usd"]
    report(f"Debated {len(chosen)}: {kept} kept, {vetoed} vetoed"
           + (f", {failed} failed (still queued)" if failed else "")
           + f". Claude cost ${cost:.2f}. Kept papers go in the next digest (Write digest).")


def _digest(cfg, store, report, deps, **_):
    from .cli import _write_digest
    path = _write_digest(cfg)
    report(f"Wrote {path.name}. It's at the top of the Digests tab.")
