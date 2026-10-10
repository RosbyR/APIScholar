"""Minimal Jev (TypeSafe System One) client over plain HTTP.

POST https://api.typesafe.ai/v1/systemone
Body: {"state": ..., "model": ..., "questions": {id: {type, instructions, criteria?}}}
Answers: noul -> {"noul": float}            (no confidence field!)
         score -> {"score", "legend", "probabilities", "confidence"}
         choice -> {"choice", "probabilities", "confidence"}

401/422 are bugs (bad key / malformed question set): raised as JevFatalError, never retried.
429/529 are transient: exponential backoff.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevError(Exception):
    """Transient or unknown failure for one item. The item is marked triage_error and retried next run."""


class JevFatalError(JevError):
    """Configuration bug (401 bad key, 422 bad question schema). Stop the stage."""


class JevClient:
    def __init__(self, api_key: str, model: str = "jev-latest", timeout_s: float = 20.0,
                 max_retries: int = 4, client: httpx.Client | None = None, sleep=time.sleep):
        self.model = model
        self.max_retries = max_retries
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def evaluate(self, state: Any, questions: dict[str, dict]) -> dict[str, Any]:
        body = {"state": state, "model": self.model, "questions": questions}
        attempt = 0
        while True:
            try:
                resp = self._client.post(ENDPOINT, json=body, headers=self._headers)
            except httpx.TransportError as e:
                if attempt >= self.max_retries:
                    raise JevError(f"transport error: {e}") from e
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 422):
                    raise JevFatalError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                if resp.status_code not in (429, 529) or attempt >= self.max_retries:
                    raise JevError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            self._sleep(0.25 * (2 ** attempt) + random.uniform(0, 0.1))
            attempt += 1
