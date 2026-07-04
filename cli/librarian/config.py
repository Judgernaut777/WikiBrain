"""Librarian config: the `[librarian]` table of config.toml.

Reuses the wiki CLI's root/config discovery so both halves read one file. The
API key is NEVER stored in config — `api_key_env` names an environment variable
that holds it, keeping the repo key-free (the `wiki lint` secret scan applies).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from wiki.config import Config

DEFAULTS = {
    "auto_extract": False,  # ingest commands spawn extraction for new sources
    "base_url": "http://localhost:11434/v1",  # any OpenAI-compatible endpoint (Ollama default)
    "api_key_env": "",      # NAME of the env var holding the key; "" = no key (local)
    "model": "",            # default model for every task (required to run)
    "models": {},           # per-task overrides: extract = "...", triage = "...", ...
    "max_source_chars": 24000,  # truncate raw source text sent to the model
    "timeout": 180,         # seconds per model call
    "temperature": 0.2,
    "retries": 1,           # re-ask attempts when output fails the contract
    # Output token cap per call. Sent as max_tokens; 0/None omits it (server
    # default). Reasoning models (e.g. Ornith) spend tokens on a <think> preamble
    # before the JSON, so they need generous headroom — keep this >= ~2048 for
    # those, or they truncate before emitting the answer.
    "max_tokens": 4096,
}

# Tasks the router knows about today. `extract` is implemented; the others are
# reserved so configs written now keep working as passes are added.
TASKS = ("extract", "triage", "adjudicate", "synthesize")


class LibrarianConfigError(SystemExit):
    pass


@dataclass
class LibrarianConfig:
    root: object
    data: dict

    @classmethod
    def load(cls, start=None) -> "LibrarianConfig":
        cfg = Config.load(start)
        merged = dict(DEFAULTS)
        user = cfg.data.get("librarian", {})
        for k, v in user.items():
            if k == "models" and isinstance(v, dict):
                merged["models"] = dict(v)
            else:
                merged[k] = v
        return cls(root=cfg.root, data=merged)

    def get(self, key: str):
        return self.data.get(key, DEFAULTS.get(key))

    @property
    def enabled(self) -> bool:
        return bool(self.get("auto_extract"))

    def model_for(self, task: str) -> str:
        model = self.data.get("models", {}).get(task) or self.get("model")
        if not model:
            raise LibrarianConfigError(
                "error: no model configured — set [librarian] model (or a per-task "
                "override under [librarian.models]) in config.toml")
        return model

    def api_key(self) -> str | None:
        env = self.get("api_key_env")
        if not env:
            return None
        key = os.environ.get(env)
        if not key:
            raise LibrarianConfigError(
                f"error: [librarian] api_key_env names {env!r} but that environment "
                "variable is not set")
        return key
