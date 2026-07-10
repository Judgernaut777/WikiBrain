"""The orchestrator: run the engines, aggregate, decide.

Order is fixed, and each step is here for a reason:

    1. normalize          — so a zero-width space cannot hide a lure
    2. run every engine   — recording *how* each one ended, not just what it found
    3. synthesize errors  — an engine that could not look becomes a `scanner_error`
                            finding, at a severity that depends on whether the
                            surface required it
    4. aggregate          — union across engines, dedup, preserve attribution
    5. decide             — strongest per-finding decision wins
    6. redact             — mask exactly those findings the policy mapped to
                            `redact`, with overlapping spans merged first

Step 3 is the one that matters. Every other scanner design tends to collapse
"detect-secrets is not installed" into "no secrets found", and the result is a
system that reports clean because it never looked. Here an engine's status is a
first-class output, a required engine that did not finish `ok` manufactures a
critical finding, and every policy maps that finding to something other than
`allow`.

One engine finding nothing never cancels another engine finding risk: findings are
unioned, and the decision is the strongest, never the average or the majority.

Zero model calls in this module. `prompt_guard` is the only engine that can run
one, it is off by default, and it is local.
"""
from __future__ import annotations

import json

from . import redaction
from .configuration import SafetyConfig
from .engines.base import EngineScanRequest
from .models import (CATEGORY_OF_CAPABILITY, Category, Decision, EngineOutcome,
                     EngineStatus, Finding, RiskLevel, SafetyResult, strongest)
from .normalize import normalize
from .policies import Policy, policy as get_policy
from .registry import build as build_engine

#: Engines are stateless with respect to a scan, and constructing `presidio` means
#: importing spaCy. Cache per configuration so a recall of eight claims builds them
#: once, not eight times.
_ENGINE_CACHE: dict = {}


def _cache_key(config: SafetyConfig) -> str:
    return json.dumps([[e.name, e.enabled, e.required, sorted(e.options.items(),
                                                              key=str)]
                       for e in config.engines], default=str, sort_keys=True)


def _engines(config: SafetyConfig):
    key = _cache_key(config)
    cached = _ENGINE_CACHE.get(key)
    if cached is None:
        cached = [(build_engine(s), s) for s in config.engines if s.enabled]
        _ENGINE_CACHE[key] = cached
    return cached


def _status_for(engine, settings, pol: Policy) -> EngineStatus | None:
    """The status an engine gets before it is asked to scan, or None to scan."""
    if not (engine.capabilities & pol.capabilities):
        return EngineStatus.skipped
    try:
        if not engine.available():
            return EngineStatus.unavailable
    except Exception:
        # `available()` must not raise. One that does is broken, not absent.
        return EngineStatus.failed
    return None


def _run_engine(engine, settings, pol: Policy, request: EngineScanRequest,
                required: bool) -> EngineOutcome:
    outcome = EngineOutcome(name=engine.name, version=str(engine.version),
                            status=EngineStatus.ok, required=required)
    pre = _status_for(engine, settings, pol)
    if pre is not None:
        outcome.status = pre
        return outcome
    try:
        outcome.findings = list(engine.scan(request))
    except TimeoutError as exc:
        outcome.status = EngineStatus.timeout
        outcome.error = str(exc)
    except Exception as exc:
        outcome.status = EngineStatus.failed
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


def _error_findings(outcomes: list[EngineOutcome]) -> list[Finding]:
    """Turn "did not look" into something the policy table can see.

    A *required* engine in any state but `ok` is critical: the surface asked for a
    guarantee it did not get. An *optional* engine that broke is low: the other
    engines did look, and a warning is the honest report. An optional engine that
    was disabled, unavailable, or skipped produces nothing — that is the expected,
    configured state of a lightweight install, not an anomaly.
    """
    out: list[Finding] = []
    for o in outcomes:
        if o.status is EngineStatus.ok:
            continue
        if o.required:
            out.append(Finding(
                engine=o.name, engine_version=o.version,
                kind=Category.scanner_error, rule=f"required_engine_{o.status.value}",
                severity=RiskLevel.critical,
                message=(f"required engine {o.name!r} did not scan "
                         f"({o.status.value}); content is unscanned, not clean"),
                metadata={"status": o.status.value,
                          **({"error": o.error} if o.error else {})}))
        elif o.broke:
            out.append(Finding(
                engine=o.name, engine_version=o.version,
                kind=Category.scanner_error, rule=f"engine_{o.status.value}",
                severity=RiskLevel.low,
                message=(f"optional engine {o.name!r} {o.status.value}; other "
                         "engines completed"),
                metadata={"status": o.status.value,
                          **({"error": o.error} if o.error else {})}))
    return out


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Drop exact repeats; keep every distinct engine's view.

    Two engines reporting the same span are two pieces of evidence, and both are
    retained so that attribution survives into the audit record. Only an identical
    (engine, kind, rule, span) tuple is a duplicate.
    """
    seen: set = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.engine, f.kind, f.rule, f.start, f.end)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _wanted_categories(pol: Policy) -> frozenset:
    return frozenset(CATEGORY_OF_CAPABILITY[c] for c in pol.capabilities)


def scan(text: str, *, surface: str, config: SafetyConfig,
         path=None, metadata: dict | None = None) -> SafetyResult:
    """Scan `text` for `surface`. Never raises for content; only for misconfiguration."""
    pol = get_policy(surface)
    original = text or ""

    if not config.enabled:
        return SafetyResult(surface=surface, decision=Decision.allow, text=original,
                            original_text=original,
                            notes=["safety scanning is disabled in configuration"])

    norm = normalize(original)
    scanned = norm.text
    notes = list(norm.notes)
    if len(scanned) > config.max_text_chars:
        scanned = scanned[:config.max_text_chars]
        notes.append(f"text truncated to {config.max_text_chars} characters "
                     "before scanning")

    request = EngineScanRequest(text=scanned, surface=surface,
                                capabilities=pol.capabilities, path=path,
                                metadata=metadata or {})

    built = {e.name: (e, s) for e, s in _engines(config)}

    outcomes: list[EngineOutcome] = []
    for settings in config.engines:
        required = settings.required or settings.name in pol.required_engines
        if settings.name not in built:
            # Disabled by configuration. Recorded, not omitted: a reader of the
            # audit record must be able to tell "switched off" from "found nothing".
            outcomes.append(EngineOutcome(name=settings.name, version="",
                                          required=required,
                                          status=EngineStatus.disabled))
            continue
        engine, _ = built[settings.name]
        outcomes.append(_run_engine(engine, settings, pol, request, required))

    # A policy-required engine absent from configuration entirely. `load()` refuses
    # required+disabled, but it cannot refuse a name that was never mentioned.
    seen = {o.name for o in outcomes}
    for name in pol.required_engines - seen:
        outcomes.append(EngineOutcome(name=name, version="", required=True,
                                      status=EngineStatus.disabled))

    wanted = _wanted_categories(pol)
    findings: list[Finding] = []
    for o in outcomes:
        findings.extend(f for f in o.findings if f.kind in wanted)
    findings.extend(_error_findings(outcomes))
    findings = _dedupe(findings)

    decision = strongest(pol.decide(f.kind, f.severity) for f in findings)

    to_mask = [f for f in findings if pol.decide(f.kind, f.severity) is Decision.redact]
    if to_mask:
        final = redaction.redact(scanned, to_mask)
    else:
        # Nothing to hide: hand back exactly what the caller gave us. Normalization
        # is a scanning aid, not a licence to rewrite the user's text.
        final = original

    return SafetyResult(surface=surface, decision=decision, text=final,
                        original_text=original, findings=findings,
                        engines=outcomes, notes=notes)


def clear_engine_cache() -> None:
    """Drop built engines. For tests that mutate configuration between scans."""
    _ENGINE_CACHE.clear()
