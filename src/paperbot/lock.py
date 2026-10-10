"""Single-instance lock so two scheduled `paperbot run`s can't overlap.

The lock is a file created with O_EXCL (atomic on Windows, macOS and Linux). It is removed
on exit; if a run is killed hard and leaves it behind, a lock older than `stale_after_s`
is treated as abandoned and taken over, so one crash can't block every future run.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


class LockHeld(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: str | Path, stale_after_s: float = 6 * 3600):
        self.path = Path(path)
        self.stale_after_s = stale_after_s
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue  # released between our open and stat; try again
                if age < self.stale_after_s:
                    holder = self.path.read_text(encoding="utf-8", errors="replace").strip()
                    raise LockHeld(f"another run holds {self.path} ({holder}, {age / 60:.0f} min old)")
                log.warning("taking over stale lock %s (%.1f h old)", self.path, age / 3600)
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%S')}")
            self._held = True
            return
        raise LockHeld(f"could not acquire {self.path}")

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
