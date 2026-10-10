"""Build the Jev request (state + question set) from profile.yaml.

Design rules (from TypeSafe docs / field experience):
- Relevance is a Score (ordered ladder), not a Choice: we threshold on the weighted score.
- Topic facets are Nouls: they are not mutually exclusive, so a Choice would split
  probability between true options and destroy confidence.
- State carries only what the questions need (title, venue, abstract). Extra context hurts.
"""

from __future__ import annotations

from typing import Any

RELEVANCE_KEY = "relevance"
FACET_PREFIX = "facet_"


def build_questions(profile: dict[str, Any]) -> dict[str, dict]:
    rel = profile["relevance"]
    levels = rel["levels"]
    if not 2 <= len(levels) <= 10:
        raise ValueError("relevance.levels must have 2-10 entries (Jev Score limit)")
    questions: dict[str, dict] = {
        RELEVANCE_KEY: {"type": "score", "instructions": " ".join(rel["instructions"].split()), "criteria": levels},
    }
    for key, facet in (profile.get("facets") or {}).items():
        q: dict[str, Any] = {"type": "noul", "instructions": facet["instructions"]}
        if facet.get("criteria"):
            q["criteria"] = facet["criteria"]
        questions[FACET_PREFIX + key] = q
    return questions


def must_have_keys(profile: dict[str, Any]) -> list[str]:
    return [FACET_PREFIX + k for k, f in (profile.get("facets") or {}).items() if f.get("must_have")]


def build_state(title: str, abstract: str, venue: str | None) -> dict[str, str]:
    state = {"title": title, "abstract": abstract or "(no abstract available)"}
    if venue:
        state["venue"] = venue
    return state
