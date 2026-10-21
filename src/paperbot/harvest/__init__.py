"""Harvest stage: run each source adapter, normalize, dedup into the store.

Each source is isolated: one failing source is logged and skipped, and its watermark
is NOT advanced, so the next run re-covers the gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Protocol

import httpx

from ..config import Config
from ..http import RateLimiter, user_agent
from ..models import Paper
from ..store import Store
from .arxiv import ArxivAPI, ArxivRSS
from .openalex import OpenAlex

log = logging.getLogger(__name__)


class Source(Protocol):
    name: str

    def fetch(self, since: datetime | None = None) -> Iterator[Paper]: ...


@dataclass
class HarvestStats:
    per_source: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_new(self) -> int:
        return sum(s["new"] for s in self.per_source.values())


def make_client(cfg: Config) -> httpx.Client:
    # Env var first so the address can live in .env instead of the committed settings file.
    contact = Config.env("PAPERBOT_CONTACT_EMAIL") or cfg.settings.get("contact_email")
    return httpx.Client(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": user_agent(contact)},
        follow_redirects=True,
    )


def build_sources(cfg: Config, client: httpx.Client, store: Store | None = None) -> list[Source]:
    """Roz's sources from profile.yaml, plus (with `store`) separate `*_friends` sources for the
    arXiv categories and search phrases friends asked for that her profile doesn't cover."""
    from ..friends import Relabeled, harvest_extras
    h = cfg.section("harvest")
    src_profile = cfg.profile.get("sources", {})
    sources: list[Source] = []
    ax = h.get("arxiv", {})
    oa = h.get("openalex", {})
    arxiv_limiter = RateLimiter(ax.get("min_interval_s", 3.0))  # shared: arXiv allows 1 request / 3 s

    def openalex(queries):
        return OpenAlex(client, RateLimiter(oa.get("min_interval_s", 0.2)), queries,
                        api_key=Config.env("OPENALEX_API_KEY"),
                        per_page=oa.get("per_page", 100), max_pages_per_query=oa.get("max_pages_per_query", 3))

    if ax.get("enabled", True) and src_profile.get("arxiv_categories"):
        sources.append(ArxivRSS(client, arxiv_limiter, src_profile["arxiv_categories"], ax.get("announce_types")))
    if oa.get("enabled", True) and src_profile.get("openalex_queries"):
        sources.append(openalex(src_profile["openalex_queries"]))
    if store is not None:
        cats, phrases = harvest_extras(store, src_profile.get("arxiv_categories") or [],
                                       src_profile.get("openalex_queries") or [])
        if cats and ax.get("enabled", True):
            sources.append(Relabeled(ArxivRSS(client, arxiv_limiter, cats, ax.get("announce_types")),
                                     "arxiv_rss_friends"))
        if phrases and oa.get("enabled", True):
            sources.append(Relabeled(openalex(phrases), "openalex_friends"))
    return sources


def _since(store: Store, source: str, overlap_days: int, first_run_days: int, now: datetime) -> datetime:
    wm = store.get_watermark(source)
    if wm:
        return datetime.fromisoformat(wm) - timedelta(days=overlap_days)
    return now - timedelta(days=first_run_days)


def run_harvest(cfg: Config, store: Store, sources: list[Source], now: datetime | None = None) -> HarvestStats:
    now = now or datetime.now(timezone.utc)
    h = cfg.section("harvest")
    overlap, first = h.get("lookback_overlap_days", 2), h.get("first_run_days", 7)
    stats = HarvestStats()
    for src in sources:
        fetched = new = 0
        try:
            since = _since(store, src.name, overlap, first, now)
            for paper in src.fetch(since):
                fetched += 1
                if not paper.title:
                    continue
                _, is_new = store.upsert(paper)
                new += is_new
        except Exception as e:  # isolate sources; do not advance watermark
            log.exception("harvest source %s failed", src.name)
            stats.per_source[src.name] = {"fetched": fetched, "new": new, "errors": 1}
            store.log_run(f"harvest:{src.name}", fetched, new, 1, note=repr(e)[:500])
            continue
        store.set_watermark(src.name, now.replace(microsecond=0).isoformat())
        stats.per_source[src.name] = {"fetched": fetched, "new": new, "errors": 0}
        store.log_run(f"harvest:{src.name}", fetched, new, 0)
        log.info("harvest %s: fetched=%d new=%d", src.name, fetched, new)
    return stats


def backfill_arxiv(cfg: Config, store: Store, client: httpx.Client, days: int) -> HarvestStats:
    """One-off: pull the last N days per category from the arXiv query API."""
    ax = cfg.section("harvest").get("arxiv", {})
    src = ArxivAPI(client, RateLimiter(ax.get("min_interval_s", 3.0)),
                   cfg.profile.get("sources", {}).get("arxiv_categories", []))
    now = datetime.now(timezone.utc)
    stats = HarvestStats()
    fetched = new = 0
    for paper in src.fetch(now - timedelta(days=days)):
        fetched += 1
        _, is_new = store.upsert(paper)
        new += is_new
    stats.per_source[src.name] = {"fetched": fetched, "new": new, "errors": 0}
    store.log_run("harvest:arxiv_api_backfill", fetched, new, 0)
    return stats
