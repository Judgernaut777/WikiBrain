"""WikiBrain-local memory safety.

WikiBrain owns *when* content is scanned and *what happens* when something is
found. It does not own detection: engines are modular, and the good ones are
maintained by other people. `docs/SAFETY.md` is the public contract.

The separation this package exists to enforce:

    trusted does not mean safe to expose
    safe    does not mean trusted

`trusted` is a statement about authority — a human promoted this claim, from this
source, in this scope, and nothing contradicts it. It says nothing about whether
the text contains an API key. Conversely, a scanner returning clean says nothing
about whether the claim is true, and **no engine and no policy in this package may
ever set `trusted = True`.** Safety can withhold, mask, or block. It cannot vouch.

Three surfaces are enforced today: `memory_candidate` (capture),
`memory_recall` (read), and `memory_promotion` (the human gate). Source ingest and
the Obsidian projection are specified as future work and have no policy, so asking
to scan them raises rather than silently allowing.

No engine in the default configuration makes a network call, spawns a process, or
loads a model. Zero model calls in the deterministic core CLI is unchanged.
"""
from __future__ import annotations

from .configuration import (SafetyConfig, SafetyConfigError, from_repo, load)
from .models import (Category, Decision, EngineOutcome, EngineStatus, Finding,
                     RiskLevel, SafetyResult, at_least)
from .pipeline import clear_engine_cache, scan
from .policies import (MEMORY_CANDIDATE, MEMORY_PROMOTION, MEMORY_RECALL,
                       PolicyError, SURFACES, policy)
from .registry import ENGINE_NAMES

__all__ = [
    "Category", "Decision", "EngineOutcome", "EngineStatus", "Finding",
    "RiskLevel", "SafetyResult", "SafetyConfig", "SafetyConfigError",
    "MEMORY_CANDIDATE", "MEMORY_PROMOTION", "MEMORY_RECALL", "SURFACES",
    "ENGINE_NAMES", "PolicyError",
    "at_least", "clear_engine_cache", "from_repo", "health", "load", "policy",
    "scan", "scan_for",
]


def scan_for(repo, text: str, surface: str, **kw) -> SafetyResult:
    """Scan `text` for `surface`, reading configuration from `repo`.

    The one call site the rest of WikiBrain uses. Everything else in this package
    is an implementation detail.
    """
    return scan(text, surface=surface, config=from_repo(repo), **kw)


def health(repo) -> dict:
    """Which engines are configured, and which of them could actually run.

    `enabled` and `available` are reported separately on purpose. An engine that is
    enabled and unavailable is the single most misleading state a scanner can be
    in, and a health check that collapses the two hides it.
    """
    from .registry import build as _build

    config = from_repo(repo)
    engines = []
    for settings in config.engines:
        entry = {"engine": settings.name, "enabled": settings.enabled,
                 "required": settings.required}
        if settings.enabled:
            try:
                engine = _build(settings)
                entry["available"] = bool(engine.available())
                entry["version"] = str(engine.version)
            except Exception as exc:  # a build error, not a scan error
                entry["available"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
        else:
            entry["available"] = None
        engines.append(entry)

    unmet = [e["engine"] for e in engines
             if e["required"] and not e.get("available")]
    return {
        "enabled": config.enabled,
        "surfaces": list(SURFACES),
        "engines": engines,
        #: True only if every required engine can run. False means promotion and
        #: recall will fail closed, which is correct but worth knowing about.
        "ok": config.enabled and not unmet,
        **({"required_engines_unavailable": unmet} if unmet else {}),
    }
