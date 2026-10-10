"""Threshold gate: decides which scored papers go to the (expensive) debate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .questions import RELEVANCE_KEY


@dataclass
class GateDecision:
    passed: bool
    score: float
    reason: str


def decide(answers: dict[str, Any], threshold: float, must_have: list[str],
           rescue_margin: float = 0.5, rescue_noul: float = 0.9) -> GateDecision:
    score = float(answers[RELEVANCE_KEY]["score"])
    if score >= threshold:
        return GateDecision(True, score, f"score {score:.2f} >= {threshold}")
    if score >= threshold - rescue_margin:
        for key in must_have:
            val = (answers.get(key) or {}).get("noul")  # Nouls have no `confidence` field
            if val is not None and val >= rescue_noul:
                return GateDecision(True, score, f"rescued by {key}={val:.2f} (score {score:.2f})")
    return GateDecision(False, score, f"score {score:.2f} < {threshold}")
