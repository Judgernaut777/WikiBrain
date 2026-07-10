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

# Overrides `[paths] db`. The isolation lever for tests, MCP verification, and any
# throwaway script — because opening a repo migrates whatever DB it resolves to.
DB_ENV_VAR = "WIKIBRAIN_DB"

DEFAULTS = {
    "paths": {
        "db": "~/.wiki-brain/wiki.db",
        "bookmarks": [],
        "bookmark_folder": "wiki",
        "drop_folder": "~/wiki-brain-drop",
        "sources": [],          # [[paths.sources]] — extra ingestion folders (see IngestSource)
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
    "extract": {
        "ocr": True,            # allow Tesseract OCR for images
        "tesseract_cmd": "",    # explicit path to tesseract.exe (Windows), or "" for PATH
        "docling_ocr": True,    # let Docling OCR scanned PDFs
        "whisper_model": "base",  # local ASR model size (only if [whisper] extra)
    },
    "embed": {
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "enabled": False,       # off by default; the [semantic] extra is heavy (torch)
    },
    "mcp": {
        "read_only": False,     # if True, `wiki mcp serve` omits the capture write tool
        "recall_k": 8,          # default top-k claims returned by brain_recall
    },
    "safety": {
        # WikiBrain-local memory safety (docs/SAFETY.md). WikiBrain owns policy;
        # detection is delegated to modular engines. The default set is
        # lightweight: `baseline` is pure stdlib, and `detect_secrets` is simply
        # inert when its package is absent. Nothing here spawns a process, loads a
        # model, or makes a network call.
        #
        # Full schema, per-engine options and install extras live in
        # wiki/safety/configuration.py and config.example.toml. Unknown engine
        # names are a hard error, because a typo that silently disables secret
        # scanning is the worst possible failure mode.
        "enabled": True,
        "max_text_chars": 200_000,
        "engines": {
            "baseline": {"enabled": True, "required": True},
            "detect_secrets": {"enabled": True, "required": False},
            "trufflehog": {"enabled": False},
            "gitleaks": {"enabled": False},
            "presidio": {"enabled": False},
            "prompt_guard": {"enabled": False},
        },
    },
    "retrieval": {
        # Which retrieval backend serves recall (LEDGER_SPEC.md §8). The backend
        # returns *candidates*; WikiBrain applies trust/status/scope filtering
        # afterwards, so a backend can degrade recall quality but never widen
        # trust. Only `sqlite_fts` ships today; graphiti/cognee/qdrant/chroma/
        # llamaindex are the planned adapters. Unknown names fail loudly.
        "backend": "sqlite_fts",
        "profile": "manager_brief",  # default recall profile
        "max_items": 8,              # default bound on a RecallPack
        # How many candidates to ask the backend for per requested item. Trust and
        # scope filtering happens after retrieval, so the backend must over-fetch
        # or a bounded pack could come back short.
        "overfetch": 4,
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


def find_repo_root(start: Path | None = None) -> tuple[Path, bool]:
    """Return (root, found). `found` is True iff an ancestor of `start` (or CWD)
    contains config.toml. False means no wiki-brain repo was found and we fell
    back to that directory anyway — callers that require an existing brain
    should treat `found=False` as an error instead of silently operating on
    whatever directory the shell happened to be in."""
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / "config.toml").exists():
            return p, True
    return cur, False


@dataclass(frozen=True)
class IngestSource:
    """One configured ingestion folder: where to scan and how to treat it.

    `include` is a tuple of fnmatch globs (empty = all files). `move=True`
    archives originals to <folder>/.processed/ after ingest; the default False
    leaves the user's files untouched — global content-hash dedup
    (`sources.hash`) makes re-scanning the same folder harmless. `origin` tags
    provenance on each ingested source (avoid the reserved values 'clip',
    'transcript', 'session/*', 'autoresearch', which carry special downstream
    meaning)."""
    path: Path
    origin: str = "drop"
    recursive: bool = False
    include: tuple[str, ...] = ()
    move: bool = False


@dataclass
class Config:
    root: Path
    data: dict = field(default_factory=dict)
    found: bool = True

    @classmethod
    def load(cls, start: Path | None = None) -> "Config":
        root, found = find_repo_root(start)
        cfg_path = root / "config.toml"
        user = {}
        if cfg_path.exists():
            with open(cfg_path, "rb") as fh:
                user = tomllib.load(fh)
        return cls(root=root, data=_deep_merge(DEFAULTS, user), found=found)

    # --- typed accessors ---
    @property
    def db_path(self) -> Path:
        """Where the live database lives.

        `WIKIBRAIN_DB` overrides `[paths] db` and takes precedence over everything.
        It exists because `Repo.open()` runs forward migrations on EVERY open (see
        docs/MIGRATIONS.md): a verification script that merely passes `root=` picks
        the repo's config.toml and therefore still opens — and migrates — the
        user's real `~/.wiki-brain/wiki.db`. Passing a temp root is NOT isolation.
        Point this at a scratch file instead.
        """
        env = os.environ.get(DB_ENV_VAR, "").strip()
        raw = env or self.data["paths"]["db"]
        return Path(os.path.expanduser(raw)).resolve()

    @property
    def bookmark_files(self) -> list[Path]:
        return [Path(os.path.expanduser(p)) for p in self.data["paths"].get("bookmarks", [])]

    @property
    def drop_folder(self) -> Path | None:
        raw = self.data["paths"].get("drop_folder")
        return Path(os.path.expanduser(raw)).resolve() if raw else None

    @property
    def ingest_sources(self) -> list[IngestSource]:
        """Every configured ingestion folder as a normalized IngestSource.

        Each `[[paths.sources]]` entry (expanduser + resolve, dedup by path,
        entries lacking `path` skipped), plus the legacy single `drop_folder`
        synthesized as an implicit source (origin 'drop', archives originals) —
        appended only when its path isn't already listed explicitly, so an
        explicit entry for that path wins."""
        out: list[IngestSource] = []
        seen: set[Path] = set()
        for e in self.data["paths"].get("sources", []):
            raw = e.get("path") if isinstance(e, dict) else None
            if not raw:
                continue
            p = Path(os.path.expanduser(raw)).resolve()
            if p in seen:
                continue
            seen.add(p)
            out.append(IngestSource(
                path=p,
                origin=e.get("origin") or "drop",
                recursive=bool(e.get("recursive", False)),
                include=tuple(e.get("include") or ()),
                move=bool(e.get("move", False)),
            ))
        legacy = self.data["paths"].get("drop_folder")
        if legacy:
            lp = Path(os.path.expanduser(legacy)).resolve()
            if lp not in seen:
                out.append(IngestSource(path=lp, origin="drop", recursive=False,
                                        include=(), move=True))
        return out

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

    def extract_cfg(self, key: str):
        return self.data["extract"].get(key)

    def embed_cfg(self, key: str):
        return self.data["embed"].get(key)

    def mcp_cfg(self, key: str):
        return self.data["mcp"].get(key)

    def retrieval_cfg(self, key: str):
        return self.data["retrieval"].get(key)
