"""Normalized paper record shared by every harvester adapter."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

# Pipeline status values, in order. A paper only moves forward.
STATUS_NEW = "new"                    # harvested, not yet scored
STATUS_TRIAGE_ERROR = "triage_error"  # Jev failed after retries (fail open: retried next run, counted in digest)
STATUS_REJECTED = "rejected"          # below threshold
STATUS_PASSED = "passed"              # above threshold, waiting for debate
STATUS_DEBATE_ERROR = "debate_error"  # LLM failure; retried next run
STATUS_VETOED = "vetoed"              # judge said not worth reading
STATUS_ACCEPTED = "accepted"          # judge kept it; goes in next digest
STATUS_DIGESTED = "digested"          # delivered in a digest

_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?$|([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$", re.I)
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d or None


def normalize_arxiv_id(raw: str | None) -> str | None:
    """'http://arxiv.org/abs/2410.01234v2' -> '2410.01234'; 'oai:arXiv.org:2410.01234v1' -> '2410.01234'."""
    if not raw:
        return None
    s = raw.strip()
    m = _ARXIV_DOI_RE.match(s)
    if m:
        s = m.group(1)
    for sep in ("abs/", "pdf/", "arXiv.org:", "arxiv.org:", "arXiv:", "arxiv:"):
        if sep in s:
            s = s.split(sep, 1)[1]
    s = s.removesuffix(".pdf")
    m = _ARXIV_ID_RE.search(s)
    if not m:
        return None
    return m.group(1) or m.group(3)


def title_hash(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    return hashlib.sha1(t.encode()).hexdigest()[:16]


@dataclass
class Paper:
    title: str
    abstract: str
    source: str                       # adapter name, e.g. "arxiv_rss", "openalex"
    doi: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    authors: list[str] = field(default_factory=list)
    venue: str | None = None
    published: str | None = None      # ISO date YYYY-MM-DD
    url: str | None = None
    pdf_url: str | None = None
    categories: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.title = " ".join((self.title or "").split())
        self.abstract = " ".join((self.abstract or "").split())
        self.doi = normalize_doi(self.doi)
        # arXiv-minted DOIs identify the arXiv record
        if self.doi and not self.arxiv_id:
            self.arxiv_id = normalize_arxiv_id(self.doi) if self.doi.startswith("10.48550/") else None
        self.arxiv_id = normalize_arxiv_id(self.arxiv_id) if self.arxiv_id else None
        if self.arxiv_id and not self.pdf_url:
            self.pdf_url = f"https://arxiv.org/pdf/{self.arxiv_id}"
        if self.arxiv_id and not self.url:
            self.url = f"https://arxiv.org/abs/{self.arxiv_id}"

    @property
    def title_key(self) -> str:
        return title_hash(self.title)

    @property
    def canonical_id(self) -> str:
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.doi:
            return f"doi:{self.doi}"
        if self.openalex_id:
            return f"openalex:{self.openalex_id}"
        return f"title:{self.title_key}"
