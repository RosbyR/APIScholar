"""Load settings.yaml and profile.yaml. Secrets come only from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Config:
    root: Path
    settings: dict[str, Any]
    profile: dict[str, Any]

    # convenience accessors -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._resolve(self.settings.get("db_path", "data/paperbot.sqlite"))

    @property
    def digest_dir(self) -> Path:
        return self._resolve(self.settings.get("digest_dir", "digests"))

    def section(self, name: str) -> dict[str, Any]:
        return self.settings.get(name, {}) or {}

    def _resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.root / path

    @staticmethod
    def env(name: str, required: bool = False) -> str | None:
        val = os.environ.get(name) or None
        if required and not val:
            raise RuntimeError(f"Environment variable {name} is not set")
        return val


def load_config(root: str | Path | None = None) -> Config:
    """Load config from <root>/config. Root defaults to $PAPERBOT_ROOT or the cwd."""
    root_path = Path(root or os.environ.get("PAPERBOT_ROOT") or Path.cwd()).resolve()
    cfg_dir = root_path / "config"
    with open(cfg_dir / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}
    with open(cfg_dir / "profile.yaml", encoding="utf-8") as f:
        profile = yaml.safe_load(f) or {}
    return Config(root=root_path, settings=settings, profile=profile)
