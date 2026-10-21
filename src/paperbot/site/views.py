"""Server-rendered tracking pages for `paperbot serve`: votes, the Jev bank, the debate queue,
and one page per paper. Read-only except the thumb buttons (site/app.js -> /api/feedback).

Everything shown comes from the database and is untrusted text (titles, abstracts, LLM output),
so every value goes through `e()` before it reaches HTML.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import urlencode

from ..config import Config
from ..models import (STATUS_ACCEPTED, STATUS_DEBATE_ERROR, STATUS_DIGESTED, STATUS_NEW, STATUS_PASSED,
                      STATUS_REJECTED, STATUS_SKIM, STATUS_TRIAGE_ERROR, STATUS_VETOED)
from ..store import Store

PAGE_SIZE = 50
STATUSES = [STATUS_PASSED, STATUS_DEBATE_ERROR, STATUS_ACCEPTED, STATUS_DIGESTED, STATUS_SKIM, STATUS_VETOED,
            STATUS_REJECTED, STATUS_TRIAGE_ERROR, STATUS_NEW]
STATUS_HELP = {
    STATUS_PASSED: "passed the gate, waiting for debate",
    STATUS_DEBATE_ERROR: "debate failed, retried next run",
    STATUS_ACCEPTED: "judge kept it, goes in the next digest",
    STATUS_DIGESTED: "in a digest",
    STATUS_SKIM: "passed but over the debate cap, listed undebated",
    STATUS_VETOED: "judge vetoed it",
    STATUS_REJECTED: "below the Jev threshold",
    STATUS_TRIAGE_ERROR: "Jev failed, retried next run",
    STATUS_NEW: "not scored yet",
}
STATUS_HELP["friends_only"] = "fetched only for friends' feeds"
SORTS = {"score": "jev_score DESC, first_seen DESC", "newest": "COALESCE(published, first_seen) DESC",
         "oldest": "COALESCE(published, first_seen) ASC"}


def e(x: Any) -> str:
    return escape("" if x is None else str(x), quote=True)


def q(**params: Any) -> str:
    """Query string from the non-empty params."""
    kept = {k: v for k, v in params.items() if v not in (None, "")}
    return "?" + urlencode(kept) if kept else ""


# ---- shared pieces --------------------------------------------------------------------
@dataclass
class Ctx:
    cfg: Config
    store: Store

    @property
    def threshold(self) -> float:
        return float(self.cfg.section("triage").get("relevance_threshold", 2.5))

    @property
    def max_score(self) -> int:
        return len(self.cfg.profile["relevance"]["levels"]) - 1

    def level_names(self) -> list[str]:
        return [lvl.split(".", 1)[0].strip() for lvl in self.cfg.profile["relevance"]["levels"]]

    def facets(self) -> list[str]:
        return list((self.cfg.profile.get("facets") or {}).keys())


def buttons(pid: str) -> str:
    return (f'<span class="fb" data-id="{e(pid)}">'
            '<button type="button" data-r="1" aria-pressed="false" title="Relevant">👍</button>'
            '<button type="button" data-r="-1" aria-pressed="false" title="Not relevant">👎</button>'
            '<span class="fb-msg" role="status"></span></span>')


def score_badge(score: float | None, threshold: float) -> str:
    if score is None:
        return '<span class="score none">–</span>'
    cls = "pass" if score >= threshold else "fail"
    return f'<span class="score {cls}" title="Jev relevance (threshold {threshold})">{score:.2f}</span>'


def status_badge(status: str) -> str:
    return f'<span class="status st-{e(status)}" title="{e(STATUS_HELP.get(status, ""))}">{e(status)}</span>'


def facet_chips(answers: dict | None, min_val: float = 0.5) -> str:
    if not answers:
        return ""
    chips = [(k.removeprefix("facet_"), v.get("noul")) for k, v in answers.items()
             if k.startswith("facet_") and isinstance(v, dict) and (v.get("noul") or 0) >= min_val]
    return "".join(f'<span class="chip" title="Jev Noul {v:.2f}">{e(k.replace("_", " "))}</span>'
                   for k, v in sorted(chips, key=lambda c: -c[1]))


def paper_link(row) -> str:
    return f'<a class="title" href="paper{q(id=row["id"])}">{e(row["title"])}</a>'


def paper_item(ctx: Ctx, row, extra: str = "") -> str:
    answers = json.loads(row["jev_answers"]) if row["jev_answers"] else None
    meta = " · ".join(e(x) for x in (row["venue"], (row["published"] or "")[:10]) if x)
    abstract = e(row["abstract"]) or "<em>(no abstract)</em>"
    return f"""<li class="paper">
<div class="row-top">{score_badge(row["jev_score"], ctx.threshold)}{status_badge(row["status"])}{extra}<span class="meta">{meta}</span></div>
{paper_link(row)}
<div class="chips">{facet_chips(answers)}</div>
<details><summary>Abstract</summary><p>{abstract}</p></details>
{buttons(row["id"])}
</li>"""


def nav(active: str) -> str:
    links = [("./", "Digests"), ("votes", "My votes"), ("bank", "Jev bank"), ("queue", "Debate queue")]
    return '<nav class="tabs">' + "".join(
        f'<a href="{h}"{" aria-current=page" if h == active else ""}>{t}</a>' for h, t in links) + "</nav>"


# ---- votes ------------------------------------------------------------------------------
def votes_page(ctx: Ctx, show: str = "relevant") -> tuple[str, str]:
    c = ctx.store.conn
    rows = list(c.execute(
        """SELECT p.*, l.relevant, l.labeled_at,
                  (SELECT note FROM feedback f WHERE f.paper_id = p.id ORDER BY f.id DESC LIMIT 1) AS note,
                  (SELECT COUNT(*) FROM feedback f WHERE f.paper_id = p.id) AS thumbs
           FROM labels l JOIN papers p ON p.id = l.paper_id ORDER BY l.labeled_at DESC"""))
    rel = [r for r in rows if r["relevant"]]
    notrel = [r for r in rows if not r["relevant"]]
    t = ctx.threshold

    def gate_line(group, word):
        scored = [r for r in group if r["jev_score"] is not None]
        above = sum(r["jev_score"] >= t for r in scored)
        return f"{len(group)} {word}: {above} score at or above the threshold ({t}), {len(scored) - above} below"

    summary = (f'<p class="lead">{gate_line(rel, "relevant")}. {gate_line(notrel, "not relevant")}. '
               "Relevant papers below the threshold are ones the gate would miss; that's what "
               "<code>paperbot calibrate</code> tunes against.</p>")
    tabs = "".join(f'<a href="votes{q(show=k)}"{" aria-current=page" if k == show else ""}>{lbl} ({n})</a>'
                   for k, lbl, n in (("relevant", "👍 Relevant", len(rel)), ("not", "👎 Not relevant", len(notrel)),
                                     ("all", "All", len(rows))))
    group = {"relevant": rel, "not": notrel}.get(show, rows)
    items = []
    for r in group:
        how = "👍/👎 vote" if r["thumbs"] else "hand label"
        extra = f'<span class="meta">{how}, {e((r["labeled_at"] or "")[:10])}</span>'
        if r["note"]:
            extra += f'<span class="note">“{e(r["note"])}”</span>'
        items.append(paper_item(ctx, r, extra))
    body = (f"<h1>My votes</h1>{summary}<nav class=\"subtabs\">{tabs}</nav>"
            + (f'<ul class="papers">{"".join(items)}</ul>' if items else "<p>No votes here yet.</p>"))
    return "My votes", nav("votes") + body


# ---- Jev bank --------------------------------------------------------------------------
def bank_page(ctx: Ctx, params: dict[str, str]) -> tuple[str, str]:
    c = ctx.store.conn
    status = params.get("status", "")
    facet = params.get("facet", "")
    text = params.get("q", "").strip()
    sort = params.get("sort", "score") if params.get("sort") in SORTS else "score"
    voted = params.get("voted", "")
    try:
        min_score = float(params["min"]) if params.get("min") else None
    except ValueError:
        min_score = None
    try:
        page = max(1, int(params.get("page") or 1))
    except ValueError:
        page = 1

    where, args = ["jev_score IS NOT NULL"], []
    if status in STATUSES:
        where.append("status = ?"); args.append(status)
    if min_score is not None:
        where.append("jev_score >= ?"); args.append(min_score)
    if facet in ctx.facets():
        where.append("CAST(json_extract(jev_answers, ?) AS REAL) >= 0.5"); args.append(f"$.facet_{facet}.noul")
    if text:
        where.append("(title LIKE ? OR abstract LIKE ?)"); args += [f"%{text}%"] * 2
    if voted == "yes":
        where.append("id IN (SELECT paper_id FROM labels)")
    elif voted == "no":
        where.append("id NOT IN (SELECT paper_id FROM labels)")
    sql_where = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM papers WHERE {sql_where}", args).fetchone()[0]
    rows = list(c.execute(f"SELECT * FROM papers WHERE {sql_where} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
                          [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE]))

    # Overview: status counts and a score histogram over the whole bank (not the filter).
    counts = dict(c.execute("SELECT status, COUNT(*) FROM papers GROUP BY status").fetchall())
    scored = c.execute("SELECT COUNT(*) FROM papers WHERE jev_score IS NOT NULL").fetchone()[0]
    names = ctx.level_names()
    hist = [c.execute("SELECT COUNT(*) FROM papers WHERE jev_score >= ? AND jev_score < ?",
                      (i - 0.5, i + 0.5)).fetchone()[0] for i in range(ctx.max_score + 1)]
    peak = max(hist) or 1
    bars = "".join(
        f'<div class="bar"><span class="lbl">{i} {e(names[i] if i < len(names) else "")}</span>'
        f'<span class="track"><span class="fill" style="width:{100 * n / peak:.1f}%"></span></span>'
        f'<span class="n">{n:,}</span></div>' for i, n in enumerate(hist))
    status_links = " · ".join(f'<a href="bank{q(status=s)}">{e(s)} {counts[s]:,}</a>'
                              for s in STATUSES if counts.get(s))

    def opt(value, label, current):
        return f'<option value="{e(value)}"{" selected" if value == current else ""}>{e(label)}</option>'

    form = f"""<form class="filters" method="get" action="bank">
<input type="search" name="q" value="{e(text)}" placeholder="Search title or abstract">
<select name="status">{opt("", "Any status", status)}{"".join(opt(s, s, status) for s in STATUSES)}</select>
<select name="facet">{opt("", "Any facet", facet)}{"".join(opt(f, f.replace("_", " "), facet) for f in ctx.facets())}</select>
<select name="voted">{opt("", "Voted or not", voted)}{opt("yes", "Voted", voted)}{opt("no", "Not voted", voted)}</select>
<input type="number" name="min" step="0.1" min="0" max="{ctx.max_score}" value="{e(params.get("min", ""))}" placeholder="Min score">
<select name="sort">{opt("score", "Highest score", sort)}{opt("newest", "Newest", sort)}{opt("oldest", "Oldest", sort)}</select>
<button type="submit">Filter</button> <a href="bank">Reset</a>
</form>"""
    pages = max(1, -(-total // PAGE_SIZE))
    keep = {k: v for k, v in params.items() if k != "page"}
    pager = '<nav class="pager">' + (
        f'<a href="bank{q(**keep, page=page - 1)}">← Prev</a>' if page > 1 else "<span></span>") + (
        f"<span>Page {page} of {pages} · {total:,} paper{'' if total == 1 else 's'}</span>") + (
        f'<a href="bank{q(**keep, page=page + 1)}">Next →</a>' if page < pages else "<span></span>") + "</nav>"
    body = f"""<h1>Jev bank</h1>
<!--actions-->
<p class="lead">{scored:,} papers scored by Jev ({e(ctx.cfg.section("triage").get("jev_model", ""))}). Threshold
{ctx.threshold}: green scores pass the gate. Facet chips are topic flags Jev rated ≥ 0.5.</p>
<div class="hist">{bars}</div>
<p class="statuses">{status_links}</p>
{form}{pager}
<ul class="papers">{"".join(paper_item(ctx, r) for r in rows) or "<li>No papers match.</li>"}</ul>
{pager if rows else ""}"""
    return "Jev bank", nav("bank") + body


# ---- debate queue -----------------------------------------------------------------------
def queue_page(ctx: Ctx) -> tuple[str, str]:
    d = ctx.cfg.section("debate")
    cap = int(d.get("max_papers_per_run", 8))
    overflow = d.get("overflow", "skim")
    queue = ctx.store.by_status(STATUS_PASSED, STATUS_DEBATE_ERROR, order="jev_score DESC")
    cost = ctx.store.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), COUNT(DISTINCT paper_id) FROM llm_calls WHERE cost_usd IS NOT NULL").fetchone()
    per_paper = cost[0] / cost[1] if cost[1] else None
    nxt = min(cap, len(queue))
    est = f" (about ${per_paper * nxt:.2f} at your average ${per_paper:.2f}/paper)" if per_paper else ""
    if overflow == "skim" and len(queue) > cap:
        fate = (f"The other {len(queue) - cap} are <b>not</b> kept for later: with <code>debate.overflow: skim</code> "
                f"they move to the digest's skim list (top {ctx.cfg.section('digest').get('max_skim', 25)} shown), "
                "undebated. Set <code>overflow: wait</code> in settings.yaml to keep them queued instead.")
    elif len(queue) > cap:
        fate = f"The other {len(queue) - cap} wait for later runs, highest score first."
    else:
        fate = ""
    items = []
    for i, r in enumerate(queue, 1):
        if i == cap + 1:
            items.append(f'<li class="cutoff">Debate cap ({cap}) — below this line: '
                         f'{"skim list" if overflow == "skim" else "later runs"}</li>')
        pick = (f'<input type="checkbox" name="ids" value="{e(r["id"])}" form="debate-form" '
                f'aria-label="Select for debate">')
        items.append(paper_item(ctx, r, f'{pick}<span class="pos">#{i}</span>'))
    skims = ctx.store.conn.execute("SELECT COUNT(*) FROM papers WHERE status = ?", (STATUS_SKIM,)).fetchone()[0]
    body = f"""<h1>Debate queue</h1>
<!--actions-->
<p class="lead">{len(queue)} paper(s) passed the Jev gate and are waiting. The next <code>paperbot run</code>
debates the top {nxt}, highest Jev score first{est}. {fate}</p>
<p class="statuses">Also: <a href="bank{q(status=STATUS_SKIM)}">{skims} on the skim list</a> ·
<a href="bank{q(status=STATUS_ACCEPTED)}">accepted</a> · <a href="bank{q(status=STATUS_VETOED)}">vetoed</a> ·
<a href="bank{q(status=STATUS_DIGESTED)}">digested</a></p>
<ul class="papers">{"".join(items) or "<li>The queue is empty.</li>"}</ul>"""
    return "Debate queue", nav("queue") + body


# ---- one paper ------------------------------------------------------------------------
def paper_page(ctx: Ctx, pid: str) -> tuple[str, str] | None:
    row = ctx.store.get(pid)
    if row is None:
        return None
    c = ctx.store.conn
    answers = json.loads(row["jev_answers"]) if row["jev_answers"] else {}
    link = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    title = (f'<a href="{e(link)}" rel="noopener" target="_blank">{e(row["title"])}</a>'
             if link.startswith(("http://", "https://")) else e(row["title"]))
    authors = json.loads(row["authors"] or "[]")
    parts = [f"<h1>{title}</h1>",
             f'<p class="meta">{e(", ".join(authors[:8]) + (" et al." if len(authors) > 8 else ""))}<br>'
             f'{e(row["venue"] or "")} · {e(row["published"] or "")} · <code>{e(row["id"])}</code></p>',
             f'<div class="row-top">{score_badge(row["jev_score"], ctx.threshold)}{status_badge(row["status"])}'
             f'<span class="meta">{e(STATUS_HELP.get(row["status"], ""))}</span></div>',
             buttons(row["id"])]
    label = c.execute("SELECT relevant, labeled_at FROM labels WHERE paper_id = ?", (pid,)).fetchone()
    notes = list(c.execute("SELECT rating, note, at FROM feedback WHERE paper_id = ? ORDER BY id", (pid,)))
    if label or notes:
        lines = [f"<li>Label: {'relevant' if label['relevant'] else 'not relevant'} ({e(label['labeled_at'][:10])})</li>"] if label else []
        lines += [f"<li>{'👍' if n['rating'] > 0 else '👎'} {e(n['at'][:10])}{': ' + e(n['note']) if n['note'] else ''}</li>"
                  for n in notes]
        parts.append(f"<h2>Your votes</h2><ul>{''.join(lines)}</ul>")
    rel = answers.get("relevance") or {}
    if rel:
        names = ctx.level_names()
        probs = rel.get("probabilities") or {}
        bars = "".join(
            f'<div class="bar"><span class="lbl">{k} {e(names[int(k)] if int(k) < len(names) else "")}</span>'
            f'<span class="track"><span class="fill" style="width:{100 * float(v):.0f}%"></span></span>'
            f'<span class="n">{float(v):.2f}</span></div>' for k, v in sorted(probs.items(), key=lambda kv: int(kv[0])))
        facets = "".join(
            f'<div class="bar"><span class="lbl">{e(k.removeprefix("facet_").replace("_", " "))}</span>'
            f'<span class="track"><span class="fill" style="width:{100 * float(v.get("noul") or 0):.0f}%"></span></span>'
            f'<span class="n">{float(v.get("noul") or 0):.2f}</span></div>'
            for k, v in answers.items() if k.startswith("facet_") and isinstance(v, dict))
        parts.append(f'<h2>Jev</h2><p class="meta">Score {rel.get("score")} · confidence {rel.get("confidence")} · '
                     f'{e(row["jev_model"] or "")}</p><h3 class="plain">Relevance level probabilities</h3>'
                     f'<div class="hist">{bars}</div><h3 class="plain">Topic facets (Nouls)</h3><div class="hist">{facets}</div>')
    if row["debate"]:
        deb = json.loads(row["debate"])
        j = deb.get("judge") or {}
        ev = "".join(f"<li>{e(x.get('claim'))} <em>({e(x.get('where'))})</em></li>" for x in j.get("key_evidence") or [])
        cav = "".join(f"<li>{e(x)}</li>" for x in j.get("caveats") or [])
        parts.append(
            f"<h2>Debate verdict: {e(j.get('verdict'))} · {e(j.get('read_priority'))} · relevance {e(j.get('relevance'))}/5</h2>"
            f"<p><b>Summary.</b> {e(j.get('summary'))}</p><p><b>Why you care.</b> {e(j.get('why_you_care'))}</p>"
            + (f"<p><b>Evidence.</b></p><ul>{ev}</ul>" if ev else "")
            + (f"<p><b>Caveats.</b></p><ul>{cav}</ul>" if cav else "")
            + (f"<p><em>Debate:</em> {e(j.get('debate_takeaway'))}</p>" if j.get("debate_takeaway") else "")
            + f'<p class="meta">Based on {e(deb.get("text_kind", "abstract only"))}</p>')
        turns = deb.get("transcript") or {}
        for key in ("advocate_opening", "skeptic_opening", "advocate_rebuttal", "skeptic_rebuttal"):
            if turns.get(key):
                parts.append(f"<details><summary>{e(key.replace('_', ' ').capitalize())}</summary>"
                             f'<p class="pre">{e(turns[key])}</p></details>')
    parts.append(f"<h2>Abstract</h2><p>{e(row['abstract']) or '<em>(no abstract)</em>'}</p>")
    return row["title"], nav("") + "\n".join(parts)
