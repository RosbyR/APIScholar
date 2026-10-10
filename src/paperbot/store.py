"""SQLite store: papers, per-source watermarks, run log, and human feedback.

Dedup order when a paper arrives: arXiv ID -> DOI -> normalized title hash.
A match merges the new record into the existing row (fills missing IDs, keeps the
longer abstract, records the extra source) instead of inserting a duplicate.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import STATUS_NEW, Paper

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id            TEXT PRIMARY KEY,          -- canonical id at first insert, never changes
    arxiv_id      TEXT,
    doi           TEXT,
    openalex_id   TEXT,
    title_key     TEXT NOT NULL,
    title         TEXT NOT NULL,
    abstract      TEXT NOT NULL,
    authors       TEXT NOT NULL DEFAULT '[]', -- JSON list
    venue         TEXT,
    published     TEXT,
    url           TEXT,
    pdf_url       TEXT,
    categories    TEXT NOT NULL DEFAULT '[]', -- JSON list
    sources       TEXT NOT NULL DEFAULT '[]', -- JSON list of adapter names that saw it
    first_seen    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',
    jev_score     REAL,                       -- relevance Score (0..n-1)
    jev_confidence REAL,
    jev_answers   TEXT,                       -- full Jev answers JSON
    jev_model     TEXT,
    triaged_at    TEXT,
    debate        TEXT,                       -- transcript + judge JSON
    debated_at    TEXT,
    digest_date   TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title_key);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);

CREATE TABLE IF NOT EXISTS source_state (
    source     TEXT PRIMARY KEY,
    watermark  TEXT NOT NULL                  -- ISO timestamp of last successful harvest
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started    TEXT NOT NULL,
    stage      TEXT NOT NULL,                 -- harvest:<source> | triage | debate | digest
    fetched    INTEGER NOT NULL DEFAULT 0,
    new        INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS labels (           -- hand labels for threshold calibration
    paper_id   TEXT PRIMARY KEY REFERENCES papers(id),
    relevant   INTEGER NOT NULL,              -- 1 relevant, 0 not
    labeled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (         -- thumbs on digest entries
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id   TEXT NOT NULL REFERENCES papers(id),
    rating     INTEGER NOT NULL,              -- +1 / -1
    note       TEXT,
    at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (        -- one row per Claude request, for cost tracking
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    paper_id      TEXT NOT NULL,
    role          TEXT NOT NULL,              -- advocate | skeptic | judge
    model         TEXT NOT NULL,              -- model that served the request (after any fallback)
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL                        -- NULL if the model had no price in settings
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_at ON llm_calls(at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- papers -------------------------------------------------------------
    def find_existing(self, p: Paper) -> sqlite3.Row | None:
        c = self.conn
        if p.arxiv_id:
            row = c.execute("SELECT * FROM papers WHERE arxiv_id=?", (p.arxiv_id,)).fetchone()
            if row:
                return row
        if p.doi:
            row = c.execute("SELECT * FROM papers WHERE doi=?", (p.doi,)).fetchone()
            if row:
                return row
        return c.execute("SELECT * FROM papers WHERE title_key=?", (p.title_key,)).fetchone()

    def upsert(self, p: Paper) -> tuple[str, bool]:
        """Insert or merge. Returns (paper_id, is_new)."""
        existing = self.find_existing(p)
        with self.tx() as c:
            if existing is None:
                pid = p.canonical_id
                c.execute(
                    """INSERT INTO papers (id, arxiv_id, doi, openalex_id, title_key, title, abstract,
                       authors, venue, published, url, pdf_url, categories, sources, first_seen, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, p.arxiv_id, p.doi, p.openalex_id, p.title_key, p.title, p.abstract,
                     json.dumps(p.authors), p.venue, p.published, p.url, p.pdf_url,
                     json.dumps(p.categories), json.dumps([p.source]), now_iso(), STATUS_NEW),
                )
                return pid, True

            sources = json.loads(existing["sources"])
            if p.source not in sources:
                sources.append(p.source)
            cats = sorted(set(json.loads(existing["categories"])) | set(p.categories))
            abstract = existing["abstract"]
            if len(p.abstract) > len(abstract or ""):
                abstract = p.abstract
            c.execute(
                """UPDATE papers SET
                     arxiv_id    = COALESCE(arxiv_id, ?),
                     doi         = COALESCE(doi, ?),
                     openalex_id = COALESCE(openalex_id, ?),
                     venue       = COALESCE(venue, ?),
                     published   = COALESCE(published, ?),
                     pdf_url     = COALESCE(pdf_url, ?),
                     abstract    = ?,
                     categories  = ?,
                     sources     = ?
                   WHERE id = ?""",
                (p.arxiv_id, p.doi, p.openalex_id, p.venue, p.published, p.pdf_url,
                 abstract, json.dumps(cats), json.dumps(sources), existing["id"]),
            )
            return existing["id"], False

    def get(self, paper_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()

    def by_status(self, *statuses: str, order: str = "first_seen", limit: int | None = None) -> list[sqlite3.Row]:
        q = f"SELECT * FROM papers WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY {order}"
        if limit:
            q += f" LIMIT {int(limit)}"
        return list(self.conn.execute(q, statuses))

    def update(self, paper_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        with self.tx() as c:
            c.execute(f"UPDATE papers SET {cols} WHERE id=?", (*vals, paper_id))

    def status_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM papers GROUP BY status")}

    # ---- watermarks / runs -----------------------------------------------------
    def get_watermark(self, source: str) -> str | None:
        row = self.conn.execute("SELECT watermark FROM source_state WHERE source=?", (source,)).fetchone()
        return row[0] if row else None

    def set_watermark(self, source: str, ts: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO source_state(source, watermark) VALUES(?,?) "
                "ON CONFLICT(source) DO UPDATE SET watermark=excluded.watermark",
                (source, ts),
            )

    def log_run(self, stage: str, fetched: int = 0, new: int = 0, errors: int = 0, note: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(started, stage, fetched, new, errors, note) VALUES(?,?,?,?,?,?)",
                (now_iso(), stage, fetched, new, errors, note),
            )

    def runs_since(self, ts: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM runs WHERE started >= ? ORDER BY id", (ts,)))

    # ---- LLM cost log ------------------------------------------------------------
    def log_llm_call(self, paper_id: str, call: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO llm_calls(at, paper_id, role, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd) VALUES(?,?,?,?,?,?,?,?,?)""",
                (now_iso(), paper_id, call["role"], call["model"], call["input_tokens"], call["output_tokens"],
                 call.get("cache_read_input_tokens", 0), call.get("cache_creation_input_tokens", 0),
                 call.get("cost_usd")),
            )

    def llm_cost_since(self, ts: str) -> sqlite3.Row:
        """Totals since `ts`: calls, papers, tokens, cost (cost sums priced calls only)."""
        return self.conn.execute(
            """SELECT COUNT(*) AS calls, COUNT(DISTINCT paper_id) AS papers,
                      COALESCE(SUM(input_tokens + cache_read_tokens + cache_write_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd,
                      SUM(cost_usd IS NULL) AS unpriced
               FROM llm_calls WHERE at >= ?""", (ts,)).fetchone()

    def llm_cost_by_day(self, ts: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT substr(at, 1, 10) AS day, model, COUNT(*) AS calls, COUNT(DISTINCT paper_id) AS papers,
                      SUM(input_tokens + cache_read_tokens + cache_write_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens, COALESCE(SUM(cost_usd), 0) AS cost_usd
               FROM llm_calls WHERE at >= ? GROUP BY day, model ORDER BY day, model""", (ts,)))

    # ---- labels / feedback -----------------------------------------------------
    def add_label(self, paper_id: str, relevant: bool) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO labels(paper_id, relevant, labeled_at) VALUES(?,?,?) "
                "ON CONFLICT(paper_id) DO UPDATE SET relevant=excluded.relevant, labeled_at=excluded.labeled_at",
                (paper_id, int(relevant), now_iso()),
            )

    def labeled_scores(self) -> list[tuple[float, int]]:
        q = """SELECT p.jev_score, l.relevant FROM labels l JOIN papers p ON p.id = l.paper_id
               WHERE p.jev_score IS NOT NULL"""
        return [(r[0], r[1]) for r in self.conn.execute(q)]

    def unlabeled_scored(self) -> list[sqlite3.Row]:
        q = """SELECT * FROM papers WHERE jev_score IS NOT NULL
               AND id NOT IN (SELECT paper_id FROM labels)"""
        return list(self.conn.execute(q))

    def add_feedback(self, paper_id: str, rating: int, note: str | None = None) -> None:
        if self.get(paper_id) is None:
            raise KeyError(f"No paper with id {paper_id!r}")
        with self.tx() as c:
            c.execute(
                "INSERT INTO feedback(paper_id, rating, note, at) VALUES(?,?,?,?)",
                (paper_id, 1 if rating > 0 else -1, note, now_iso()),
            )
        # A thumb on a digested paper is also a calibration label.
        self.add_label(paper_id, rating > 0)
