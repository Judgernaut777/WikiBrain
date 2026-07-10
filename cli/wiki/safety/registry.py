"""Which engines exist, and how to build one.

Engines are named here and nowhere else. A name that is not in `ENGINE_FACTORIES`
is a configuration error, not a silently-skipped engine — see
`configuration.load()`.

GLiNER is **absent on purpose**. There is no `gliner` key, because there is no
adapter: naming it here would let someone write `gliner.enabled = true`, get an
engine that never runs, and believe their PII coverage improved. Deferred work is
recorded in docs/SAFETY.md, not in the registry.
"""
from __future__ import annotations

from .configuration import EngineSettings
from .engines.base import SafetyEngine


def _baseline(**kw):
    from .engines.baseline import BaselineEngine
    return BaselineEngine(**kw)


def _detect_secrets(**kw):
    from .engines.detect_secrets import DetectSecretsEngine
    return DetectSecretsEngine(**kw)


def _trufflehog(**kw):
    from .engines.trufflehog import TruffleHogEngine
    return TruffleHogEngine(**kw)


def _gitleaks(**kw):
    from .engines.gitleaks import GitleaksEngine
    return GitleaksEngine(**kw)


def _presidio(**kw):
    from .engines.presidio import PresidioEngine
    return PresidioEngine(**kw)


def _prompt_guard(**kw):
    from .engines.prompt_guard import PromptGuardEngine
    return PromptGuardEngine(**kw)


#: name -> factory. Factories import lazily so that a heavy optional dependency is
#: never imported by `import wiki`.
ENGINE_FACTORIES = {
    "baseline": _baseline,
    "detect_secrets": _detect_secrets,
    "trufflehog": _trufflehog,
    "gitleaks": _gitleaks,
    "presidio": _presidio,
    "prompt_guard": _prompt_guard,
}

ENGINE_NAMES = frozenset(ENGINE_FACTORIES)


class EngineBuildError(Exception):
    pass


def build(settings: EngineSettings) -> SafetyEngine:
    """Construct one engine. A constructor that raises is a build error.

    Constructors must not raise merely because their dependency is missing — that
    is what `available()` reports. They raise when the *configuration* is wrong.
    """
    factory = ENGINE_FACTORIES.get(settings.name)
    if factory is None:
        raise EngineBuildError(f"unknown safety engine {settings.name!r}")
    try:
        return factory(**settings.options)
    except TypeError as exc:
        raise EngineBuildError(
            f"safety engine {settings.name!r} rejected its options "
            f"({', '.join(sorted(settings.options)) or 'none'}): {exc}") from exc


def build_all(config) -> list[tuple[SafetyEngine, EngineSettings]]:
    """Every enabled engine, in registry order so a scan is deterministic."""
    out = []
    for name in ENGINE_FACTORIES:
        settings = config.engine(name)
        if settings is None or not settings.enabled:
            continue
        out.append((build(settings), settings))
    return out
