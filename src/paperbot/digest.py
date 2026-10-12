"""Digest stage: render accepted papers + pipeline health into a dated Markdown file."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import STATUS_ACCEPTED, STATUS_DIGESTED, STATUS_PASSED, STATUS_SKIM, STATUS_TRIAGE_ERROR
from .store import Store

PRIORITY_ORDER = {"read now": 0, "skim": 1, "file away": 2}


def _health(store: Store, since_iso: str, today: date) -> list[str]:
    lines, warnings = [], []
    runs = store.runs_since(since_iso)
    harvest = [r for r in runs if r["stage"].startswith("harvest:")]
    for r in harvest:
        flag = " ⚠ error" if r["errors"] else ""
        lines.append(f"- {r['stage'].split(':', 1)[1]}: fetched {r['fetched']}, new {r['new']}{flag}")
        if r["errors"]:
            warnings.append(f"{r['stage']} failed: {r['note']}")
        if r["stage"] == "harvest:arxiv_rss" and r["fetched"] == 0 and today.weekday() < 5:
            warnings.append("arXiv RSS returned 0 items on a weekday — check the feed or the parser.")
    if not harvest:
        warnings.append("No harvest runs recorded since the last digest.")
    cost = store.llm_cost_since(since_iso)
    if cost["calls"]:
        lines.append(f"- Claude: {cost['papers']} paper(s) debated, {cost['calls']} calls, "
                     f"{cost['input_tokens']:,} in / {cost['output_tokens']:,} out tokens, ${cost['cost_usd']:.2f}")
        if cost["unpriced"]:
            warnings.append(f"{cost['unpriced']} Claude calls used a model with no price in debate.pricing; "
                            "the cost above is an undercount.")
    counts = store.status_counts()
    if counts.get(STATUS_TRIAGE_ERROR):
        warnings.append(f"{counts[STATUS_TRIAGE_ERROR]} papers failed Jev triage and will be retried.")
    if counts.get(STATUS_PASSED):
        warnings.append(f"{counts[STATUS_PASSED]} papers passed the gate but are waiting for debate (budget cap).")
    return lines + [f"- ⚠ {w}" for w in warnings]


def render_entry(row) -> str:
    deb = json.loads(row["debate"])
    j = deb["judge"]
    link = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    authors = json.loads(row["authors"] or "[]")
    who = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    out = [
        f"### [{row['title']}]({link})" if link else f"### {row['title']}",
        f"*{who or 'Unknown authors'} · {row['venue'] or 'unknown venue'} · {row['published'] or ''}*  ",
        f"**{j.get('read_priority', 'skim').capitalize()}** · relevance {j.get('relevance')}/5 · "
        f"Jev {row['jev_score']:.2f} · based on {deb.get('text_kind', 'abstract only')}",
        "",
        f"**Summary.** {j['summary']}",
        "",
        f"**Why you care.** {j['why_you_care']}",
    ]
    if j.get("key_evidence"):
        out += ["", "**Evidence.**"] + [f"- {e.get('claim')} ({e.get('where')})" for e in j["key_evidence"]]
    if j.get("caveats"):
        out += ["", "**Caveats.**"] + [f"- {c}" for c in j["caveats"]]
    if j.get("debate_takeaway"):
        out += ["", f"*Debate:* {j['debate_takeaway']}"]
    out += ["", f"`paperbot feedback {row['id']} up` / `down`", ""]
    return "\n".join(out)


def _first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def render_skim(row) -> str:
    link = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    title = f"[{row['title']}]({link})" if link else row["title"]
    return (f"- {title} · Jev {row['jev_score']:.2f} · {row['venue'] or 'unknown venue'}  \n"
            f"  {_first_sentence(row['abstract']) or '(no abstract)'} "
            f"`paperbot feedback {row['id']} up`")


def build_digest(store: Store, digest_dir: Path, today: date | None = None,
                 since_iso: str | None = None, max_skim: int = 25) -> Path | None:
    """Write digests/<date>.md for all accepted and skim papers not yet delivered. Returns the path.

    Skim papers passed the gate but were over the debate cap; they are listed by Jev score
    without a debate. Only the top `max_skim` are shown, but all are marked digested (the
    digest says how many were left out) so the skim backlog can't grow without bound.
    """
    today = today or datetime.now(timezone.utc).date()
    since_iso = since_iso or (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    rows = store.by_status(STATUS_ACCEPTED)
    rows.sort(key=lambda r: (PRIORITY_ORDER.get(json.loads(r["debate"])["judge"].get("read_priority"), 1),
                             -(r["jev_score"] or 0)))
    skims = store.by_status(STATUS_SKIM, order="jev_score DESC")
    parts = [f"# Paper digest — {today.isoformat()}", ""]
    if rows:
        parts.append(f"{len(rows)} paper(s) survived triage and debate.\n")
        parts += [render_entry(r) for r in rows]
    else:
        parts.append("Nothing new survived triage and debate this time.\n")
    if skims:
        parts += ["---", "## Also worth a look", "",
                  "Passed triage but were over the debate budget; not debated, ranked by Jev score.", ""]
        parts += [render_skim(r) for r in skims[:max_skim]]
        if len(skims) > max_skim:
            parts.append(f"\n*+{len(skims) - max_skim} more below these scores, not shown "
                         f"(lowest shown: Jev {skims[max_skim - 1]['jev_score']:.2f}).*")
        parts.append("")
    parts += ["---", "## Pipeline health", *_health(store, since_iso, today), ""]
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{today.isoformat()}.md"
    path.write_text("\n".join(parts), encoding="utf-8")
    for r in [*rows, *skims]:
        store.update(r["id"], status=STATUS_DIGESTED, digest_date=today.isoformat())
    store.log_run("digest", fetched=len(rows) + len(skims), new=len(rows), note=f"{path} skim={len(skims)}")
    return path
