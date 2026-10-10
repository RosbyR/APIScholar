"""OpenAlex adapter: targeted keyword queries reaching journals/conferences not on arXiv.

Notes (as of 2026):
- An API key is required for production use (free; $1/day of usage). Passed as `api_key`.
- Abstracts come as `abstract_inverted_index` ({word: [positions]}) and must be rebuilt.
- Some publishers' abstracts are missing; those papers are kept with an empty abstract
  and Jev scores them on title + venue only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

import httpx

from ..http import RateLimiter, get_with_retry
from ..models import Paper, normalize_arxiv_id

log = logging.getLogger(__name__)

API = "https://api.openalex.org/works"
SELECT = ",".join([
    "id", "doi", "display_name", "publication_date", "authorships", "primary_location",
    "best_oa_location", "locations", "abstract_inverted_index", "type",
])


def rebuild_abstract(inv: dict[str, list[int]] | None) -> str:
    if not inv:
        return ""
    positions: list[tuple[int, str]] = [(i, w) for w, idxs in inv.items() for i in idxs]
    positions.sort()
    return " ".join(w for _, w in positions)


def _arxiv_from_locations(work: dict) -> str | None:
    for loc in work.get("locations") or []:
        for key in ("landing_page_url", "pdf_url"):
            url = (loc or {}).get(key) or ""
            if "arxiv.org" in url:
                aid = normalize_arxiv_id(url)
                if aid:
                    return aid
    return None


def parse_work(work: dict) -> Paper:
    primary = work.get("primary_location") or {}
    best_oa = work.get("best_oa_location") or {}
    source = primary.get("source") or {}
    return Paper(
        title=work.get("display_name") or "",
        abstract=rebuild_abstract(work.get("abstract_inverted_index")),
        source="openalex",
        doi=work.get("doi"),
        arxiv_id=_arxiv_from_locations(work),
        openalex_id=(work.get("id") or "").rsplit("/", 1)[-1] or None,
        authors=[
            (a.get("author") or {}).get("display_name")
            for a in work.get("authorships") or []
            if (a.get("author") or {}).get("display_name")
        ],
        venue=source.get("display_name"),
        published=work.get("publication_date"),
        url=primary.get("landing_page_url") or work.get("doi"),
        pdf_url=best_oa.get("pdf_url"),
    )


class OpenAlex:
    name = "openalex"

    def __init__(self, client: httpx.Client, limiter: RateLimiter, queries: list[str],
                 api_key: str | None, per_page: int = 100, max_pages_per_query: int = 3):
        self.client, self.limiter = client, limiter
        self.queries = queries
        self.api_key = api_key
        self.per_page, self.max_pages = per_page, max_pages_per_query
        if not api_key:
            log.warning("OPENALEX_API_KEY not set: keyless budget is tiny and not meant for production")

    def fetch(self, since: datetime | None = None) -> Iterator[Paper]:
        for q in self.queries:
            filters = [f"title_and_abstract.search:{q}"]
            if since:
                filters.append(f"from_publication_date:{since.date().isoformat()}")
            cursor = "*"
            for _ in range(self.max_pages):
                params = {
                    "filter": ",".join(filters),
                    "sort": "publication_date:desc",
                    "per_page": self.per_page,
                    "cursor": cursor,
                    "select": SELECT,
                }
                if self.api_key:
                    params["api_key"] = self.api_key
                data = get_with_retry(self.client, API, params=params, limiter=self.limiter).json()
                results = data.get("results") or []
                for w in results:
                    if w.get("type") in ("paratext", "erratum", "retraction"):
                        continue
                    yield parse_work(w)
                cursor = (data.get("meta") or {}).get("next_cursor")
                if not cursor or not results:
                    break
