"""Fetch and extract paper text for the debate. Falls back to the abstract."""

from __future__ import annotations

import io
import logging
import re

import httpx

from .http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

_REFS_RE = re.compile(r"\n\s*(references|bibliography)\s*\n", re.I)


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def trim(text: str, max_chars: int) -> str:
    """Drop the reference list (last 'References' heading) and cap length."""
    matches = list(_REFS_RE.finditer(text))
    if matches and matches[-1].start() > len(text) * 0.5:
        text = text[: matches[-1].start()]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... truncated ...]"
    return text


def get_text(paper: dict, client: httpx.Client, arxiv_limiter: RateLimiter | None,
             max_chars: int = 60000) -> tuple[str, str]:
    """Return (text, kind) where kind is 'full text' or 'abstract only'."""
    url = paper.get("pdf_url")
    if url:
        try:
            limiter = arxiv_limiter if "arxiv.org" in url else None
            resp = get_with_retry(client, url, limiter=limiter, max_retries=2)
            if "pdf" in resp.headers.get("content-type", "") or resp.content[:4] == b"%PDF":
                text = trim(extract_pdf_text(resp.content), max_chars)
                if len(text) > 2000:
                    return text, "full text"
        except Exception as e:  # any fetch/parse failure -> abstract fallback
            log.warning("full text failed for %s: %s", paper.get("id"), e)
    return paper.get("abstract") or "(no abstract available)", "abstract only"
