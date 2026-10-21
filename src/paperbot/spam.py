"""Spam filter for harvested records, applied before anything is spent on them.

OpenAlex keyword searches also return records from open repositories (Zenodo, Figshare, ...)
that anyone can upload to without review. Some are keyword-stuffed "monographs" that Jev
rightly finds on-topic, so they top the debate queue. Example that prompted this: Zenodo
"STARLI RESEARCH WORKS — INSTITUTE XXIII / FLAGSHIP 09 ..." with the single author "33".

Rules (any one flags a record; settings.yaml `spam:` can disable or extend them):
- authors are listed but none looks like a name (fewer than two letters), e.g. "33";
- a mostly upper-case title from an open repository venue;
- a configured regex (case-insensitive) matches the title or venue.

Missing authors alone is not spam (OpenAlex often lacks them for real papers). Flagged papers get
status `spam`: never triaged, debated or shown to friends, but kept so they can be reviewed in
the bank and released with `paperbot spam --release <id>` or the website's "not spam" button.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_REPOSITORIES = ["zenodo", "figshare", "osf", "researchgate", "preprints.org", "ssrn"]


@dataclass
class SpamRules:
    enabled: bool = True
    patterns: list[re.Pattern] = field(default_factory=list)
    repositories: list[str] = field(default_factory=lambda: list(DEFAULT_REPOSITORIES))
    shout_ratio: float = 0.6   # share of letters that are upper case

    @classmethod
    def from_settings(cls, section: dict[str, Any] | None) -> "SpamRules":
        s = section or {}
        return cls(enabled=s.get("enabled", True),
                   patterns=[re.compile(p, re.I) for p in s.get("title_patterns") or []],
                   repositories=[r.lower() for r in s.get("repositories") or DEFAULT_REPOSITORIES],
                   shout_ratio=float(s.get("shout_ratio", 0.6)))


def _plausible_name(name: str) -> bool:
    return sum(ch.isalpha() for ch in name or "") >= 2


def _shouting(title: str, ratio: float) -> bool:
    letters = [ch for ch in title if ch.isalpha()]
    return len(letters) >= 25 and sum(ch.isupper() for ch in letters) / len(letters) >= ratio


def spam_reasons(title: str, authors: list[str], venue: str | None, rules: SpamRules) -> list[str]:
    if not rules.enabled:
        return []
    reasons = []
    if authors and not any(_plausible_name(a) for a in authors):
        reasons.append(f"no plausible author ({', '.join(authors[:3])})")
    v = (venue or "").lower()
    if any(r in v for r in rules.repositories) and _shouting(title or "", rules.shout_ratio):
        reasons.append("all-caps title from an open repository")
    for p in rules.patterns:
        if p.search(title or "") or p.search(venue or ""):
            reasons.append(f"matches blocklist /{p.pattern}/")
    return reasons


def row_reasons(row, rules: SpamRules) -> list[str]:
    return spam_reasons(row["title"], json.loads(row["authors"] or "[]"), row["venue"], rules)


# Statuses a paper can be moved to spam from. Debated/digested papers are left alone: money was
# already spent and they may be in a digest Roz has read.
RESCANNABLE = ("new", "triage_error", "rejected", "passed", "skim", "friends_only", "debate_error")


def mark(store, paper_id: str, reasons: list[str]) -> None:
    store.update(paper_id, status="spam", error="spam: " + "; ".join(reasons)[:480])


def check_new(store, paper_id: str, paper, rules: SpamRules) -> list[str]:
    """Call right after inserting a new paper; flags it as spam if any rule matches."""
    reasons = spam_reasons(paper.title, paper.authors, paper.venue, rules)
    if reasons:
        mark(store, paper_id, reasons)
    return reasons


def rescan(store, rules: SpamRules, dry_run: bool = False) -> list[tuple[str, str, str, list[str]]]:
    """Apply the rules to papers already in the database. Returns (id, old status, title, reasons)."""
    found = []
    for row in store.by_status(*RESCANNABLE):
        reasons = row_reasons(row, rules)
        if reasons:
            found.append((row["id"], row["status"], row["title"], reasons))
            if not dry_run:
                mark(store, row["id"], reasons)
    if not dry_run:
        store.log_run("spam:rescan", fetched=len(found), new=len(found))
    return found


def release(store, paper_id: str) -> bool:
    """Not spam after all: back to `new`, so the next triage scores it again (fractions of a cent)."""
    row = store.get(paper_id)
    if row is None or row["status"] != "spam":
        return False
    store.update(paper_id, status="new", error=None)
    return True
