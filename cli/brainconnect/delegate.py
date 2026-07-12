"""The delegation trigger (ADR 0008 Lane 4).

BrainConnect assembles a routing/placement request from **trusted** capability
claims (the Lane-1 registry `trusted_view`) plus a workload spec, CALLS the two
engines that own the math — AgentConnect's `RoutingEngine.route` and
ComputeConnect's `POST /route/estimate` — as a client, and RECORDS the returned
decision + rationale as ordinary BrainConnect decision-provenance memory.

What this module is NOT:

* It contains **zero routing/placement/scheduler/residency math.** No
  `select_placement` clone, no capability scoring, no queue-seconds, no
  warm/residency state. Those are delegated (ADR 0008 binding prohibitions).
* It never **promotes** the recorded decision. Provenance is filed as an ordinary
  PENDING memory candidate — never auto-trusted, never self-promoted (LEDGER_SPEC
  §2). A hostile or malformed engine response therefore can never make
  BrainConnect record something as *trusted*.
* It never **widens** privacy. The request to each engine, and any fallback, is
  clamped to a floor no more permissive than the workload's declared tier; a
  response that would send privacy-restricted work off-box is rejected, not
  obeyed.

No single point of failure: if AgentConnect or ComputeConnect is unavailable,
times out, errors, or returns malformed data, the trigger does not crash — it
emits a deterministic, safe **fallback** decision (defer; never external) tagged
with an explicit fallback reason, and records that as provenance too. With both
engines down BrainConnect still fully functions.

Pure code, zero model calls.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .db import Repo
from . import api, ingest, registry, candidates
from .delegate_clients import (
    AGENTCONNECT, COMPUTECONNECT, DelegationClientError,
    RoutingClient, EstimateClient,
)

# ---------------------------------------------------------------------------
# Privacy — the never-widen floor.
#
# BrainConnect's canonical workload privacy vocabulary is a byte-for-byte mirror
# of AgentConnect's `core.models.PRIVACY_STRICTNESS` (which ComputeConnect also
# mirrors): loosest `public` = 0 … strictest `secret_sensitive` = 4. BC clamps
# the workload tier to this scale, fails CLOSED on anything unknown (→ most
# restrictive), and maps DOWNSTREAM to each engine's own vocabulary such that the
# emitted value is never looser than the workload asked for. BC invents no new
# privacy policy; it only refuses to widen.
# ---------------------------------------------------------------------------
PRIVACY_STRICTNESS: dict[str, int] = {
    "public": 0,
    "public_redacted": 1,
    "repo_sensitive": 2,
    "local_only": 3,
    "secret_sensitive": 4,
}
#: Absent/unknown/garbage tier → the strictest tier (fail closed), mirroring
#: ComputeConnect's `MOST_RESTRICTIVE_TIER`.
MOST_RESTRICTIVE_TIER = "secret_sensitive"
#: Only these tiers may ever leave the box (mirrors CC `CLOUD_PERMITTING_TIERS`).
CLOUD_PERMITTING_TIERS = frozenset({"public", "public_redacted"})

#: Map the canonical tier onto AgentConnect's `PrivacyClass` vocabulary
#: (public/low_sensitive/repo_sensitive/secret_sensitive/restricted). Each target
#: is chosen to be NO LOOSER than the source tier — `public_redacted`→`low_sensitive`
#: and `local_only`→`restricted` round toward the stricter side, never the looser.
_AC_PRIVACY_CLASS: dict[str, str] = {
    "public": "public",
    "public_redacted": "low_sensitive",
    "repo_sensitive": "repo_sensitive",
    "local_only": "restricted",
    "secret_sensitive": "secret_sensitive",
}

#: The AgentConnect `RoutingDecision.decision` values that place work OFF the
#: local box, tagged with WHICH caller ceiling gates each. BC rejects any of these
#: when the privacy floor forbids external OR the caller ceiling forbids it.
#:   "paid"   → a cloud provider (gated by allow_paid AND allow_external)
#:   "rented" → a rented node    (gated by allow_rented AND allow_external)
_OFFBOX_DECISION_KIND: dict[str, str] = {
    "route_to_cloud_provider": "paid",
    "route_to_rented_node": "rented",
}
#: Back-compat alias: the set of off-box decision values.
_OFFBOX_DECISIONS = frozenset(_OFFBOX_DECISION_KIND)

#: ComputeConnect `placement_class` values that indicate an OFF-BOX target, tagged
#: with the gating ceiling. Only "local"/"local_resident" stay on-box.
_OFFBOX_PLACEMENT_KIND: dict[str, str] = {
    "cloud": "paid",
    "rented": "rented",
    "external": "external",
    "remote": "external",
}
#: Substrings in a CC estimate's `runtime`/`provider_id` that betray an off-box
#: (cloud/rented/external) target even if `placement_class` were spoofed to look
#: local. A secondary, defense-in-depth signal — the local runtime is llama.cpp.
_OFFBOX_RUNTIME_TOKENS = (
    "cloud", "rented", "remote", "external", "openai", "anthropic",
    "azure", "bedrock", "vertex", "gemini", "together", "fireworks",
    "groq", "mistral-api", "hosted",
)


def _routing_offbox_kind(routing: dict) -> str | None:
    """Classify an AC `RoutingDecision` as off-box: 'paid' | 'rented' | None."""
    return _OFFBOX_DECISION_KIND.get(routing.get("decision"))


def _estimate_offbox_kind(estimate: dict) -> str | None:
    """Classify a CC estimate's placement as off-box: 'paid'|'rented'|'external'|None.

    Primary signal is `reason.placement_class`; a `runtime`/`provider_id` that
    names a cloud/rented/external engine is a secondary signal so a spoofed
    `placement_class` cannot smuggle privacy-restricted work off-box."""
    reason = estimate.get("reason")
    reason = reason if isinstance(reason, dict) else {}
    pc = str(reason.get("placement_class") or "").strip().lower()
    kind = _OFFBOX_PLACEMENT_KIND.get(pc)
    if kind is not None:
        return kind
    hint = " ".join(str(v or "").lower() for v in (
        estimate.get("runtime"), reason.get("provider_id"),
        reason.get("provider"), reason.get("runtime")))
    if any(tok in hint for tok in _OFFBOX_RUNTIME_TOKENS):
        return "external"
    return None


def _offbox_rejection(kind: str | None, priv: dict, ceilings: dict) -> str | None:
    """Return a rejection reason if an off-box `kind` violates the privacy floor
    or a caller ceiling, else None. BC derives the verdict ONLY from its own floor
    and the caller's forwarded ceilings — never from the engine's response."""
    if kind is None:
        return None
    if not priv["cloud_permitted"]:
        return (f"privacy floor {priv['effective']!r} forbids off-box placement")
    if not ceilings.get("allow_external"):
        return "caller ceiling allow_external=False forbids off-box placement"
    if kind == "rented" and not ceilings.get("allow_rented"):
        return "caller ceiling allow_rented=False forbids a rented node"
    if kind == "paid" and not ceilings.get("allow_paid"):
        return "caller ceiling allow_paid=False forbids a paid provider"
    return None

#: The full closed vocabulary of `RoutingDecision.decision` (recon). A response
#: whose `decision` is outside this set is malformed → fallback.
_AC_DECISIONS = frozenset({
    "route_to_local_resident_model", "route_to_local_after_switch",
    "route_to_rented_node", "route_to_cloud_provider",
    "no_eligible_provider", "blocked_secret_sensitive",
})


@dataclass(frozen=True)
class ResolvedPrivacy:
    declared: str | None
    effective: str
    assumed: bool
    cloud_permitted: bool

    def as_dict(self) -> dict:
        return {"declared": self.declared, "effective": self.effective,
                "assumed": self.assumed, "cloud_permitted": self.cloud_permitted}


def resolve_privacy(tier: object) -> ResolvedPrivacy:
    """Clamp a workload-declared tier to the canonical scale, failing closed.

    Absent / non-string / unknown → `secret_sensitive`, `assumed=True`. A known
    tier passes through. This is the SINGLE source of the privacy floor; every
    request and every fallback derives from it, never from an engine response.
    """
    if not isinstance(tier, str) or not tier.strip():
        return ResolvedPrivacy(
            declared=(tier if isinstance(tier, str) else None),
            effective=MOST_RESTRICTIVE_TIER, assumed=True, cloud_permitted=False)
    t = tier.strip()
    if t not in PRIVACY_STRICTNESS:
        return ResolvedPrivacy(declared=t, effective=MOST_RESTRICTIVE_TIER,
                               assumed=True, cloud_permitted=False)
    return ResolvedPrivacy(declared=t, effective=t, assumed=False,
                           cloud_permitted=t in CLOUD_PERMITTING_TIERS)


# ---------------------------------------------------------------------------
# The workload spec — what the caller wants done. BC adds NO scoring/eligibility
# to this; it is forwarded (privacy-clamped) to the engines that own that math.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkloadSpec:
    task_id: str
    capability_class: str                 # a registry tier name (e.g. high-capability-local)
    privacy_tier: str = MOST_RESTRICTIVE_TIER
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    priority: str = "normal"
    quality: str = "good_enough"
    latency_preference: str = "normal"
    #: Caller ceilings on where work may go. BC only ever ANDs these with the
    #: privacy floor — it can tighten them, never loosen them.
    allow_external: bool = False
    allow_paid: bool = False
    allow_rented: bool = False
    #: Extra capabilities to require beyond the tier's structural ones.
    extra_capabilities: tuple[str, ...] = ()
    #: If True and the tier has a TRUSTED registry model, pin it as the exact
    #: model in both requests (assembled only from a trusted claim).
    pin_registry_model: bool = False
    profile: str | None = None

    @staticmethod
    def from_dict(d: dict) -> "WorkloadSpec":
        d = dict(d or {})
        if "task_id" not in d or not str(d.get("task_id") or "").strip():
            raise DelegateError("workload requires a task_id")
        if "capability_class" not in d or not str(d.get("capability_class") or "").strip():
            raise DelegateError("workload requires a capability_class (a registry tier)")
        known = WorkloadSpec.__dataclass_fields__
        unknown = set(d) - set(known)
        if unknown:
            raise DelegateError(f"unknown workload fields: {', '.join(sorted(unknown))}")
        if "extra_capabilities" in d:
            d["extra_capabilities"] = tuple(d["extra_capabilities"])
        return WorkloadSpec(**d)


class DelegateError(Exception):
    """A caller-side problem (a malformed workload). NOT an engine outage — an
    engine outage is a deterministic fallback, never an exception."""


# ---------------------------------------------------------------------------
# Assembly — trusted registry claims ⊕ workload → the two engine requests.
# ---------------------------------------------------------------------------
def _trusted_tier(repo: Repo, capability_class: str) -> tuple[dict | None, str | None]:
    """Return `(tier_structure, trusted_model)` for a capability class.

    Structure (ordinal / required capabilities / provider) is registry DATA and is
    always present. `trusted_model` is the tier's preferred model ONLY when a human
    has promoted it (from `registry.trusted_view`, which excludes pending/squatted
    facts); otherwise None. BC assembles requirements from trusted claims only.
    """
    view = registry.trusted_view(repo)
    tier = next((t for t in view["tiers"] if t["tier"] == capability_class), None)
    if tier is None:
        return None, None
    model = None
    pref = tier.get("preferred_model")
    if pref and pref.get("trusted") and pref.get("model"):
        model = pref["model"]
    else:
        dep = tier.get("deployed_model")
        if dep and dep.get("trusted") and dep.get("model"):
            model = dep["model"]
    return tier, model


def assemble_request(repo: Repo, workload: WorkloadSpec) -> dict:
    """Build the AgentConnect `RoutingContext` and ComputeConnect estimate body.

    Requirements come from the TRUSTED registry (structural capabilities + any
    human-promoted model); sizing/priority/privacy come from the workload. The
    privacy floor is applied here and never relaxed later. No ranking, no
    eligibility, no placement — that is the engines' job.
    """
    priv = resolve_privacy(workload.privacy_tier)
    tier, trusted_model = _trusted_tier(repo, workload.capability_class)

    caps: list[str] = []
    if tier is not None:
        caps.extend(tier["required_capabilities"])
    for c in workload.extra_capabilities:
        if c not in caps:
            caps.append(c)

    pinned = trusted_model if (workload.pin_registry_model and trusted_model) else None

    # Privacy floor ANDed with caller ceilings: BC can only tighten.
    may_leave_box = priv.cloud_permitted
    allow_external = bool(workload.allow_external and may_leave_box)
    allow_paid = bool(workload.allow_paid and may_leave_box)
    allow_rented = bool(workload.allow_rented and may_leave_box)

    ac_context = {
        "task_id": workload.task_id,
        "privacy_class": _AC_PRIVACY_CLASS[priv.effective],
        "needed_capabilities": list(caps),
        "profile": workload.profile,
        "require_exact_model": pinned,
        "est_input_tokens": int(workload.est_input_tokens),
        "est_output_tokens": int(workload.est_output_tokens),
        "allow_external": allow_external,
        "allow_paid": allow_paid,
        "priority": workload.priority,
        "quality": workload.quality,
        "cloud_safe": may_leave_box,
        "pending_same_model_batch": 0,
        "allow_rented": allow_rented,
    }
    cc_body = {
        "model": pinned,
        "required_capabilities": list(caps),
        "context_tokens": int(workload.est_input_tokens),
        "max_output_tokens": int(workload.est_output_tokens),
        "latency_preference": workload.latency_preference,
        "quality_preference": workload.quality,
        "privacy_tier": priv.effective,
    }
    return {
        "task_id": workload.task_id,
        "capability_class": workload.capability_class,
        "matched_trusted_tier": tier is not None,
        "trusted_model": trusted_model,
        "pinned_model": pinned,
        "privacy": priv.as_dict(),
        "agentconnect_context": ac_context,
        "computeconnect_body": cc_body,
        # The header BC sends CC: identical to the body tier, so precedence
        # (more-restrictive-wins) can only ever confirm the floor, never widen it.
        "computeconnect_privacy_header": priv.effective,
    }


# ---------------------------------------------------------------------------
# Response validation — a malformed/hostile answer is "no usable decision".
# ---------------------------------------------------------------------------
def _valid_routing_decision(obj: object) -> bool:
    return isinstance(obj, dict) and obj.get("decision") in _AC_DECISIONS


def _valid_estimate(obj: object) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("eligible"), bool)


# ---------------------------------------------------------------------------
# The trigger.
# ---------------------------------------------------------------------------
@dataclass
class DelegationResult:
    task_id: str
    delegated: bool
    fallback: bool
    outcome_class: str                     # "delegated" | "deferred"
    privacy: dict
    request: dict
    routing_decision: dict | None = None
    placement_estimate: dict | None = None
    fallback_reason: str | None = None
    rejected_decision: dict | None = None  # an AC response BC refused (would widen)
    rejected_estimate: dict | None = None  # a CC estimate BC refused (would widen)
    provenance_ref: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "delegated": self.delegated,
            "fallback": self.fallback,
            "outcome_class": self.outcome_class,
            "privacy": self.privacy,
            "request": self.request,
            "routing_decision": self.routing_decision,
            "placement_estimate": self.placement_estimate,
            "fallback_reason": self.fallback_reason,
            "rejected_decision": self.rejected_decision,
            "rejected_estimate": self.rejected_estimate,
            "errors": list(self.errors),
        }


#: Tags on the provenance candidate. `orchestration-decision` binds the record;
#: NONE of these is a promotion — the candidate stays PENDING.
PROVENANCE_TAGS = ("orchestration-decision", "delegation-provenance")


def delegate(repo: Repo, workload, *, routing_client: RoutingClient | None = None,
             estimate_client: EstimateClient | None = None,
             record: bool = True, proposed_by: str = "delegation-trigger",
             proposed_by_type: str = "tool") -> DelegationResult:
    """Assemble → delegate → (fallback if needed) → record provenance.

    `routing_client` / `estimate_client` are injected (HTTP clients in production,
    fakes in tests). Either being None means "that engine is unavailable" and is
    treated exactly like an outage — a deterministic fallback, never a crash.
    """
    workload = workload if isinstance(workload, WorkloadSpec) \
        else WorkloadSpec.from_dict(workload)
    request = assemble_request(repo, workload)
    priv = request["privacy"]
    result = DelegationResult(
        task_id=workload.task_id, delegated=False, fallback=False,
        outcome_class="deferred", privacy=priv, request=request)

    # -- call AgentConnect (routing) -----------------------------------------
    routing: dict | None = None
    if routing_client is None:
        result.errors.append(f"{AGENTCONNECT}: no client configured (unavailable)")
    else:
        try:
            raw = routing_client.route(request["agentconnect_context"])
            if _valid_routing_decision(raw):
                routing = raw
            else:
                result.errors.append(f"{AGENTCONNECT}: malformed RoutingDecision")
        except DelegationClientError as e:
            result.errors.append(str(e))

    # -- call ComputeConnect (placement estimate) ----------------------------
    estimate: dict | None = None
    if estimate_client is None:
        result.errors.append(f"{COMPUTECONNECT}: no client configured (unavailable)")
    else:
        try:
            raw = estimate_client.estimate(
                request["computeconnect_body"],
                privacy_header=request["computeconnect_privacy_header"])
            if _valid_estimate(raw):
                estimate = raw
            else:
                result.errors.append(f"{COMPUTECONNECT}: malformed estimate")
        except DelegationClientError as e:
            result.errors.append(str(e))

    # -- never-widen guard: the forwarded ceilings BC actually sent ----------
    # These are the caller ceilings AFTER the privacy-floor AND (assemble_request):
    # BC only ever tightens them. Both the AC decision and the CC estimate are
    # re-validated against the floor AND these ceilings — BC never derives privacy
    # from an engine response, only from its own floor + the caller's ceilings.
    ctx = request["agentconnect_context"]
    ceilings = {
        "allow_external": bool(ctx["allow_external"]),
        "allow_paid": bool(ctx["allow_paid"]),
        "allow_rented": bool(ctx["allow_rented"]),
    }

    # A hostile/misconfigured AgentConnect that returns an off-box placement for
    # privacy-restricted work (or one the caller's ceilings forbid) is REFUSED,
    # not obeyed: BC records it as rejected and falls back.
    if routing is not None:
        rej = _offbox_rejection(_routing_offbox_kind(routing), priv, ceilings)
        if rej:
            result.rejected_decision = routing
            result.errors.append(
                f"{AGENTCONNECT}: decision {routing.get('decision')!r} would place "
                f"{priv['effective']!r} work off-box — refused ({rej})")
            routing = None

    # SYMMETRIC guard on the ComputeConnect ESTIMATE: a hostile estimate whose
    # placement indicates a cloud/rented/external target for privacy-restricted
    # work (or work the caller's ceilings forbid) is likewise REFUSED. A prior
    # gap: the estimate was validated for shape only, never for privacy.
    if estimate is not None:
        rej = _offbox_rejection(_estimate_offbox_kind(estimate), priv, ceilings)
        if rej:
            result.rejected_estimate = estimate
            result.errors.append(
                f"{COMPUTECONNECT}: estimate placement would place "
                f"{priv['effective']!r} work off-box — refused ({rej})")
            estimate = None

    # -- decide: delegated vs deterministic fallback -------------------------
    if routing is not None and estimate is not None:
        result.delegated = True
        result.fallback = False
        result.outcome_class = "delegated"
        result.routing_decision = routing
        result.placement_estimate = estimate
    else:
        # Deterministic, safe fallback: defer; never external; privacy = floor.
        result.delegated = False
        result.fallback = True
        result.outcome_class = "deferred"
        result.routing_decision = routing        # partial info kept if present
        result.placement_estimate = estimate
        result.fallback_reason = _fallback_reason(routing, estimate, result)

    if record:
        result.provenance_ref = _record_provenance(
            repo, workload, result, proposed_by=proposed_by,
            proposed_by_type=proposed_by_type)
    return result


def _fallback_reason(routing, estimate, result) -> str:
    missing = []
    if routing is None:
        missing.append(AGENTCONNECT)
    if estimate is None:
        missing.append(COMPUTECONNECT)
    who = " and ".join(missing) if missing else "no engine"
    return (f"deferred: {who} did not return a usable decision "
            f"({'; '.join(result.errors) or 'unavailable'}). "
            "Safe default: keep on-box / queue for human dispatch; nothing routed "
            "externally.")


def _record_provenance(repo: Repo, workload: WorkloadSpec, result: DelegationResult,
                       *, proposed_by: str, proposed_by_type: str) -> str:
    """File the decision + its inputs as an ordinary PENDING memory candidate.

    Never trusted, never promoted (LEDGER_SPEC §2). The human-readable prose is
    deliberately benign (no credentials, no matched values); the structured record
    lives in `metadata`. Scoped to the task so it never leaks into another task's
    recall.
    """
    verb = "delegated to AgentConnect + ComputeConnect" if result.delegated \
        else "deferred (deterministic fallback)"
    decision = result.as_dict()
    # A deterministic content fingerprint so two DISTINCT decisions never collide
    # on the ingest content hash (even within the same second), while an exact
    # re-decision dedups. It is derived only from the decision content.
    fp = hashlib.sha256(
        json.dumps(decision, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    text = (f"Orchestration decision for task {workload.task_id}: {verb}. "
            f"Capability class {workload.capability_class}, privacy "
            f"{result.privacy['effective']}. This is a recorded delegation "
            f"decision (provenance), not a trusted capability claim. "
            f"[decision-fingerprint {fp}]")
    metadata = {
        "kind": "delegation-decision",
        "decision": decision,
        # An explicit, machine-checkable honesty flag: this record is provenance,
        # it is NOT a trust signal and must never be read as one.
        "provenance_only": True,
        "trusted": False,
        "decision_fingerprint": fp,
    }
    try:
        res = api.capture_candidate(repo, {
            "text": text,
            "proposed_by": proposed_by,
            "proposed_by_type": proposed_by_type,
            "task_id": workload.task_id,
            "proposed_scopes": [f"task:{workload.task_id}"],
            "tags": list(PROVENANCE_TAGS),
            "metadata": metadata,
        })
    except ingest.IngestError as e:
        # An exact-duplicate provenance record (same decision already filed) must
        # never crash the trigger — the decision is already captured. Degrade to a
        # note; the caller still gets its (fallback or delegated) decision.
        result.errors.append(f"provenance: {e}")
        return None
    except candidates.SafetyRefused as e:
        # A task_id / capability_class that trips the capture safety gate must not
        # crash the trigger. The routing/placement decision itself is unaffected;
        # degrade to a recorded note (degrade-never-crash, ADR 0008 no-SPOF).
        result.errors.append(f"provenance: safety-refused ({e})")
        return None
    except Exception as e:  # noqa: BLE001 — degrade-never-crash boundary.
        # Any other capture failure (a candidates/api/db fault) must likewise never
        # propagate out of delegate(): recording provenance is best-effort, the
        # decision is already computed and returned to the caller.
        result.errors.append(
            f"provenance: capture failed ({type(e).__name__}: {e})")
        return None
    return res.candidate_id


def delegate_to_dict(repo: Repo, workload, **kw) -> dict:
    """Convenience for the CLI/JSON path."""
    return delegate(repo, workload, **kw).as_dict()
