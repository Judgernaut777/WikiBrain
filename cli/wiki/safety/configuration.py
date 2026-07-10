"""Engine configuration, and the four states a config must be able to express.

`enabled` and `available` are different questions and the answer to neither is the
answer to the other:

    enabled + available    -> it will run
    enabled + unavailable  -> it wanted to run and could not. If required: refuse.
    disabled               -> it will not run, by choice
    required               -> if it does not run OK, the content is not clean

A missing *optional* tool must never crash a default install. A missing *required*
tool must never silently produce a clean result. Everything in this module exists
to keep those two sentences simultaneously true.

Read from `[safety]` in config.toml; see config.example.toml.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class SafetyConfigError(Exception):
    """A configuration that cannot be honoured. Raised at build, never at scan."""


#: Shipped defaults. Lightweight: the baseline plus one pure-Python engine that is
#: simply inert when its package is absent. Nothing here spawns a process, loads a
#: model, or touches the network.
DEFAULTS: dict = {
    "enabled": True,
    #: Text longer than this is truncated before scanning, and a note is recorded.
    #: A memory claim is a sentence; a source document is not, and one pathological
    #: ingest should not stall a promotion.
    "max_text_chars": 200_000,
    "engines": {
        "baseline": {"enabled": True, "required": True},
        "detect_secrets": {"enabled": True, "required": False},
        "trufflehog": {
            "enabled": False, "required": False, "executable": "trufflehog",
            "timeout_seconds": 20.0, "allow_network_verification": False,
        },
        "gitleaks": {
            "enabled": False, "required": False, "executable": "gitleaks",
            "timeout_seconds": 20.0,
        },
        "presidio": {"enabled": False, "required": False, "score_threshold": 0.5},
        "prompt_guard": {
            "enabled": False, "required": False, "model": "", "revision": "",
            "threshold": 0.5, "allow_download": False,
        },
    },
}

#: Options the pipeline consumes itself; everything else is passed to the engine's
#: constructor as a keyword argument.
_META_KEYS = frozenset({"enabled", "required"})


@dataclass(frozen=True)
class EngineSettings:
    name: str
    enabled: bool = True
    required: bool = False
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SafetyConfig:
    enabled: bool = True
    max_text_chars: int = DEFAULTS["max_text_chars"]
    engines: tuple = ()

    def engine(self, name: str) -> EngineSettings | None:
        return next((e for e in self.engines if e.name == name), None)

    @property
    def required_names(self) -> frozenset:
        return frozenset(e.name for e in self.engines if e.required)


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(raw: dict | None = None) -> SafetyConfig:
    """Build a `SafetyConfig` from the `[safety]` table. Validates eagerly.

    An unknown engine name is an error rather than a shrug: a typo in
    `detct_secrets` that quietly disabled secret scanning would be the worst
    possible failure, because everything downstream keeps reporting clean.
    """
    # Read the live registry, not a snapshot: a test that injects a fake engine
    # must be configurable through the same path as a real one.
    from .registry import ENGINE_FACTORIES  # local: registry imports this module

    data = _merge(DEFAULTS, raw or {})
    engines = []
    for name, opts in (data.get("engines") or {}).items():
        if name not in ENGINE_FACTORIES:
            raise SafetyConfigError(
                f"unknown safety engine {name!r}; known engines: "
                f"{', '.join(sorted(ENGINE_FACTORIES))}")
        if not isinstance(opts, dict):
            raise SafetyConfigError(f"[safety.engines.{name}] must be a table")
        enabled = bool(opts.get("enabled", True))
        required = bool(opts.get("required", False))
        if required and not enabled:
            raise SafetyConfigError(
                f"safety engine {name!r} is required but disabled; a required "
                "engine that never runs cannot certify anything as clean. "
                "Enable it, or drop `required`.")
        engines.append(EngineSettings(
            name=name, enabled=enabled, required=required,
            options={k: v for k, v in opts.items() if k not in _META_KEYS}))

    if not data.get("enabled", True):
        return SafetyConfig(enabled=False, engines=tuple(engines),
                            max_text_chars=int(data["max_text_chars"]))

    baseline = next((e for e in engines if e.name == "baseline"), None)
    if baseline is None or not baseline.enabled:
        raise SafetyConfigError(
            "the `baseline` engine cannot be disabled while safety is enabled; "
            "it is the floor that makes an offline install safe. Set "
            "`[safety] enabled = false` if you mean to turn scanning off entirely.")

    return SafetyConfig(enabled=True, engines=tuple(engines),
                        max_text_chars=int(data["max_text_chars"]))


def from_repo(repo) -> SafetyConfig:
    return load(repo.cfg.data.get("safety"))
