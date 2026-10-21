"""Friends: invite-only accounts that get a Jev-only feed for their own topics.

Each friend has topics, arXiv categories and search phrases (table `users`). Their Jev
question set is built from those (a 0-4 relevance ladder written around their topics, plus
one Noul per topic), so no Claude calls are ever made for friends. Scores and labels live in
`user_scores` / `user_labels`, separate from Roz's own `papers.jev_*` columns and `labels`.

Harvesting: friends' categories and phrases are fetched by separate source instances named
`arxiv_rss_friends` / `openalex_friends`. A paper seen only by those sources is left out of
Roz's triage (see FRIEND_SOURCES), so friends' fields never reach her bank or Jev bill.

A friend's candidates are papers whose arXiv categories overlap theirs or whose title/abstract
contains one of their phrases, first seen no earlier than `backfill_days` before they joined.

Training: `next_training_paper` draws evenly across score buckets (like `paperbot label`);
once there are enough labels `recalibrate` picks their threshold by F2.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .calibrate import suggest, sweep
from .models import FRIEND_SOURCES, Paper, is_friend_only  # noqa: F401 (re-exported)
from .store import Store, now_iso
from .triage.jev import JevClient, JevError, JevFatalError
from .triage.questions import FACET_PREFIX, RELEVANCE_KEY, build_questions, build_state

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 2.5
MAX_SCORE = 4
MAX_TOPICS, MAX_PHRASES, MAX_CATEGORIES = 8, 5, 12
TRAINING_TARGET = 40
MIN_LABELS, MIN_POSITIVES = 20, 3
JEV_USD_PER_M_INPUT = 0.042

# A curated picker; arXiv has ~150 categories. Codes are validated against this list.
ARXIV_CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "Computer science": [
        ("cs.AI", "Artificial intelligence"), ("cs.LG", "Machine learning"), ("cs.CL", "Computation and language (NLP)"),
        ("cs.CV", "Computer vision"), ("cs.RO", "Robotics"), ("cs.MA", "Multiagent systems"),
        ("cs.HC", "Human-computer interaction"), ("cs.CR", "Cryptography and security"),
        ("cs.SE", "Software engineering"), ("cs.DB", "Databases"), ("cs.DC", "Distributed computing"),
        ("cs.IR", "Information retrieval"), ("cs.NE", "Neural and evolutionary computing"),
        ("cs.CY", "Computers and society"), ("cs.GT", "Game theory"), ("cs.DS", "Data structures and algorithms"),
    ],
    "Statistics and math": [
        ("stat.ML", "Statistics: machine learning"), ("stat.ME", "Statistics: methodology"),
        ("stat.AP", "Statistics: applications"), ("math.OC", "Optimization and control"),
        ("math.PR", "Probability"), ("math.NA", "Numerical analysis"), ("math.DS", "Dynamical systems"),
    ],
    "Engineering and economics": [
        ("eess.SY", "Systems and control"), ("eess.SP", "Signal processing"), ("eess.IV", "Image and video processing"),
        ("eess.AS", "Audio and speech"), ("econ.EM", "Econometrics"), ("econ.GN", "General economics"),
        ("q-fin.ST", "Statistical finance"), ("q-fin.CP", "Computational finance"),
    ],
    "Physics": [
        ("physics.comp-ph", "Computational physics"), ("physics.data-an", "Data analysis and statistics"),
        ("physics.app-ph", "Applied physics"), ("physics.flu-dyn", "Fluid dynamics"),
        ("cond-mat.mtrl-sci", "Materials science"), ("cond-mat.stat-mech", "Statistical mechanics"),
        ("quant-ph", "Quantum physics"), ("astro-ph.IM", "Astronomy: instrumentation and methods"),
        ("physics.med-ph", "Medical physics"), ("physics.soc-ph", "Physics and society"),
    ],
    "Biology": [
        ("q-bio.QM", "Quantitative methods"), ("q-bio.NC", "Neurons and cognition"), ("q-bio.GN", "Genomics"),
        ("q-bio.BM", "Biomolecules"), ("q-bio.PE", "Populations and evolution"),
    ],
}
CATEGORY_CODES = {code for group in ARXIV_CATEGORIES.values() for code, _ in group}


class FriendError(ValueError):
    """Invalid input from a friend's form; the message is shown to them."""


@dataclass
class Friend:
    id: int
    name: str
    token: str
    interests: str
    topics: list[str]
    categories: list[str]
    phrases: list[str]
    threshold: float | None
    created_at: str
    setup_at: str | None
    disabled: bool

    @property
    def ready(self) -> bool:
        return bool(self.setup_at) and bool(self.categories or self.phrases)

    @property
    def effective_threshold(self) -> float:
        return self.threshold if self.threshold is not None else DEFAULT_THRESHOLD


def _friend(row) -> Friend:
    return Friend(id=row["id"], name=row["name"], token=row["token"], interests=row["interests"] or "",
                  topics=json.loads(row["topics"]), categories=json.loads(row["arxiv_categories"]),
                  phrases=json.loads(row["search_phrases"]), threshold=row["threshold"],
                  created_at=row["created_at"], setup_at=row["setup_at"], disabled=bool(row["disabled"]))


# ---- accounts -----------------------------------------------------------------------------
def create_friend(store: Store, name: str) -> Friend:
    name = " ".join(name.split())[:60]
    if not name:
        raise FriendError("name is required")
    token = secrets.token_urlsafe(24)
    with store.tx() as c:
        c.execute("INSERT INTO users(name, token, created_at) VALUES(?,?,?)", (name, token, now_iso()))
    return get_by_token(store, token)  # type: ignore[return-value]


def get_by_token(store: Store, token: str) -> Friend | None:
    if not token:
        return None
    row = store.conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    return _friend(row) if row else None


def get_friend(store: Store, user_id: int) -> Friend | None:
    row = store.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _friend(row) if row else None


def list_friends(store: Store) -> list[Friend]:
    return [_friend(r) for r in store.conn.execute("SELECT * FROM users ORDER BY id")]


def set_disabled(store: Store, user_id: int, disabled: bool) -> None:
    with store.tx() as c:
        c.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))


def touch(store: Store, user_id: int) -> None:
    with store.tx() as c:
        c.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now_iso(), user_id))


def _lines(text: str, limit: int, max_len: int, what: str) -> list[str]:
    items, seen = [], set()
    for raw in (text or "").splitlines():
        item = " ".join(raw.split())
        if item and item.lower() not in seen:
            if len(item) > max_len:
                raise FriendError(f"each {what} must be at most {max_len} characters")
            items.append(item)
            seen.add(item.lower())
    if len(items) > limit:
        raise FriendError(f"at most {limit} {what}s")
    return items


def save_profile(store: Store, friend: Friend, interests: str, topics_text: str,
                 categories: list[str], phrases_text: str) -> bool:
    """Validate and store a friend's interests. Returns True if anything that affects scoring
    changed, in which case their old scores are dropped (labels are kept) and get rescored."""
    interests = " ".join((interests or "").split())
    if not 10 <= len(interests) <= 600:
        raise FriendError("describe what you want papers about in 10–600 characters")
    topics = _lines(topics_text, MAX_TOPICS, 80, "topic")
    if not topics:
        raise FriendError("add at least one topic")
    cats = sorted({c for c in categories if c in CATEGORY_CODES})
    if len(cats) > MAX_CATEGORIES:
        raise FriendError(f"pick at most {MAX_CATEGORIES} arXiv categories")
    # OpenAlex filter syntax uses , : | as separators, so they can't appear inside a phrase.
    phrases = [re.sub(r"[,:|]", " ", p).strip() for p in _lines(phrases_text, MAX_PHRASES, 80, "search phrase")]
    phrases = [" ".join(p.split()) for p in phrases if len(p) >= 3]
    if not cats and not phrases:
        raise FriendError("pick at least one arXiv category or add a search phrase")
    changed = (interests, topics, cats, phrases) != (friend.interests, friend.topics, friend.categories, friend.phrases)
    with store.tx() as c:
        c.execute("""UPDATE users SET interests=?, topics=?, arxiv_categories=?, search_phrases=?,
                     setup_at=COALESCE(setup_at, ?) WHERE id=?""",
                  (interests, json.dumps(topics), json.dumps(cats), json.dumps(phrases), now_iso(), friend.id))
        if changed:
            c.execute("DELETE FROM user_scores WHERE user_id = ?", (friend.id,))
    if changed:
        recalibrate(store, friend.id)
    return changed


# ---- Jev questions ---------------------------------------------------------------------------
def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")[:40] or "topic"


def friend_profile(friend: Friend) -> dict[str, Any]:
    """A profile.yaml-shaped dict for build_questions: a relevance ladder about their topics
    and one Noul facet per topic."""
    topics = "; ".join(friend.topics)
    levels = [
        f"Unrelated. Nothing to do with these interests: {friend.interests}",
        "Peripheral. Same broad field, but about different problems than the reader's topics.",
        "Adjacent. Methods, data or results that a reader interested in these topics might find useful, "
        "though the paper is not about the topics themselves.",
        f"Related. Closely related to at least one of these topics, or directly transferable to them: {topics}",
        f"Core. Directly about at least one of these topics: {topics}",
    ]
    facets, used = {}, set()
    for t in friend.topics:
        key = _slug(t)
        while key in used:
            key += "_"
        used.add(key)
        facets[key] = {"instructions": f"The paper is about this topic: {t}"}
    return {"relevance": {"instructions": f"How relevant is this paper to a reader who wants papers about: "
                                          f"{friend.interests}", "levels": levels},
            "facets": facets}


def facet_labels(friend: Friend) -> dict[str, str]:
    """Answer key -> the friend's own topic wording."""
    out, used = {}, set()
    for t in friend.topics:
        key = _slug(t)
        while key in used:
            key += "_"
        used.add(key)
        out[FACET_PREFIX + key] = t
    return out


# ---- candidates and scoring -------------------------------------------------------------------
def candidates(store: Store, friend: Friend, limit: int, backfill_days: int = 7) -> list:
    """Unscored papers matching the friend's categories or phrases, newest first."""
    if not friend.ready:
        return []
    since = (datetime.fromisoformat(friend.created_at) - timedelta(days=backfill_days)).isoformat()
    match, args = [], []
    if friend.categories:
        match.append(f"EXISTS (SELECT 1 FROM json_each(p.categories) WHERE value IN "
                     f"({','.join('?' * len(friend.categories))}))")
        args += friend.categories
    for phrase in friend.phrases:
        match.append("(p.title LIKE ? OR p.abstract LIKE ?)")
        args += [f"%{phrase}%"] * 2
    sql = (f"""SELECT p.* FROM papers p WHERE p.first_seen >= ? AND p.status != 'spam' AND ({' OR '.join(match)})
               AND NOT EXISTS (SELECT 1 FROM user_scores s WHERE s.user_id = ? AND s.paper_id = p.id)
               ORDER BY COALESCE(p.published, p.first_seen) DESC LIMIT ?""")
    return list(store.conn.execute(sql, [since, *args, friend.id, limit]))


@dataclass
class ScoreStats:
    scored: int = 0
    errors: int = 0
    input_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * JEV_USD_PER_M_INPUT / 1e6


def score_friend(store: Store, jev: JevClient, friend: Friend, limit: int, concurrency: int = 8,
                 progress=None) -> ScoreStats:
    """Score up to `limit` candidates for one friend. Failed papers are simply retried next time."""
    rows = candidates(store, friend, limit)
    stats = ScoreStats()
    if not rows:
        return stats
    questions = build_questions(friend_profile(friend))
    fatal: JevFatalError | None = None
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(jev.evaluate, build_state(r["title"], r["abstract"], r["venue"]), questions): r["id"]
                   for r in rows}
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                resp = fut.result()
            except JevFatalError as e:
                fatal = fatal or e
                continue
            except JevError as e:
                log.warning("friend %s: Jev failed on %s: %s", friend.id, pid, e)
                stats.errors += 1
                continue
            ans = resp["answers"]
            rel = ans[RELEVANCE_KEY]
            with store.tx() as c:
                c.execute("""INSERT OR REPLACE INTO user_scores(user_id, paper_id, score, confidence, answers,
                             model, scored_at) VALUES(?,?,?,?,?,?,?)""",
                          (friend.id, pid, float(rel["score"]), rel.get("confidence"), json.dumps(ans),
                           resp.get("model"), now_iso()))
            stats.scored += 1
            stats.input_tokens += (resp.get("usage") or {}).get("input_tokens", 0)
            if progress:
                progress(stats)
    if stats.scored or stats.errors:
        with store.tx() as c:
            c.execute("""INSERT INTO user_usage(user_id, day, papers, input_tokens) VALUES(?,?,?,?)
                         ON CONFLICT(user_id, day) DO UPDATE SET papers = papers + excluded.papers,
                         input_tokens = input_tokens + excluded.input_tokens""",
                      (friend.id, now_iso()[:10], stats.scored, stats.input_tokens))
        store.log_run(f"friends:{friend.id}", fetched=len(rows), new=stats.scored, errors=stats.errors,
                      note=f"tokens={stats.input_tokens}")
    if fatal:
        raise fatal
    return stats


def run_friends(store: Store, jev: JevClient, per_friend: int, concurrency: int = 8) -> dict[str, ScoreStats]:
    """The `friends` stage of `paperbot run`: score new candidates for every active friend."""
    out = {}
    for f in list_friends(store):
        if f.disabled or not f.ready:
            continue
        out[f.name] = score_friend(store, jev, f, per_friend, concurrency)
    return out


def usage(store: Store, user_id: int) -> tuple[int, int]:
    row = store.conn.execute("SELECT COALESCE(SUM(papers),0), COALESCE(SUM(input_tokens),0) FROM user_usage "
                             "WHERE user_id = ?", (user_id,)).fetchone()
    return row[0], row[1]


# ---- harvesting for friends -------------------------------------------------------------------
def harvest_extras(store: Store, own_categories: list[str], own_queries: list[str]) -> tuple[list[str], list[str]]:
    """Categories and phrases friends want that Roz's own profile doesn't already fetch."""
    cats, phrases = set(), set()
    for f in list_friends(store):
        if f.disabled or not f.ready:
            continue
        cats.update(f.categories)
        phrases.update(f.phrases)
    own_q = {q.lower() for q in own_queries}
    return sorted(cats - set(own_categories)), sorted(p for p in phrases if p.lower() not in own_q)


class Relabeled:
    """Wrap a source so its papers are recorded under another source name."""

    def __init__(self, inner, name: str):
        self.inner, self.name = inner, name

    def fetch(self, since=None) -> Iterator[Paper]:
        for p in self.inner.fetch(since):
            p.source = self.name
            yield p


# ---- training and calibration -------------------------------------------------------------
def label(store: Store, user_id: int, paper_id: str, relevant: bool, source: str) -> None:
    with store.tx() as c:
        c.execute("""INSERT INTO user_labels(user_id, paper_id, relevant, at, source) VALUES(?,?,?,?,?)
                     ON CONFLICT(user_id, paper_id) DO UPDATE SET relevant=excluded.relevant, at=excluded.at,
                     source=excluded.source""", (user_id, paper_id, int(relevant), now_iso(), source))
    recalibrate(store, user_id)


def label_counts(store: Store, user_id: int) -> tuple[int, int]:
    row = store.conn.execute("SELECT COUNT(*), COALESCE(SUM(relevant),0) FROM user_labels WHERE user_id = ?",
                             (user_id,)).fetchone()
    return row[0], row[1]


def next_training_paper(store: Store, user_id: int, rng=None):
    """An unlabeled scored paper from the score bucket with the fewest labels so far, so the
    training set covers the whole range (the highest buckets are rare but matter most)."""
    import random
    rng = rng or random.Random()
    rows = list(store.conn.execute(
        """SELECT p.*, s.score FROM user_scores s JOIN papers p ON p.id = s.paper_id
           WHERE s.user_id = ? AND p.status != 'spam'
             AND s.paper_id NOT IN (SELECT paper_id FROM user_labels WHERE user_id = ?)""",
        (user_id, user_id)))
    if not rows:
        return None
    labeled = [r[0] for r in store.conn.execute(
        "SELECT s.score FROM user_labels l JOIN user_scores s ON s.user_id = l.user_id AND s.paper_id = l.paper_id "
        "WHERE l.user_id = ?", (user_id,))]
    have = {b: 0 for b in range(MAX_SCORE + 1)}
    for s in labeled:
        have[min(int(s), MAX_SCORE)] += 1
    by_bucket: dict[int, list] = {}
    for r in rows:
        by_bucket.setdefault(min(int(r["score"]), MAX_SCORE), []).append(r)
    bucket = min(by_bucket, key=lambda b: (have[b], -b))
    return rng.choice(by_bucket[bucket])


def recalibrate(store: Store, user_id: int) -> float | None:
    """Set the friend's threshold from their labels by F2 (recall-weighted; ties go to the higher
    cutoff). No pass-rate budget: a friend's feed costs nothing extra to show, and their sources
    are hand-picked, so a mostly-on-topic stream should mostly pass. Leaves it unset (default
    2.5) until there are enough labels."""
    labeled = [(r[0], r[1]) for r in store.conn.execute(
        "SELECT s.score, l.relevant FROM user_labels l JOIN user_scores s "
        "ON s.user_id = l.user_id AND s.paper_id = l.paper_id WHERE l.user_id = ?", (user_id,))]
    pos = sum(y for _, y in labeled)
    threshold = None
    if len(labeled) >= MIN_LABELS and pos >= MIN_POSITIVES:
        population = [r[0] for r in store.conn.execute("SELECT score FROM user_scores WHERE user_id = ?", (user_id,))]
        rows = sweep(labeled, beta=2.0, max_score=MAX_SCORE, population=population)
        threshold = suggest(rows).threshold
    with store.tx() as c:
        c.execute("UPDATE users SET threshold = ? WHERE id = ?", (threshold, user_id))
    return threshold


def onboard(cfg, user_id: int, report, score_limit: int = 600, backfill_days: int = 7,
            pages_per_category: int = 3) -> None:
    """After a friend saves their topics: fetch the last week for categories Roz doesn't already
    harvest (arXiv query API) and for their phrases (OpenAlex), then score with Jev so they have
    papers to train on right away. Runs in a background thread with its own DB connection."""
    from .config import Config
    from .harvest import make_client
    from .harvest.arxiv import ArxivAPI
    from .harvest.openalex import OpenAlex
    from .http import RateLimiter
    from .spam import SpamRules, check_new
    from .triage import make_jev_client

    store = Store(cfg.db_path)
    try:
        friend = get_friend(store, user_id)
        if friend is None or not friend.ready:
            return
        own = cfg.profile.get("sources", {}) or {}
        new_cats = [c for c in friend.categories if c not in (own.get("arxiv_categories") or [])]
        since = datetime.now(timezone.utc) - timedelta(days=backfill_days)
        rules = SpamRules.from_settings(cfg.section("spam"))
        added = 0
        with make_client(cfg) as client:
            sources = []
            if new_cats:
                ax = cfg.section("harvest").get("arxiv", {})
                sources.append(Relabeled(ArxivAPI(client, RateLimiter(ax.get("min_interval_s", 3.0)), new_cats,
                                                  max_pages=pages_per_category), "arxiv_api_friends"))
            if friend.phrases:
                sources.append(Relabeled(OpenAlex(client, RateLimiter(0.2), friend.phrases,
                                                  api_key=Config.env("OPENALEX_API_KEY"), max_pages_per_query=1),
                                         "openalex_friends"))
            for src in sources:
                report("Fetching last week's papers for your sources (arXiv allows one request every 3 s, "
                       "so this can take a few minutes)...")
                try:
                    for p in src.fetch(since):
                        if p.title:
                            pid, is_new = store.upsert(p)
                            added += is_new
                            if is_new:
                                check_new(store, pid, p, rules)
                except Exception as e:  # one failing source shouldn't block scoring the rest
                    log.warning("onboarding fetch %s for friend %s failed: %s", src.name, user_id, e)
        report(f"Fetched {added} new papers. Scoring with Jev...")
        stats = score_friend(store, make_jev_client(cfg), friend, score_limit,
                             progress=lambda s: report(f"Scoring with Jev... {s.scored} done"))
        total = store.conn.execute("SELECT COUNT(*) FROM user_scores WHERE user_id = ?", (user_id,)).fetchone()[0]
        report(f"Ready: {total} papers scored for you ({stats.scored} just now).")
    finally:
        store.close()


# ---- background jobs (started from the website) ------------------------------------------------
class Jobs:
    """At most one background job per friend; status text is shown on their page."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status: dict[int, str] = {}
        self._running: set[int] = set()

    def running(self, user_id: int) -> bool:
        with self._lock:
            return user_id in self._running

    def start(self, user_id: int, fn) -> bool:
        with self._lock:
            if user_id in self._running:
                return False
            self._running.add(user_id)

        def run():
            try:
                fn(lambda msg: self.status.__setitem__(user_id, msg))
            except Exception as e:  # shown to the friend; details in the log
                log.exception("friend job %s failed", user_id)
                self.status[user_id] = f"Something went wrong ({type(e).__name__}); Roz can check the log."
            finally:
                with self._lock:
                    self._running.discard(user_id)

        threading.Thread(target=run, daemon=True, name=f"friend-{user_id}").start()
        return True
