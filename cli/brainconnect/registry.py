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

import json
from dataclasses import dataclass

from .db import Repo
from . import candidates, refs
from .scopes import Scope, GLOBAL

# The classification tag that binds every registry fact to the `model_performance`
# retrieval profile (LEDGER_SPEC §7). Kept as the literal substrate string rather
# than importing a profile name: the profile filters ON this tag, it is not this
# tag. A registry claim always also carries a stable `reg:*` key tag (below) that
# identifies which tier/role it speaks for.
MODEL_PERFORMANCE_TAG = "model-performance"

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
ROLE_DEPLOYED = "deployed"    # the currently DEPLOYED / measured model


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
        _ClaimSpec(
            key=_key(ROLE_TIER, t.name), role=ROLE_TIER, tier=t.name,
            scope=GLOBAL,
            text=(f"Capability tier '{t.name}' (ordinal {t.ordinal}): required "
                  f"capabilities [{caps}]; runtime provider binding: {t.provider}. "
                  "Tier-hierarchy metadata for the model/worker capability registry; "
                  "carries no benchmark numbers."),
            tags=(MODEL_PERFORMANCE_TAG, _key(ROLE_TIER, t.name))),
    ]
    if t.preferred_model:
        specs.append(_ClaimSpec(
            key=_key(ROLE_PREFERRED, t.name), role=ROLE_PREFERRED, tier=t.name,
            scope=Scope("model", t.preferred_model), model=t.preferred_model,
            text=(f"Declared PREFERRED model for the '{t.name}' tier: "
                  f"{t.preferred_model}. This is a preference/recommendation only "
                  "— it is not a required runtime dependency, and no benchmark "
                  "numbers have been measured for it."),
            tags=(MODEL_PERFORMANCE_TAG, _key(ROLE_PREFERRED, t.name))))
    if t.deployed_model:
        specs.append(_ClaimSpec(
            key=_key(ROLE_DEPLOYED, t.name), role=ROLE_DEPLOYED, tier=t.name,
            scope=Scope("model", t.deployed_model), model=t.deployed_model,
            text=(f"Currently DEPLOYED model for the '{t.name}' tier: "
                  f"{t.deployed_model}."),
            tags=(MODEL_PERFORMANCE_TAG, _key(ROLE_DEPLOYED, t.name))))
    return specs


def all_specs() -> list[_ClaimSpec]:
    out: list[_ClaimSpec] = []
    for t in tier_order():
        out.extend(_specs_for(t))
    return out


# --- status lookup (the trust half; joins DATA to the ledger) ---------------
def _status_for(repo: Repo, key: str) -> dict:
    """Where a registry fact stands in the ledger, keyed by its stable tag.

    A promoted claim wins over a pending candidate (a promoted fact of record
    supersedes its own proposal). Ordering is by id so the answer is deterministic
    even if a key were ever filed twice. `trusted` reflects the ledger's authority
    signal: a claim is trusted only when promoted AND not in an open contradiction
    (LEDGER_SPEC §14.1 — status is not trust).
    """
    like = f"%{json.dumps(key)}%"  # the key as its JSON-quoted array element
    claim = repo.one(
        "SELECT id, status FROM claims WHERE tags LIKE ? ORDER BY id LIMIT 1",
        (like,))
    if claim:
        promoted = claim["status"] == "promoted"
        trusted = False
        if promoted:
            disputed = repo.one(
                "SELECT 1 AS x FROM contradictions "
                "WHERE status='open' AND (claim_a=? OR claim_b=?) LIMIT 1",
                (claim["id"], claim["id"]))
            trusted = disputed is None
        return {"state": "claim", "ref": refs.claim(claim["id"]),
                "status": claim["status"], "promoted": promoted, "trusted": trusted}
    cand = repo.one(
        "SELECT id, status FROM memory_candidates WHERE tags LIKE ? ORDER BY id LIMIT 1",
        (like,))
    if cand:
        return {"state": "candidate", "ref": refs.candidate(cand["id"]),
                "status": cand["status"], "promoted": False, "trusted": False}
    return {"state": "absent", "ref": None, "status": "absent",
            "promoted": False, "trusted": False}


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


# --- seeding (proposes; never promotes) -------------------------------------
def seed(repo: Repo, *, proposed_by: str = "registry-seed",
         proposed_by_type: str = "tool") -> list[str]:
    """File the tier hierarchy + model declarations as PENDING memory candidates.

    Returns the refs of the candidates created (empty when everything is already
    present — seeding is idempotent). It NEVER promotes: every fact enters the
    ordinary human/librarian promotion gate, and no argument to this function can
    change that. A model or agent cannot use this to confer trust on a capability
    claim about itself — promotion is `candidates.promote`, which refuses every
    agent reviewer type (LEDGER_SPEC §2).
    """
    created: list[str] = []
    for spec in all_specs():
        if _status_for(repo, spec.key)["state"] != "absent":
            continue  # already filed (as a candidate or a promoted claim)
        cid, _ = candidates.create_checked(
            repo, spec.text, proposed_by=proposed_by,
            proposed_by_type=proposed_by_type,
            proposed_scopes=[spec.scope], tags=list(spec.tags))
        created.append(refs.candidate(cid))
    return created
