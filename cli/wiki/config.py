"""Config + repo-root discovery.

The repo root is the nearest ancestor of CWD containing config.toml. The live DB
lives at an absolute path from config (default ~/.wiki-brain/wiki.db) and is NOT
inside the working tree (BUILD_SPEC.md §2).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "paths": {
        "db": "~/.wiki-brain/wiki.db",
        "bookmarks": [],
        "bookmark_folder": "wiki",
        "drop_folder": "~/wiki-brain-drop",
    },
    "gate": {
        "auto_promote_confidence": 0.85,
        "machine_confidence_ceiling": 0.9,
    },
    "budgets": {
        "queries_per_question": 4,
        "fetches_per_question": 5,
        "questions_per_night": 8,
        "fetches_per_night": 40,
    },
    "search": {
        "engine": "ddg",
        "searxng_url": "http://localhost:8888",
        "fetch_backend": "jina",
        "jina_base": "https://r.jina.ai/",
    },
    "lint": {
        "stale_days": 30,
        "contradiction_days": 14,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_repo_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / "config.toml").exists():
            return p
    # Fallback: if a marker dir layout exists, use cwd; else cwd.
    return cur


@dataclass
class Config:
    root: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, start: Path | None = None) -> "Config":
        root = find_repo_root(start)
        cfg_path = root / "config.toml"
        user = {}
        if cfg_path.exists():
            with open(cfg_path, "rb") as fh:
                user = tomllib.load(fh)
        return cls(root=root, data=_deep_merge(DEFAULTS, user))

    # --- typed accessors ---
    @property
    def db_path(self) -> Path:
        raw = self.data["paths"]["db"]
        return Path(os.path.expanduser(raw)).resolve()

    @property
    def bookmark_files(self) -> list[Path]:
        return [Path(os.path.expanduser(p)) for p in self.data["paths"].get("bookmarks", [])]

    @property
    def drop_folder(self) -> Path | None:
        raw = self.data["paths"].get("drop_folder")
        return Path(os.path.expanduser(raw)).resolve() if raw else None

    @property
    def bookmark_folder(self) -> str:
        return self.data["paths"].get("bookmark_folder", "wiki")

    def gate(self, key: str):
        return self.data["gate"][key]

    def budget(self, key: str):
        return self.data["budgets"][key]

    def search_cfg(self, key: str):
        return self.data["search"].get(key)

    def lint_cfg(self, key: str):
        return self.data["lint"].get(key)
