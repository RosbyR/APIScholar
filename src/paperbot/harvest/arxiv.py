"""arXiv adapters.

- ArxivRSS: the daily firehose. One request per category, no date parameter; the feed
  is "today's announcements". Empty on weekends/holidays (feed declares skipDays).
- ArxivAPI: the query API, used for backfill (`paperbot backfill --days N`).

arXiv ToU: legacy APIs (RSS, OAI-PMH, query API) <= 1 request every 3 s, one connection.
Both adapters share one RateLimiter so they can never exceed that together.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterator

import feedparser
import httpx

from ..http import RateLimiter, get_with_retry
from ..models import Paper, normalize_arxiv_id

log = logging.getLogger(__name__)

RSS_BASE = "https://rss.arxiv.org/rss/"
API_BASE = "https://export.arxiv.org/api/query"   # note: export. host; arxiv.org/api redirects

_ABSTRACT_RE = re.compile(r"Abstract:\s*(.*)", re.S)
_ANNOUNCE_RE = re.compile(r"Announce Type:\s*([\w-]+)")


def _iso_date(struct_time) -> str | None:
    if not struct_time:
        return None
    return datetime(*struct_time[:6], tzinfo=timezone.utc).date().isoformat()


def parse_rss(xml: str | bytes, keep_types: set[str] | None = None) -> list[Paper]:
    """Parse an arXiv RSS document into Papers.

    Item description looks like: "arXiv:2410.01234v1 Announce Type: new \\nAbstract: ...".
    The announce type is also exposed as <arxiv:announce_type>.
    """
    feed = feedparser.parse(xml)
    out: list[Paper] = []
    for e in feed.entries:
        desc = e.get("summary") or e.get("description") or ""
        announce = e.get("arxiv_announce_type")
        if not announce:
            m = _ANNOUNCE_RE.search(desc)
            announce = m.group(1) if m else "new"
        if keep_types and announce not in keep_types:
            continue
        m = _ABSTRACT_RE.search(desc)
        abstract = m.group(1) if m else desc
        arxiv_id = normalize_arxiv_id(e.get("id") or e.get("guid") or e.get("link"))
        if not arxiv_id:
            log.warning("arXiv RSS item without parseable id: %r", e.get("title"))
            continue
        authors_raw = e.get("author") or ""
        authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
        cats = [t.get("term") for t in e.get("tags", []) if t.get("term")]
        out.append(Paper(
            title=e.get("title", ""),
            abstract=abstract,
            source="arxiv_rss",
            arxiv_id=arxiv_id,
            authors=authors,
            venue="arXiv",
            published=_iso_date(e.get("published_parsed")),
            categories=cats,
        ))
    return out


def parse_api(xml: str | bytes) -> list[Paper]:
    """Parse an arXiv query-API Atom document."""
    feed = feedparser.parse(xml)
    out: list[Paper] = []
    for e in feed.entries:
        arxiv_id = normalize_arxiv_id(e.get("id"))
        if not arxiv_id:
            continue
        pdf = next((l.get("href") for l in e.get("links", []) if l.get("type") == "application/pdf"), None)
        out.append(Paper(
            title=e.get("title", ""),
            abstract=e.get("summary", ""),
            source="arxiv_api",
            arxiv_id=arxiv_id,
            doi=e.get("arxiv_doi"),
            authors=[a.get("name") for a in e.get("authors", []) if a.get("name")],
            venue=e.get("arxiv_journal_ref") or "arXiv",
            published=_iso_date(e.get("published_parsed")),
            pdf_url=pdf,
            categories=[t.get("term") for t in e.get("tags", []) if t.get("term")],
        ))
    return out


class ArxivRSS:
    name = "arxiv_rss"

    def __init__(self, client: httpx.Client, limiter: RateLimiter, categories: list[str],
                 announce_types: list[str] | None = None):
        self.client, self.limiter = client, limiter
        self.categories = categories
        self.keep = set(announce_types or ["new", "cross"])

    def fetch(self, since: datetime | None = None) -> Iterator[Paper]:  # since unused: feed is daily
        for cat in self.categories:
            resp = get_with_retry(self.client, RSS_BASE + cat, limiter=self.limiter)
            papers = parse_rss(resp.content, self.keep)
            log.info("arXiv RSS %s: %d items", cat, len(papers))
            yield from papers


class ArxivAPI:
    name = "arxiv_api"

    def __init__(self, client: httpx.Client, limiter: RateLimiter, categories: list[str],
                 page_size: int = 100, max_pages: int = 20):
        self.client, self.limiter = client, limiter
        self.categories = categories
        self.page_size, self.max_pages = page_size, max_pages

    def fetch(self, since: datetime | None = None) -> Iterator[Paper]:
        """Newest-first per category, stopping once results are older than `since`."""
        since_date = since.date().isoformat() if since else None
        for cat in self.categories:
            for page in range(self.max_pages):
                params = {
                    "search_query": f"cat:{cat}",
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "start": page * self.page_size,
                    "max_results": self.page_size,
                }
                resp = get_with_retry(self.client, API_BASE, params=params, limiter=self.limiter)
                papers = parse_api(resp.content)
                if not papers:
                    break
                fresh = [p for p in papers if not since_date or (p.published or "") >= since_date]
                yield from fresh
                if len(fresh) < len(papers):
                    break
