"""Threshold calibration from hand labels.

Workflow: run harvest + triage for a few days with the placeholder threshold, then
`paperbot label` (~50-100 abstracts, sampled across the score range), then
`paperbot calibrate` to see precision/recall per threshold and a suggested cutoff.

Default objective is F2 (recall weighted 2x over precision): missing a relevant paper
costs more than paying for one extra debate.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class ThresholdRow:
    threshold: float
    precision: float
    recall: float
    f_beta: float
    pass_rate: float
    passes: int | None = None   # papers in the population at or above the threshold


def _bucket(score: float, max_score: float) -> int:
    return min(int(score), int(max_score))


def stratum_weights(labeled: list[tuple[float, int]], population: list[float],
                    max_score: float = 4.0) -> tuple[dict[int, float], list[int]]:
    """Inverse-sampling-probability weight per score bucket: population count / label count.

    `sample_for_labeling` draws evenly per bucket, so the high buckets are heavily
    over-represented among labels; without these weights precision, recall and pass rate
    describe the label sample, not the paper stream. Returns (weights, buckets that have
    papers but no labels — their papers are invisible to the estimate)."""
    pop, lab = Counter(_bucket(s, max_score) for s in population), Counter(_bucket(s, max_score) for s, _ in labeled)
    weights = {b: pop[b] / lab[b] for b in lab if pop[b]}
    uncovered = sorted(b for b in pop if not lab[b])
    return weights, uncovered


def sweep(labeled: list[tuple[float, int]], beta: float = 2.0, step: float = 0.25,
          max_score: float = 4.0, population: list[float] | None = None) -> list[ThresholdRow]:
    """Precision/recall/F-beta per threshold. With `population` (all scored papers), labels are
    reweighted per score bucket and pass rate/counts come from the population."""
    if population:
        weights, _ = stratum_weights(labeled, population, max_score)
        w = [(s, y, weights.get(_bucket(s, max_score), 0.0)) for s, y in labeled]
    else:
        w = [(s, y, 1.0) for s, y in labeled]
    rows: list[ThresholdRow] = []
    positives = sum(wt for _, y, wt in w if y)
    total = sum(wt for _, _, wt in w)
    t = 0.0
    while t <= max_score + 1e-9:
        tp = sum(wt for s, y, wt in w if s >= t and y)
        fp = sum(wt for s, y, wt in w if s >= t and not y)
        passed = tp + fp
        p = tp / passed if passed else 1.0
        r = tp / positives if positives else 0.0
        b2 = beta * beta
        f = (1 + b2) * p * r / (b2 * p + r) if (p + r) else 0.0
        if population:
            n_pass = sum(1 for s in population if s >= t)
            rows.append(ThresholdRow(round(t, 2), p, r, f, n_pass / len(population), n_pass))
        else:
            rows.append(ThresholdRow(round(t, 2), p, r, f, passed / total if total else 0.0))
        t += step
    return rows


def suggest(rows: list[ThresholdRow], max_passes: int | None = None) -> ThresholdRow:
    """Highest F-beta; ties broken toward the higher (cheaper) threshold. With `max_passes`,
    only thresholds whose population pass count fits the budget are eligible (falling back
    to the strictest threshold if none fits)."""
    eligible = rows
    if max_passes is not None and any(r.passes is not None for r in rows):
        eligible = [r for r in rows if r.passes is not None and r.passes <= max_passes] or [rows[-1]]
    return max(eligible, key=lambda r: (round(r.f_beta, 6), r.threshold))


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in [start, end]: arXiv announces on weekdays, so this approximates runs."""
    days = (end - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)


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
    with_counts = any(r.passes is not None for r in rows)
    lines = ["threshold  precision  recall  F2     pass-rate" + ("   passes" if with_counts else "")]
    for r in rows:
        mark = "  <- suggested" if r is best else ""
        count = f"  {r.passes:>7}" if with_counts else ""
        lines.append(f"{r.threshold:>9.2f}  {r.precision:>9.2f}  {r.recall:>6.2f}  {r.f_beta:.3f}  "
                     f"{r.pass_rate:>8.1%}{count}{mark}")
    return "\n".join(lines)
