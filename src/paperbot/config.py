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


def load_dotenv(path: Path) -> list[str]:
    """Load KEY=VALUE lines from a .env file into os.environ. Variables already set in the
    real environment win. Returns the names that were set. Comments and blanks are skipped;
    surrounding single/double quotes on values are stripped."""
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if key and val and not os.environ.get(key):
            os.environ[key] = val
            loaded.append(key)
    return loaded


def load_config(root: str | Path | None = None) -> Config:
    """Load config from <root>/config, and secrets from <root>/.env if present.
    Root defaults to $PAPERBOT_ROOT or the cwd."""
    root_path = Path(root or os.environ.get("PAPERBOT_ROOT") or Path.cwd()).resolve()
    load_dotenv(root_path / ".env")
    cfg_dir = root_path / "config"
    with open(cfg_dir / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}
    with open(cfg_dir / "profile.yaml", encoding="utf-8") as f:
        profile = yaml.safe_load(f) or {}
    return Config(root=root_path, settings=settings, profile=profile)
