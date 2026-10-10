"""Shared HTTP plumbing: a polite rate limiter and GET with bounded retries."""

from __future__ import annotations

import logging
import random
import threading
import time

import httpx

from . import __version__

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 502, 503, 504, 529}


class RateLimiter:
    """Enforces a minimum interval between calls (thread-safe)."""

    def __init__(self, min_interval_s: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval_s = min_interval_s
        self._clock, self._sleep = clock, sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self.min_interval_s - (now - self._last)
                if delay > 0:
                    self._sleep(delay)
                    now = self._clock()
            self._last = now


def user_agent(contact_email: str | None) -> str:
    ua = f"paperbot/{__version__} (personal research digest)"
    return f"{ua}; mailto:{contact_email}" if contact_email else ua


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    limiter: RateLimiter | None = None,
    max_retries: int = 4,
    backoff_s: float = 2.0,
    sleep=time.sleep,
) -> httpx.Response:
    """GET with exponential backoff on 429/5xx and transport errors. Raises on final failure."""
    attempt = 0
    while True:
        if limiter:
            limiter.wait()
        try:
            resp = client.get(url, params=params)
        except httpx.TransportError as e:
            if attempt >= max_retries:
                raise
            log.warning("transport error on %s: %s (retry %d)", url, e, attempt + 1)
        else:
            if resp.status_code not in RETRYABLE:
                resp.raise_for_status()
                return resp
            if attempt >= max_retries:
                resp.raise_for_status()
            log.warning("HTTP %s on %s (retry %d)", resp.status_code, url, attempt + 1)
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep(float(retry_after))
                attempt += 1
                continue
        sleep(backoff_s * (2 ** attempt) + random.uniform(0, 0.5))
        attempt += 1
