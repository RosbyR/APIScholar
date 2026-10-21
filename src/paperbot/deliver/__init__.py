"""Deliver stage: push a written digest to where Roz reads it. Runs after `digest`.

Channels are configured under `deliver:` in settings.yaml. Each (digest file, channel) pair
is delivered at most once (recorded in the `deliveries` table) unless forced, so rerunning
the stage, or `paperbot run`, never duplicates a page.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Config
from ..store import Store
from .onenote import OneNoteError, markdown_to_page_xml, push_page

log = logging.getLogger(__name__)

PUSH_SCRIPT = Path(__file__).with_name("onenote_push.ps1")


@dataclass
class DeliverStats:
    delivered: list[str] = field(default_factory=list)   # "<channel>: <ref>"
    skipped: list[str] = field(default_factory=list)     # already delivered / disabled
    errors: list[str] = field(default_factory=list)


def _title(md: str, fallback: str) -> str:
    first = md.splitlines()[0] if md else ""
    return first[2:].strip() if first.startswith("# ") else fallback


def run_deliver(cfg: Config, store: Store, digest_path: Path, force: bool = False,
                push: Callable[..., str] = push_page) -> DeliverStats:
    stats = DeliverStats()
    on = (cfg.section("deliver").get("onenote") or {})
    if not on.get("enabled"):
        stats.skipped.append("onenote: disabled")
        return stats
    if push is push_page and sys.platform != "win32":
        # The scheduled cloud run is Linux; there the website is the delivery channel.
        stats.skipped.append("onenote: needs Windows with OneNote desktop")
        return stats
    key = digest_path.name
    if not force and store.delivered(key, "onenote"):
        stats.skipped.append(f"onenote: {key} already delivered")
        return stats
    md = digest_path.read_text(encoding="utf-8")
    try:
        xml = markdown_to_page_xml(md, _title(md, digest_path.stem))
        page_id = push(xml, on.get("notebook", ""), on.get("section", "Paper Digests"), PUSH_SCRIPT)
    except (OneNoteError, OSError) as e:
        log.error("OneNote delivery of %s failed: %s", key, e)
        stats.errors.append(f"onenote: {e}")
        store.log_run("deliver:onenote", errors=1, note=f"{key}: {e}"[:500])
        return stats
    store.record_delivery(key, "onenote", page_id)
    store.log_run("deliver:onenote", fetched=1, new=1, note=f"{key} -> {page_id}")
    stats.delivered.append(f"onenote: {on.get('notebook')} / {on.get('section')} ({key})")
    return stats
