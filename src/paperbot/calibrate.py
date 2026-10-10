"""Threshold calibration from hand labels.

Workflow: run harvest + triage for a few days with the placeholder threshold, then
`paperbot label` (~50-100 abstracts, sampled across the score range), then
`paperbot calibrate` to see precision/recall per threshold and a suggested cutoff.

Default objective is F2 (recall weighted 2x over precision): missing a relevant paper
costs more than paying for one extra debate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class ThresholdRow:
    threshold: float
    precision: float
    recall: float
    f_beta: float
    pass_rate: float


def sweep(labeled: list[tuple[float, int]], beta: float = 2.0, step: float = 0.25,
          max_score: float = 4.0) -> list[ThresholdRow]:
    rows: list[ThresholdRow] = []
    n = len(labeled)
    positives = sum(y for _, y in labeled)
    t = 0.0
    while t <= max_score + 1e-9:
        tp = sum(1 for s, y in labeled if s >= t and y)
        fp = sum(1 for s, y in labeled if s >= t and not y)
        passed = tp + fp
        p = tp / passed if passed else 1.0
        r = tp / positives if positives else 0.0
        b2 = beta * beta
        f = (1 + b2) * p * r / (b2 * p + r) if (p + r) else 0.0
        rows.append(ThresholdRow(round(t, 2), p, r, f, passed / n if n else 0.0))
        t += step
    return rows


def suggest(rows: list[ThresholdRow]) -> ThresholdRow:
    # highest F-beta; ties broken toward the higher (cheaper) threshold
    return max(rows, key=lambda r: (round(r.f_beta, 6), r.threshold))


def sample_for_labeling(rows: list, n: int, max_score: float = 4.0, seed: int | None = None) -> list:
    """Stratified sample across score buckets so labels cover the whole range."""
    rng = random.Random(seed)
    buckets: dict[int, list] = {}
    for r in rows:
        b = min(int(r["jev_score"]), int(max_score))
        buckets.setdefault(b, []).append(r)
    for b in buckets.values():
        rng.shuffle(b)
    out: list = []
    while len(out) < n and any(buckets.values()):
        for k in sorted(buckets):
            if buckets[k] and len(out) < n:
                out.append(buckets[k].pop())
    rng.shuffle(out)
    return out


def format_table(rows: list[ThresholdRow], best: ThresholdRow) -> str:
    lines = ["threshold  precision  recall  F2     pass-rate"]
    for r in rows:
        mark = "  <- suggested" if r is best else ""
        lines.append(f"{r.threshold:>9.2f}  {r.precision:>9.2f}  {r.recall:>6.2f}  {r.f_beta:.3f}  "
                     f"{r.pass_rate:>8.0%}{mark}")
    return "\n".join(lines)
