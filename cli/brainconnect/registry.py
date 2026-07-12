"""The trusted model/worker capability registry (ADR 0008 — Lane 1).

BrainConnect's ONE genuinely-missing, orchestration-layer, BC-native artifact: a
*trusted* capability fact. ComputeConnect's `estimated_quality` is an operator
heuristic; AgentConnect's `learned_quality` is self-conferred from observed
outcomes. Neither is trusted, and self-conferral is exactly the self-promotion
LEDGER_SPEC §2 forbids. This module is the closed loop that makes "no model owns
authority over its own capability claim" a structural fact.

It is a TRUST / KNOWLEDGE artifact, **not an engine**. Per ADR 0008 it contains:

  * NO routing math, NO placement math, NO scheduler, NO residency/warm-state.
    Those ship in AgentConnect (`RoutingEngine`) and ComputeConnect
    (`select_placement`) and are delegated, never duplicated here.

What lives here is only:

  1. `SEED_TIERS` — the tier hierarchy as **data** (ordinal, required capabilities,
     provider binding, the declared-preferred and currently-deployed model per
     tier). The preferred model is swappable by editing this data structure; no
     code branches on a tier name, so a swap is an ADR-0008 provider-portability
     change with no BrainConnect architectural change.
  2. `seed()` — files each tier/model fact as an ordinary PENDING BrainConnect
     memory candidate (LEDGER_SPEC §5.2) under the `model_performance` retrieval
     profile (§7) and `model:`/`worker:`/`global` scope (§5.5). It NEVER promotes:
     capability claims enter through the existing human/librarian promotion gate,
     and a model/agent can never self-promote one (that gate is `candidates.promote`,
     whose `REVIEWER_TYPES` excludes every agent type).
  3. `snapshot()` / `preferred_model()` — a deterministic read surface that joins
     the tier DATA with the ledger's TRUST STATUS (pending / promoted / absent).

Capability facts are therefore ordinary claims in the one ledger, not a parallel
store. This module adds structure and a query; it invents no new persistence.

Pure code, zero model calls.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

from .db import Repo
from . import candidates, refs, trust
from .scopes import Scope, GLOBAL

# The classification tag that binds a *model* capability fact (preferred/deployed,
# always `model:` scoped) to the `model_performance` retrieval profile
# (LEDGER_SPEC §7). Kept as the literal substrate string rather than importing a
# profile name: the profile filters ON this tag, it is not this tag.
#
# It is applied ONLY to model-scoped claims. §7 restricts `model_performance` to
# `model`/`worker` scope, so a GLOBAL-scoped claim must never carry it — the
# tier-STRUCTURE facts below are registry-structural, not model-performance, and
# carry `REGISTRY_STRUCTURAL_TAG` instead. (Regression: no claim binds
# `model_performance` at global scope.)
MODEL_PERFORMANCE_TAG = "model-performance"

# The classification tag for GLOBAL-scoped tier-STRUCTURE facts (ordinal, required
# capabilities, provider binding). This metadata describes the registry's own
# hierarchy; it is NOT a measured performance fact about any model, so it is kept
# off the `model_performance` profile and off model scope. It is a plain registry
# marker, distinct from the unforgeable per-fact ownership marker below.
REGISTRY_STRUCTURAL_TAG = "registry-structural"

# The metadata path the registry resolves its OWN canonical facts by. A registry
# fact is located by this UNFORGEABLE, registry-written marker
# (candidates.REGISTRY_CANONICAL_KEY) — NEVER by the public, squattable `reg:*`
# tag. See `_status_for`.
_CANON_PATH = "$." + candidates.REGISTRY_CANONICAL_KEY

# Tier names — the hierarchy small -> general-doc -> high-capability-local ->
# frontier-managers. Exposed as constants so callers (diagnostics, tests) never
# hard-code a string, but the ORDER and structure are data in `SEED_TIERS`.
SMALL = "small"
GENERAL_DOC = "general-doc"
HIGH_CAPABILITY_LOCAL = "high-capability-local"
FRONTIER_MANAGERS = "frontier-managers"

# Roles a registry fact can play about a tier.
ROLE_TIER = "tier"          # the tier-hierarchy metadata itself
ROLE_PREFERRED = "preferred"  # the DECLARED preferred model (a recommendation)
# ROLE_DEPLOYED is a DECLARED STATIC fact: "this is the model recorded as serving
# the tier", editable only by changing SEED_TIERS data. It is NOT a liveness or
# residency signal and must NEVER be wired to ComputeConnect residency / warm-state
# or host liveness — ADR 0008 / LEDGER_SPEC §2 forbid BrainConnect from re-holding
# live run state. Whether the model is actually loaded on :8080 right now is
# ComputeConnect's to answer, never this registry's.
ROLE_DEPLOYED = "deployed"    # the DECLARED-deployed model (STATIC; never live state)


@dataclass(frozen=True)
class TierSeed:
    """One tier, as data. Changing `preferred_model` here is the entire cost of
    swapping the preferred model for a tier — no code path branches on the name."""
    name: str
    ordinal: int
    #: Capabilities a model must satisfy to serve this tier. Requirements, not a
    #: score: the ranking/eligibility math lives in AgentConnect, not here.
    required_capabilities: tuple[str, ...]
    #: Which Connect plane the runtime for this tier is delegated to. A binding,
    #: not an engine — BrainConnect never loads or routes to the provider.
    provider: str
    #: The DECLARED preferred model: a preference/recommendation. It carries NO
    #: performance numbers and is NOT a required runtime dependency.
    preferred_model: str | None = None
    #: The currently DEPLOYED model actually serving this tier, if any.
    deployed_model: str | None = None


# ---------------------------------------------------------------------------
# THE SEED DATA — the whole tier hierarchy, data-driven (ADR 0008 Lane 1).
#
# `Qwen3.6-35B-A3B` is recorded as the DECLARED PREFERRED high-capability-local
# model per the epic brief (a recommendation, no benchmarks — see ADR 0008
# "Model-name reconciliation"). `qwen3-30b-a3b` is the currently-DEPLOYED model
# for that tier (the real model on the wiki-llama :8080 node). No benchmark
# numbers are published for either — none have been measured.
# ---------------------------------------------------------------------------
SEED_TIERS: tuple[TierSeed, ...] = (
    TierSeed(
        name=SMALL, ordinal=1,
        required_capabilities=("classification", "routing-hints", "short-extraction"),
        provider="computeconnect"),
    TierSeed(
        name=GENERAL_DOC, ordinal=2,
        required_capabilities=("summarization", "doc-qa", "tagging"),
        provider="computeconnect"),
    TierSeed(
        name=HIGH_CAPABILITY_LOCAL, ordinal=3,
        required_capabilities=("code", "reasoning", "tool-use", "long-context"),
        provider="computeconnect",
        preferred_model="Qwen3.6-35B-A3B",
        deployed_model="qwen3-30b-a3b"),
    TierSeed(
        name=FRONTIER_MANAGERS, ordinal=4,
        required_capabilities=("planning", "delegation", "adjudication", "verification"),
        provider="agentconnect"),
)


class RegistryError(Exception):
    pass


def tier_order() -> list[TierSeed]:
    """The tiers in their canonical order: by ordinal, ties broken by name.

    Deterministic and total — the sole ordering every read surface uses.
    """
    return sorted(SEED_TIERS, key=lambda t: (t.ordinal, t.name))


def get_tier(name: str) -> TierSeed:
    for t in SEED_TIERS:
        if t.name == name:
            return t
    raise RegistryError(
        f"unknown tier {name!r}; expected one of "
        f"{', '.join(t.name for t in tier_order())}")


def preferred_model(tier_name: str) -> str | None:
    """The DECLARED preferred model for a tier — read from DATA, never a constant.

    This is what diagnostics/status surfaces surface. It returns the recommendation
    only; it says nothing about whether a claim about that model has been promoted.
    """
    return get_tier(tier_name).preferred_model


def deployed_model(tier_name: str) -> str | None:
    """The DECLARED-deployed model for a tier — a STATIC recorded fact from data.

    This is NOT a liveness check. It says which model is *recorded* as serving the
    tier, not whether that model is loaded or reachable right now. Host/residency
    liveness belongs to ComputeConnect; wiring this to it would re-hold live run
    state, which ADR 0008 / LEDGER_SPEC §2 forbid.
    """
    return get_tier(tier_name).deployed_model


# --- how a tier maps to ledger facts ----------------------------------------
@dataclass(frozen=True)
class _ClaimSpec:
    """One capability fact to file for a tier. Identity + text + where it is
    scoped. The rich structure (ordinal, capabilities, provider) is NOT re-stored
    per claim — it lives in `SEED_TIERS`; the ledger fact only carries the trust
    status. `key` is a stable per-(tier, role) tag used both to make seeding
    idempotent and to locate the fact's status on read."""
    key: str
    role: str
    tier: str
    scope: Scope
    text: str
    tags: tuple[str, ...]
    model: str | None = None


def _key(role: str, tier: str) -> str:
    return f"reg:{role}:{tier}"


def _specs_for(t: TierSeed) -> list[_ClaimSpec]:
    """The ledger facts a tier contributes: its metadata, and its preferred and
    deployed model declarations (when the tier names them)."""
    caps = ", ".join(t.required_capabilities)
    specs: list[_ClaimSpec] = [
        # Tier-STRUCTURE metadata is GLOBAL-scoped and REGISTRY-STRUCTURAL, not a
        # model-performance fact: it describes the registry's own hierarchy, not a
        # measured property of any model. So it carries REGISTRY_STRUCTURAL_TAG, NOT
        # the model_performance tag — §7 confines model_performance to model/worker
        # scope, and a global claim must never bind it.
        _ClaimSpec(
            key=_key(ROLE_TIER, t.name), role=ROLE_TIER, tier=t.name,
            scope=GLOBAL,
            text=(f"Capability tier '{t.name}' (ordinal {t.ordinal}): required "
                  f"capabilities [{caps}]; runtime provider binding: {t.provider}. "
                  "Tier-hierarchy metadata for the model/worker capability registry; "
                  "carries no benchmark numbers."),
            tags=(REGISTRY_STRUCTURAL_TAG, _key(ROLE_TIER, t.name))),
    ]
    if t.preferred_model:
        # A MODEL claim: model-scoped, so it legitimately binds model_performance.
        specs.append(_ClaimSpec(
            key=_key(ROLE_PREFERRED, t.name), role=ROLE_PREFERRED, tier=t.name,
            scope=Scope("model", t.preferred_model), model=t.preferred_model,
            text=(f"Declared PREFERRED model for the '{t.name}' tier: "
                  f"{t.preferred_model}. This is a preference/recommendation only "
                  "— it is not a required runtime dependency, and no benchmark "
                  "numbers have been measured for it."),
            tags=(MODEL_PERFORMANCE_TAG, _key(ROLE_PREFERRED, t.name))))
    if t.deployed_model:
        # A DECLARED-STATIC model claim (model-scoped). It records which model is
        # recorded as serving the tier — NOT that it is live. Never wire this to
        # ComputeConnect residency or host liveness (ADR 0008 / §2 no-live-state).
        specs.append(_ClaimSpec(
            key=_key(ROLE_DEPLOYED, t.name), role=ROLE_DEPLOYED, tier=t.name,
            scope=Scope("model", t.deployed_model), model=t.deployed_model,
            text=(f"DECLARED-deployed model recorded for the '{t.name}' tier: "
                  f"{t.deployed_model}. A static recorded fact, not a liveness "
                  "signal."),
            tags=(MODEL_PERFORMANCE_TAG, _key(ROLE_DEPLOYED, t.name))))
    return specs


def all_specs() -> list[_ClaimSpec]:
    out: list[_ClaimSpec] = []
    for t in tier_order():
        out.extend(_specs_for(t))
    return out


# --- status lookup (the trust half; joins DATA to the ledger) ---------------
def _absent() -> dict:
    return {"state": "absent", "ref": None, "status": "absent",
            "promoted": False, "trusted": False}


def _status_for(repo: Repo, key: str) -> dict:
    """Where the registry's OWN canonical fact for `key` stands in the ledger.

    Resolution is by the registry-CONTROLLED, UNFORGEABLE marker the registry wrote
    into the candidate's metadata at seed time (candidates.REGISTRY_CANONICAL_KEY),
    matched EXACTLY via `json_extract` — NEVER by the public `reg:*` tag. That tag
    is squattable: `api.capture_candidate` forwards arbitrary caller tags, so any
    agent can file a candidate carrying `reg:preferred:...`. Such a squatter does
    NOT carry this marker (the public metadata path strips reserved keys), so it can
    neither be surfaced here nor suppress the canonical fact. This is the fix for the
    tag-squatting backdoor.

    A promoted canonical candidate resolves to its claim of record (via
    `promoted_claim_id`, the registry's own link — again not a tag). `trusted`
    reflects the ledger's authority signal: promoted AND not in an open
    contradiction (LEDGER_SPEC §14.1 — status is not trust). Ordering by id keeps
    the answer deterministic even if a key were somehow filed twice.
    """
    cand = repo.one(
        "SELECT id, status, promoted_claim_id FROM memory_candidates "
        "WHERE json_extract(metadata, ?) = ? ORDER BY id LIMIT 1",
        (_CANON_PATH, key))
    if cand is None:
        return _absent()
    if cand["promoted_claim_id"] is not None:
        claim = repo.one(
            "SELECT id, status FROM claims WHERE id = ?", (cand["promoted_claim_id"],))
        if claim is not None:
            promoted = claim["status"] == "promoted"
            contradicted = False
            if promoted:
                disputed = repo.one(
                    "SELECT 1 AS x FROM contradictions "
                    "WHERE status='open' AND (claim_a=? OR claim_b=?) LIMIT 1",
                    (claim["id"], claim["id"]))
                contradicted = disputed is not None
            trusted = trust.is_trusted(status=claim["status"], contradicted=contradicted)
            return {"state": "claim", "ref": refs.claim(claim["id"]),
                    "status": claim["status"], "promoted": promoted,
                    "trusted": trusted}
    return {"state": "candidate", "ref": refs.candidate(cand["id"]),
            "status": cand["status"], "promoted": False, "trusted": False}


def _model_entry(repo: Repo, spec: _ClaimSpec) -> dict:
    """A preferred/deployed model entry: identity + scope + trust status ONLY.

    Deliberately carries no metric field of any kind. A capability *number* is a
    measured `model_performance` claim that flows in through Lane 7 as its own
    candidate; the registry never fabricates or attaches one here.
    """
    entry = {"model": spec.model, "role": spec.role, "scope": str(spec.scope)}
    entry.update(_status_for(repo, spec.key))
    return entry


def snapshot(repo: Repo) -> dict:
    """The full registry as a deterministic dict: every tier in order, each with
    its metadata-claim status and its preferred/deployed model + trust status.

    Structure and identity come from `SEED_TIERS` (data); trust status comes from
    the ledger. The result is stable — two calls against an unchanged ledger are
    byte-identical.
    """
    tiers = []
    for t in tier_order():
        specs = {s.role: s for s in _specs_for(t)}
        pref = specs.get(ROLE_PREFERRED)
        dep = specs.get(ROLE_DEPLOYED)
        tiers.append({
            "tier": t.name,
            "ordinal": t.ordinal,
            "required_capabilities": list(t.required_capabilities),
            "provider": t.provider,
            "metadata_claim": _status_for(repo, _key(ROLE_TIER, t.name)),
            "preferred_model": _model_entry(repo, pref) if pref else None,
            "deployed_model": _model_entry(repo, dep) if dep else None,
        })
    return {
        "tiers": tiers,
        # The declared preferred high-capability-local model, read from data. This
        # is what diagnostics surface — never a hard-coded constant.
        "preferred_high_capability_local": preferred_model(HIGH_CAPABILITY_LOCAL),
    }


# --- the trusted-only view (ADR 0008 Lane 3, served over :8787) --------------
# AgentConnect's `RoutingEngine` pulls a HUMAN-PROMOTED capability source to weight
# instead of its self-conferred `learned_quality` (the self-promotion §2 forbids).
# What it may weight is EXACTLY the set of trusted claims — nothing pending, nothing
# squatted. This view is the read side of that contract: it filters `snapshot()` down
# to only the entries whose ledger status is `trusted` (promoted AND not in an open
# contradiction — LEDGER_SPEC §14.1, resolved by the unforgeable registry marker in
# `_status_for`, never by a squattable `reg:*` tag). Untrusted/pending/squatted
# entries are OMITTED (the tier's model slot goes null), never relabelled trusted.
#
# Serialization is delegated to the server; this returns a plain, deterministic
# dict. It carries NO fabricated numbers — only identity (tier/role/model/scope),
# trust status, and the promoted claim reference AgentConnect keys on.

def _trusted_model(tier_name: str, entry: dict | None) -> dict | None:
    """A model entry from `snapshot()`, reshaped for AC — or None if not trusted.

    Only a `trusted` entry (promoted + uncontradicted) survives; a pending or
    squatter-tricked entry returns None and is therefore never presented as a claim
    AgentConnect should weight. No metric field is ever added — a capability number
    is a measured `model_performance` claim that arrives through Lane 7 on its own.
    """
    if not entry or not entry.get("trusted"):
        return None
    return {
        "tier": tier_name,
        "role": entry["role"],
        "model": entry["model"],
        "scope": entry["scope"],
        "status": entry["status"],
        "promoted": entry["promoted"],
        "trusted": entry["trusted"],
        # `snapshot()` resolves a trusted entry's ref to its claim of record.
        "promoted_claim_ref": entry["ref"],
    }


def _trusted_metadata(entry: dict | None) -> dict | None:
    """The tier-structure claim's trust status — surfaced only when trusted."""
    if not entry or not entry.get("trusted"):
        return None
    return {
        "status": entry["status"],
        "promoted": entry["promoted"],
        "trusted": entry["trusted"],
        "promoted_claim_ref": entry["ref"],
    }


def trusted_view(repo: Repo) -> dict:
    """The registry restricted to TRUSTED, human-promoted capability claims.

    The read-only payload served at ``GET /registry`` (ADR 0008 Lane 3). It is the
    input AgentConnect PULLS to weight a trusted source in place of self-conferred
    `learned_quality`; BC decides what is trusted, AC decides how to weight it.

    Shape: the full tier hierarchy STRUCTURE (ordinal / required capabilities /
    provider — data-derived, never squattable), each with its trusted metadata and
    preferred/deployed model claim (or ``null`` when not yet promoted), plus a flat
    ``trusted_capability_claims`` list AC can consume directly. Only entries the
    ledger marks ``trusted`` appear as claims; pending and squatted facts are
    omitted, never relabelled. Deterministic — two calls against an unchanged ledger
    are byte-identical (it walks `snapshot()`, which is itself deterministic).
    """
    snap = snapshot(repo)
    tiers: list[dict] = []
    flat: list[dict] = []
    for t in snap["tiers"]:
        pref = _trusted_model(t["tier"], t["preferred_model"])
        dep = _trusted_model(t["tier"], t["deployed_model"])
        tiers.append({
            "tier": t["tier"],
            "ordinal": t["ordinal"],
            "required_capabilities": t["required_capabilities"],
            "provider": t["provider"],
            "metadata_claim": _trusted_metadata(t["metadata_claim"]),
            "preferred_model": pref,
            "deployed_model": dep,
        })
        for model_claim in (pref, dep):
            if model_claim is not None:
                flat.append(model_claim)
    return {
        "registry": "brainconnect",
        # What these facts ARE and are NOT — so a consumer can never mistake this
        # for a benchmark feed or a live-state signal.
        "trust_basis": (
            "human-promoted, registry-canonical capability claims — LEDGER_SPEC §2 "
            "(promotion is human-only), §7 (model_performance), §14 (:8787). No "
            "benchmark numbers; not a liveness signal."),
        "tiers": tiers,
        "trusted_capability_claims": flat,
        "count": len(flat),
    }


# --- seeding (proposes; never promotes) -------------------------------------
def _squatter_id(repo: Repo, key: str) -> int | None:
    """A NON-registry candidate that has squatted the public `reg:*` tag for `key`.

    Membership is EXACT JSON-array containment (`json_each` value equality), never a
    `LIKE '%...%'` substring — so a future tier/role name containing `_` or `%`
    cannot over-match a different key. A registry-authored candidate (one carrying
    the unforgeable marker) is excluded: it is not a squatter, it is us.
    """
    row = repo.one(
        "SELECT id FROM memory_candidates "
        "WHERE EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?) "
        "AND json_extract(metadata, ?) IS NULL ORDER BY id LIMIT 1",
        (key, _CANON_PATH))
    return row["id"] if row else None


def seed(repo: Repo, *, proposed_by: str = "registry-seed",
         proposed_by_type: str = "tool") -> list[str]:
    """File the tier hierarchy + model declarations as PENDING memory candidates.

    Returns the refs of the candidates created (empty when everything is already
    present — seeding is idempotent). It NEVER promotes: every fact enters the
    ordinary human/librarian promotion gate, and no argument to this function can
    change that. A model or agent cannot use this to confer trust on a capability
    claim about itself — promotion is `candidates.promote`, which refuses every
    agent reviewer type (LEDGER_SPEC §2).

    Idempotency and squatting are resolved by the UNFORGEABLE registry marker, never
    by the public tag: a fact is "already filed" only when the registry's OWN
    canonical candidate/claim exists (marker present). If a non-registry actor has
    squatted the `reg:*` tag, that does NOT cause a silent skip — the canonical fact
    is still filed (carrying the marker), and a warning surfaces the collision for a
    human. The squatter can neither impersonate nor suppress the canonical fact.
    """
    created: list[str] = []
    for spec in all_specs():
        if _status_for(repo, spec.key)["state"] != "absent":
            continue  # OUR canonical fact is already filed (marker present)
        squatter = _squatter_id(repo, spec.key)
        if squatter is not None:
            warnings.warn(
                f"registry: public tag {spec.key!r} was squatted by non-registry "
                f"candidate #{squatter}; filing the canonical fact anyway (the "
                "squatter cannot impersonate or suppress it).",
                stacklevel=2)
        cid, _ = candidates.create_checked(
            repo, spec.text, proposed_by=proposed_by,
            proposed_by_type=proposed_by_type,
            proposed_scopes=[spec.scope], tags=list(spec.tags),
            registry_canonical=spec.key)
        created.append(refs.candidate(cid))
    return created
