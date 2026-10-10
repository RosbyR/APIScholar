"""Command-line entry point: `paperbot <command>`.

Typical daily schedule:  paperbot run
First week:              paperbot backfill --days 7 && paperbot triage && paperbot label
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from .calibrate import format_table, sample_for_labeling, suggest, sweep
from .config import Config, load_config
from .store import Store

log = logging.getLogger("paperbot")


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path)


def _last_digest_iso(store: Store) -> str:
    row = store.conn.execute("SELECT MAX(started) FROM runs WHERE stage='digest'").fetchone()
    if row and row[0]:
        return row[0]
    return (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()


# ---- stages -------------------------------------------------------------------
def cmd_harvest(cfg: Config, args) -> int:
    from .harvest import build_sources, make_client, run_harvest
    store = _store(cfg)
    with make_client(cfg) as client:
        stats = run_harvest(cfg, store, build_sources(cfg, client))
    print(json.dumps(stats.per_source, indent=2))
    return 1 if any(s["errors"] for s in stats.per_source.values()) else 0


def cmd_backfill(cfg: Config, args) -> int:
    from .harvest import backfill_arxiv, make_client
    store = _store(cfg)
    with make_client(cfg) as client:
        stats = backfill_arxiv(cfg, store, client, args.days)
    print(json.dumps(stats.per_source, indent=2))
    return 0


def cmd_triage(cfg: Config, args) -> int:
    from .triage import make_jev_client, run_triage
    stats = run_triage(cfg, _store(cfg), make_jev_client(cfg), limit=args.limit)
    print(stats)
    return 0


def cmd_regate(cfg: Config, args) -> int:
    from .triage import run_regate
    stats = run_regate(cfg, _store(cfg), dry_run=args.dry_run)
    for pid, old, new, reason in stats.changes:
        print(f"{pid}: {old} -> {new}  ({reason})")
    verb = "would change" if args.dry_run else "changed"
    print(f"checked {stats.checked}, {verb}: +{stats.newly_passed} passed, +{stats.newly_rejected} rejected")
    if stats.model_mismatch:
        print(f"note: {stats.model_mismatch} papers were scored by a different jev_model than "
              f"{cfg.section('triage').get('jev_model')}; re-gating them uses a threshold calibrated "
              "for another model.", file=sys.stderr)
    return 0


def _text_getter(cfg: Config):
    from .fulltext import get_text
    from .harvest import make_client
    from .http import RateLimiter
    client = make_client(cfg)
    limiter = RateLimiter(cfg.section("harvest").get("arxiv", {}).get("min_interval_s", 3.0))
    max_chars = cfg.section("debate").get("fulltext_max_chars", 60000)
    return lambda paper: get_text(paper, client, limiter, max_chars)


def cmd_debate(cfg: Config, args) -> int:
    from .debate import run_debate
    from .debate.llm import AnthropicLLM
    Config.env("ANTHROPIC_API_KEY", required=True)
    stats = run_debate(cfg, _store(cfg), AnthropicLLM(), _text_getter(cfg))
    print(stats)
    return 1 if stats.errors else 0


def cmd_digest(cfg: Config, args) -> int:
    from .digest import build_digest
    store = _store(cfg)
    path = build_digest(store, cfg.digest_dir, since_iso=_last_digest_iso(store))
    print(path)
    return 0


def cmd_run(cfg: Config, args) -> int:
    """Full pipeline under a single-instance lock (scheduled runs must not overlap)."""
    from .lock import LockHeld, RunLock
    lock = RunLock(cfg.db_path.parent / "paperbot.lock",
                   stale_after_s=float(cfg.settings.get("lock_stale_hours", 6)) * 3600)
    try:
        lock.acquire()
    except LockHeld as e:
        print(f"skipping: {e}", file=sys.stderr)
        return 2
    try:
        return _run_stages(cfg, args)
    finally:
        lock.release()


def _run_stages(cfg: Config, args) -> int:
    """Each stage runs even if an earlier one partly failed; the digest reports health."""
    rc = 0
    for name, fn in (("harvest", cmd_harvest), ("triage", cmd_triage), ("debate", cmd_debate)):
        try:
            rc |= fn(cfg, args)
        except Exception as e:
            log.exception("stage %s failed", name)
            _store(cfg).log_run(f"{name}:failed", errors=1, note=repr(e)[:500])
            rc = 1
    rc |= cmd_digest(cfg, args)
    return rc


# ---- inspection / calibration -----------------------------------------------------
def cmd_status(cfg: Config, args) -> int:
    store = _store(cfg)
    print(json.dumps(store.status_counts(), indent=2))
    for r in store.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10"):
        print(dict(r))
    return 0


def cmd_costs(cfg: Config, args) -> int:
    store = _store(cfg)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(microsecond=0).isoformat()
    rows = store.llm_cost_by_day(since)
    if not rows:
        print(f"No Claude calls in the last {args.days} days.")
        return 0
    print(f"{'day':<12}{'model':<22}{'papers':>7}{'calls':>7}{'in tok':>12}{'out tok':>10}{'USD':>9}")
    for r in rows:
        print(f"{r['day']:<12}{r['model']:<22}{r['papers']:>7}{r['calls']:>7}"
              f"{r['input_tokens']:>12,}{r['output_tokens']:>10,}{r['cost_usd']:>9.2f}")
    t = store.llm_cost_since(since)
    per_paper = t["cost_usd"] / t["papers"] if t["papers"] else 0
    print(f"\nTotal {args.days}d: ${t['cost_usd']:.2f} over {t['papers']} papers (${per_paper:.3f}/paper)")
    if t["unpriced"]:
        print(f"{t['unpriced']} calls have no price (model missing from debate.pricing); total is an undercount.")
    return 0


def cmd_show(cfg: Config, args) -> int:
    row = _store(cfg).get(args.paper_id)
    if not row:
        print(f"no paper {args.paper_id}", file=sys.stderr)
        return 1
    d = dict(row)
    for k in ("jev_answers", "debate", "authors", "sources", "categories"):
        if d.get(k):
            d[k] = json.loads(d[k])
    print(json.dumps(d, indent=2))
    return 0


def cmd_label(cfg: Config, args) -> int:
    store = _store(cfg)
    max_score = len(cfg.profile["relevance"]["levels"]) - 1
    batch = sample_for_labeling(store.unlabeled_scored(), args.n, max_score=max_score)
    if not batch:
        print("No scored, unlabeled papers. Run harvest + triage first.")
        return 0
    print("Label each abstract: y = I'd want this, n = not relevant, s = skip, q = quit\n")
    done = 0
    for row in batch:
        print("=" * 80)
        print(row["title"])
        print(f"[{row['venue'] or ''}]  (Jev score hidden to avoid anchoring)\n")
        print((row["abstract"] or "(no abstract)")[:2000])
        ans = input("\nrelevant? [y/n/s/q] ").strip().lower()
        if ans == "q":
            break
        if ans in ("y", "n"):
            store.add_label(row["id"], ans == "y")
            done += 1
    print(f"\nLabeled {done}. Total labels: {len(store.labeled_scores())}. Run `paperbot calibrate`.")
    return 0


def cmd_calibrate(cfg: Config, args) -> int:
    store = _store(cfg)
    labeled = store.labeled_scores()
    pos = sum(y for _, y in labeled)
    if len(labeled) < 30 or pos < 5:
        print(f"Only {len(labeled)} labels ({pos} relevant). Aim for 50+ with 10+ relevant before trusting this.")
        if not labeled or not pos:
            return 1
    max_score = len(cfg.profile["relevance"]["levels"]) - 1
    rows = sweep(labeled, beta=args.beta, max_score=max_score)
    best = suggest(rows)
    print(format_table(rows, best))
    current = cfg.section("triage").get("relevance_threshold")
    print(f"\nCurrent threshold: {current}. Suggested: {best.threshold} "
          f"(recall {best.recall:.0%}, precision {best.precision:.0%}). "
          "Edit triage.relevance_threshold in config/settings.yaml to apply.")
    return 0


def cmd_feedback(cfg: Config, args) -> int:
    rating = 1 if args.rating in ("up", "+", "y", "yes") else -1
    _store(cfg).add_feedback(args.paper_id, rating, args.note)
    print("recorded")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="paperbot")
    p.add_argument("--root", help="project root containing config/ (default: $PAPERBOT_ROOT or cwd)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest").set_defaults(fn=cmd_harvest)
    b = sub.add_parser("backfill"); b.add_argument("--days", type=int, default=7); b.set_defaults(fn=cmd_backfill)
    t = sub.add_parser("triage"); t.add_argument("--limit", type=int); t.set_defaults(fn=cmd_triage)
    g = sub.add_parser("regate", help="re-apply the gate from stored Jev answers after a threshold change")
    g.add_argument("--dry-run", action="store_true"); g.set_defaults(fn=cmd_regate)
    sub.add_parser("debate").set_defaults(fn=cmd_debate)
    sub.add_parser("digest").set_defaults(fn=cmd_digest)
    r = sub.add_parser("run"); r.set_defaults(fn=cmd_run, limit=None)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    co = sub.add_parser("costs", help="Claude token usage and spend per day/model")
    co.add_argument("--days", type=int, default=30); co.set_defaults(fn=cmd_costs)
    s = sub.add_parser("show"); s.add_argument("paper_id"); s.set_defaults(fn=cmd_show)
    l = sub.add_parser("label"); l.add_argument("--n", type=int, default=50); l.set_defaults(fn=cmd_label)
    c = sub.add_parser("calibrate"); c.add_argument("--beta", type=float, default=2.0); c.set_defaults(fn=cmd_calibrate)
    f = sub.add_parser("feedback"); f.add_argument("paper_id"); f.add_argument("rating", choices=["up", "down"])
    f.add_argument("--note"); f.set_defaults(fn=cmd_feedback)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.fn(load_config(args.root), args)


if __name__ == "__main__":
    sys.exit(main())
