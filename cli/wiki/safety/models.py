"""The safety vocabulary: findings, engine outcomes, decisions.

Detection and policy are separate concerns and this module holds neither. It holds
the words they use to talk to each other, so that an engine can say *what it saw*
without saying *what should happen*, and a policy can say what should happen
without knowing which engine looked.

Two distinctions in here carry the whole design:

  * **A finding is not a decision.** `Finding` records `kind` and `severity`.
    Nothing in an engine may set a `Decision`; only `policies.Policy` maps the
    pair to one.
  * **"Found nothing" is not "did not look."** See `EngineStatus`. Collapsing
    those two states is how a scanner starts reporting unread content as clean.

Nothing here — not a `Finding`, not an `EngineOutcome`, not a `SafetyResult` —
ever carries the matched text. A span locates a secret; it does not copy it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Category(str, Enum):
    """What kind of risk a finding describes."""
    secret = "secret"
    pii = "pii"
    prompt_injection = "prompt_injection"
    tool_instruction = "tool_instruction"
    encoding = "encoding"
    #: Not a property of the content. An engine that was asked to look and could
    #: not. Policy must never map this to `allow`.
    scanner_error = "scanner_error"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


_RISK_ORDER = {RiskLevel.low: 0, RiskLevel.medium: 1,
               RiskLevel.high: 2, RiskLevel.critical: 3}


def highest(risks) -> RiskLevel:
    """The strongest risk in `risks`, or `low` if empty."""
    return max(risks, key=lambda r: _RISK_ORDER[r], default=RiskLevel.low)


class Decision(str, Enum):
    """What policy says to do. Ordered: later strictly dominates earlier."""
    allow = "allow"
    warn = "warn"
    redact = "redact"
    quarantine = "quarantine"
    block = "block"


_DECISION_ORDER = {Decision.allow: 0, Decision.warn: 1, Decision.redact: 2,
                   Decision.quarantine: 3, Decision.block: 4}


def strongest(decisions) -> Decision:
    """The most restrictive decision in `decisions`, or `allow` if empty."""
    return max(decisions, key=lambda d: _DECISION_ORDER[d], default=Decision.allow)


def at_least(a: Decision, b: Decision) -> bool:
    """True iff `a` is at least as restrictive as `b`."""
    return _DECISION_ORDER[a] >= _DECISION_ORDER[b]


class Capability(str, Enum):
    """What an engine can detect. A surface asks for capabilities, not engines."""
    secrets = "secrets"
    pii = "pii"
    prompt_injection = "prompt_injection"
    tool_control = "tool_control"
    encoded_content = "encoded_content"
    #: Whole-file/repository scanning. Deliberately distinct from `secrets`: it is
    #: how a policy declines to run TruffleHog against every eight-word recall item.
    source_or_repository_secrets = "source_or_repository_secrets"


CATEGORY_OF_CAPABILITY: dict[Capability, Category] = {
    Capability.secrets: Category.secret,
    Capability.source_or_repository_secrets: Category.secret,
    Capability.pii: Category.pii,
    Capability.prompt_injection: Category.prompt_injection,
    Capability.tool_control: Category.tool_instruction,
    Capability.encoded_content: Category.encoding,
}


class EngineStatus(str, Enum):
    """Six states. They are not interchangeable.

    * `ok` — the engine ran. It may have found nothing; that is a *result*.
    * `disabled` — switched off in configuration. It never looked, by choice.
    * `unavailable` — no import, no binary, no model. It never looked, by absence.
    * `skipped` — it has no capability this surface asked for. Correctly idle.
    * `failed` — it looked, it raised, and we do not know what it saw.
    * `timeout` — an external tool exceeded its budget. A `failed` with a cause.

    Only `ok` licenses the sentence "this content was scanned for X."
    """
    ok = "ok"
    disabled = "disabled"
    unavailable = "unavailable"
    skipped = "skipped"
    failed = "failed"
    timeout = "timeout"


#: An engine in one of these states did not look. If it was required, the content
#: is unscanned, and unscanned is not clean.
DID_NOT_LOOK = (EngineStatus.disabled, EngineStatus.unavailable,
                EngineStatus.skipped, EngineStatus.failed, EngineStatus.timeout)

#: It looked, and it broke. Distinct from never having looked at all.
BROKE = (EngineStatus.failed, EngineStatus.timeout)


@dataclass(frozen=True)
class Finding:
    """One normalized observation. Engine-agnostic by construction.

    `span` is half-open `[start, end)` into the scanned text. `(0, 0)` means "no
    span": a whole-text classifier score can be warned about, but never redacted.
    `metadata` never contains the matched value.
    """
    engine: str
    engine_version: str
    kind: Category
    rule: str
    severity: RiskLevel
    message: str
    confidence: float = 1.0
    start: int = 0
    end: int = 0
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def has_span(self) -> bool:
        return self.end > self.start

    def as_dict(self) -> dict:
        d = {"engine": self.engine, "engine_version": self.engine_version,
             "kind": self.kind.value, "rule": self.rule,
             "severity": self.severity.value, "confidence": round(self.confidence, 4),
             "message": self.message}
        if self.has_span:
            d["span"] = [self.start, self.end]
        if self.metadata:
            d["metadata"] = self.metadata
        return d


@dataclass
class EngineOutcome:
    """What one engine did on one surface. The audit record of an attempt to look."""
    name: str
    version: str
    status: EngineStatus
    required: bool = False
    findings: list[Finding] = field(default_factory=list)
    error: str = ""

    @property
    def looked(self) -> bool:
        return self.status is EngineStatus.ok

    @property
    def broke(self) -> bool:
        return self.status in BROKE

    def as_dict(self) -> dict:
        d = {"engine": self.name, "version": self.version,
             "status": self.status.value, "required": self.required,
             "findings": len(self.findings)}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class SafetyResult:
    """The pipeline's answer for one piece of text on one surface."""
    surface: str
    decision: Decision
    #: The text a caller should use. Identical to the input unless a finding was
    #: mapped to `redact`, in which case it is normalized and masked.
    text: str
    original_text: str
    findings: list[Finding] = field(default_factory=list)
    engines: list[EngineOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.decision is Decision.allow

    @property
    def redacted(self) -> bool:
        return self.text != self.original_text

    def has(self, kind: Category) -> bool:
        return any(f.kind is kind for f in self.findings)

    @property
    def scanner_failed(self) -> bool:
        return any(o.broke for o in self.engines)

    def kinds(self) -> list[str]:
        seen = {f.kind.value: None for f in self.findings}
        return list(seen)

    def engines_by_status(self, *statuses: EngineStatus) -> list[str]:
        return [o.name for o in self.engines if o.status in statuses]

    def summary(self) -> dict:
        """An audit-safe record. Contains no matched text, ever.

        This is what gets persisted into candidate metadata and echoed in recall.
        """
        return {
            "surface": self.surface,
            "decision": self.decision.value,
            "kinds": self.kinds(),
            "redacted": self.redacted,
            "findings": [f.as_dict() for f in self.findings],
            "engines": [o.as_dict() for o in self.engines],
            **({"notes": self.notes} if self.notes else {}),
        }

    def reason(self) -> str:
        """One line, for a warning or an error message. No matched text."""
        if self.clean:
            return "clean"
        bits = []
        for kind in self.kinds():
            hits = [f for f in self.findings if f.kind.value == kind]
            worst = highest([f.severity for f in hits]).value
            engines = sorted({f.engine for f in hits})
            bits.append(f"{kind} ({worst}, via {'+'.join(engines)})")
        return "; ".join(bits)
