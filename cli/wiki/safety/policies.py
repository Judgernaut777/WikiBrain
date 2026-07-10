"""Policy: what WikiBrain does about what an engine saw.

This is the WikiBrain-owned half of the design, and the half no third-party tool
gets a vote in. An engine says *there is a probable AWS key at offset 40*. Policy
says *therefore this candidate is stored with that span masked, and this promotion
is refused.*

Policy is data. Each surface is a table from `(kind, severity)` to a `Decision`,
plus the set of capabilities that surface wants looked for. A pair absent from the
table means `allow`, which is safe only because `scanner_error` is present in every
table at every severity above `low`.

Three surfaces are live. `source_ingest` and `obsidian_projection` are named in
docs/SAFETY.md as future work and are deliberately **not** defined here: asking for
an undefined policy raises rather than defaulting to something permissive.

Two invariants hold across every table:

  * **No `scanner_error` maps to `allow`.** An engine that could not look has not
    found the content clean.
  * **`secret` never maps to `allow` or `warn` above `low`.** A credential is
    masked at capture, masked at recall, and blocks a promotion.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Capability, Category, Decision, RiskLevel

MEMORY_CANDIDATE = "memory_candidate"
MEMORY_RECALL = "memory_recall"
MEMORY_PROMOTION = "memory_promotion"

#: Named, specified in docs/SAFETY.md, and not yet implemented. Listed so that a
#: caller who reaches for one gets a clear error instead of a permissive default.
DEFERRED_SURFACES = ("source_ingest", "obsidian_projection")

_ALL = RiskLevel.low, RiskLevel.medium, RiskLevel.high, RiskLevel.critical


def _row(**kw) -> dict:
    return {RiskLevel(k): v for k, v in kw.items()}


def _uniform(decision: Decision) -> dict:
    return {risk: decision for risk in _ALL}


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    name: str
    #: What to look for here. An engine offering none of these is `skipped`.
    capabilities: frozenset
    rules: dict
    #: Engines that must finish `ok` on this surface. Config may add more; it may
    #: not remove these.
    required_engines: frozenset = frozenset({"baseline"})

    def decide(self, kind: Category, severity: RiskLevel) -> Decision:
        return self.rules.get(kind, {}).get(severity, Decision.allow)


#: Capture. A secret is masked before it can reach the inbox artifact or the
#: candidate row; injection and tool-control payloads are quarantined so a human
#: sees them flagged rather than promoting them by reflex.
_CANDIDATE = Policy(
    name=MEMORY_CANDIDATE,
    capabilities=frozenset({
        Capability.secrets, Capability.pii, Capability.prompt_injection,
        Capability.tool_control, Capability.encoded_content}),
    rules={
        Category.secret: _uniform(Decision.redact),
        Category.pii: _row(low=Decision.warn, medium=Decision.redact,
                           high=Decision.redact, critical=Decision.redact),
        Category.prompt_injection: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.quarantine, critical=Decision.quarantine),
        Category.tool_instruction: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.quarantine, critical=Decision.quarantine),
        Category.encoding: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.quarantine, critical=Decision.quarantine),
        Category.scanner_error: _row(
            low=Decision.warn, medium=Decision.quarantine,
            high=Decision.quarantine, critical=Decision.quarantine),
    },
)

#: Recall. Whole-file scanners are excluded by capability, not by name: a promoted
#: claim is a sentence, and spawning TruffleHog per item would make recall a
#: process-spawn loop. Everything cheap is looked for, because recall is the moment
#: stored text reaches a model — the one place a tool-control directive planted in
#: memory pays off. A secret in a trusted claim is masked on the way out; the claim
#: stays trusted, because trust is about authority, not about exposure.
_RECALL = Policy(
    name=MEMORY_RECALL,
    capabilities=frozenset({
        Capability.secrets, Capability.pii, Capability.prompt_injection,
        Capability.tool_control, Capability.encoded_content}),
    rules={
        Category.secret: _uniform(Decision.redact),
        Category.pii: _row(low=Decision.warn, medium=Decision.redact,
                           high=Decision.redact, critical=Decision.redact),
        Category.prompt_injection: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.quarantine, critical=Decision.quarantine),
        Category.tool_instruction: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.quarantine, critical=Decision.quarantine),
        Category.encoding: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.quarantine, critical=Decision.quarantine),
        Category.scanner_error: _row(
            low=Decision.warn, medium=Decision.quarantine,
            high=Decision.quarantine, critical=Decision.quarantine),
    },
)

#: Promotion. The expensive surface, and the only one that runs whole-file
#: scanners: it happens once, a human is present, and it is the moment content
#: becomes trusted. Blocking is cheap here and irreversible later.
_PROMOTION = Policy(
    name=MEMORY_PROMOTION,
    capabilities=frozenset({
        Capability.secrets, Capability.pii, Capability.prompt_injection,
        Capability.tool_control, Capability.encoded_content,
        Capability.source_or_repository_secrets}),
    rules={
        Category.secret: _row(low=Decision.warn, medium=Decision.block,
                              high=Decision.block, critical=Decision.block),
        Category.pii: _uniform(Decision.warn),
        Category.prompt_injection: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.block, critical=Decision.block),
        Category.tool_instruction: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.block, critical=Decision.block),
        Category.encoding: _row(
            low=Decision.warn, medium=Decision.warn,
            high=Decision.block, critical=Decision.block),
        Category.scanner_error: _row(
            low=Decision.warn, medium=Decision.block,
            high=Decision.block, critical=Decision.block),
    },
)

POLICIES = {p.name: p for p in (_CANDIDATE, _RECALL, _PROMOTION)}

SURFACES = tuple(POLICIES)


def policy(name: str) -> Policy:
    """The policy for `name`. Raises on anything unknown — never guesses."""
    try:
        return POLICIES[name]
    except KeyError:
        if name in DEFERRED_SURFACES:
            raise PolicyError(
                f"surface {name!r} is specified but not implemented "
                "(see docs/SAFETY.md); it has no policy and must not be scanned "
                "as though it did") from None
        raise PolicyError(
            f"unknown safety surface {name!r}; known: {', '.join(SURFACES)}") from None
