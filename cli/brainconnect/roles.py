"""Agent-role assignment — recommend + record (ADR 0008 Lane 6).

BrainConnect MAPS a plan's agent-role requirements to existing AgentConnect
model-manager profiles and RECORDS the resulting role-assignment as ordinary
PENDING decision-provenance. That is the whole job.

What this module is NOT (ADR 0008 Lane 6 binding boundary):

* It does **not** run AgentConnect's `RouterService` decompose→execute→synthesize,
  does **not** spawn workers, does **not** execute, authorize, or assign ownership,
  and makes **zero** model calls. Roles are *delegated* to AgentConnect; AC
  executes and — with Decima — ENFORCES ownership / concurrency / independence at
  execution time. BC only recommends and records.
* It contains **no role engine and no verifier.** The mapping is a deterministic
  DATA table (`ROLE_TABLE`); nothing branches on a role name. Swapping which AC
  profile a role uses is an edit to DATA — the ADR-0008 provider-portability rule
  made structural for roles exactly as the registry made it structural for models.
* It never **promotes** what it records. The role-assignment is filed as an
  ordinary PENDING memory candidate (LEDGER_SPEC §2) — never auto-trusted, never
  self-promoted. An agent/worker cannot use this path to confer trust on itself.

Reviewer independence is a **recommendation**, not enforcement: BC flags when a
reviewer/verifier role would share an AgentConnect profile with the implementer
(the artifact producer under review), so an operator/AC can preserve independent
review. BC does not itself keep reviewer and implementer apart at runtime — that
is AgentConnect's and Decima's job. BC recommends; AC/Decima enforce.

Pure code, zero model calls.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .db import Repo
from . import api, ingest, registry, candidates

# ---------------------------------------------------------------------------
# The AgentConnect model-manager profiles this maps onto (from AC recon:
# `model_manager/backends.py`). A role maps to EXACTLY one of these. Kept as a
# frozen set so a data edit that names a non-existent profile fails at import,
# not silently downstream.
# ---------------------------------------------------------------------------
AC_PROFILES: frozenset[str] = frozenset({
    "general_coder", "coding_specialist", "review_worker", "critic",
})

# Role kinds — a small closed vocabulary describing what the role DOES. Used by
# the independence recommendation (a reviewer/verifier must stay independent of
# the producer under review); NOT a branch on any specific role name.
KIND_PRODUCER = "producer"
KIND_REVIEWER = "reviewer"
KIND_VERIFIER = "verifier"
_ROLE_KINDS = frozenset({KIND_PRODUCER, KIND_REVIEWER, KIND_VERIFIER})


@dataclass(frozen=True)
class RoleMapping:
    """One supported agent role, as DATA.

    Changing `ac_profile` (or `capability_class`) here is the entire cost of
    re-pointing a role at a different AgentConnect profile / capability tier — no
    code path branches on `role`. `reviews` marks a role that independently
    reviews/verifies a producer's output; `primary_producer` marks the role whose
    artifact is under review (the implementer). Both drive the reviewer-independence
    recommendation from DATA, never from a role-name branch.
    """
    role: str
    #: The existing AgentConnect model-manager profile AC would run this role as.
    ac_profile: str
    #: The registry capability tier (Lane 1) whose structural capabilities the
    #: role needs — this is what makes a role assignment COMPOSE with the Lane-4
    #: delegation trigger (feed this tier to `delegate` as the capability_class).
    capability_class: str
    role_kind: str
    #: True for a role that independently reviews/verifies the implementer's work.
    reviews: bool = False
    #: True for the artifact producer whose output the reviewers check.
    primary_producer: bool = False


# ---------------------------------------------------------------------------
# THE ROLE TABLE — the whole role→profile mapping, data-driven (ADR 0008 Lane 6).
#
# Every supported role from the brief maps to an existing AC profile and a Lane-1
# capability tier. The table is independence-clean by construction: no reviewer or
# verifier shares the implementer's `coding_specialist` profile, so the default
# assignment already preserves independent review. Re-point any role by editing a
# row; nothing else changes.
# ---------------------------------------------------------------------------
ROLE_TABLE: tuple[RoleMapping, ...] = (
    RoleMapping(
        role="implementer", ac_profile="coding_specialist",
        capability_class=registry.HIGH_CAPABILITY_LOCAL,
        role_kind=KIND_PRODUCER, primary_producer=True),
    RoleMapping(
        role="test_reviewer", ac_profile="review_worker",
        capability_class=registry.HIGH_CAPABILITY_LOCAL,
        role_kind=KIND_REVIEWER, reviews=True),
    RoleMapping(
        role="security_reviewer", ac_profile="critic",
        capability_class=registry.HIGH_CAPABILITY_LOCAL,
        role_kind=KIND_REVIEWER, reviews=True),
    RoleMapping(
        role="documentation_reviewer", ac_profile="review_worker",
        capability_class=registry.GENERAL_DOC,
        role_kind=KIND_REVIEWER, reviews=True),
    RoleMapping(
        role="verifier", ac_profile="critic",
        capability_class=registry.HIGH_CAPABILITY_LOCAL,
        role_kind=KIND_VERIFIER, reviews=True),
    RoleMapping(
        role="research_agent", ac_profile="general_coder",
        capability_class=registry.GENERAL_DOC,
        role_kind=KIND_PRODUCER),
    RoleMapping(
        role="integration_agent", ac_profile="general_coder",
        capability_class=registry.HIGH_CAPABILITY_LOCAL,
        role_kind=KIND_PRODUCER),
)

#: Role name → mapping, the single lookup every resolution goes through (no
#: branching). Built once from the table.
_BY_ROLE: dict[str, RoleMapping] = {m.role: m for m in ROLE_TABLE}

#: The supported roles, in deterministic (sorted) order.
SUPPORTED_ROLES: tuple[str, ...] = tuple(sorted(_BY_ROLE))


def _validate_table() -> None:
    """Guard the DATA at import: every profile is a real AC profile, every tier is
    a real registry tier, every kind is known, and the `reviews`/`primary_producer`
    flags are coherent. A bad data edit fails HERE, never silently downstream."""
    tier_names = {t.name for t in registry.SEED_TIERS}
    for m in ROLE_TABLE:
        if m.ac_profile not in AC_PROFILES:
            raise RoleError(
                f"role {m.role!r} maps to unknown AC profile {m.ac_profile!r}; "
                f"expected one of {', '.join(sorted(AC_PROFILES))}")
        if m.capability_class not in tier_names:
            raise RoleError(
                f"role {m.role!r} maps to unknown capability tier "
                f"{m.capability_class!r}; expected one of {', '.join(sorted(tier_names))}")
        if m.role_kind not in _ROLE_KINDS:
            raise RoleError(f"role {m.role!r} has unknown role_kind {m.role_kind!r}")
        if m.reviews and m.role_kind == KIND_PRODUCER:
            raise RoleError(f"role {m.role!r} is a producer but marked reviews=True")


class RoleError(Exception):
    """A caller-side / data problem (an unknown override target, a bad table).

    An UNKNOWN requested role is NOT this: it is handled fail-closed inside the
    result (`refused_roles`), never silently mapped and never crashing the caller.
    """


# ---------------------------------------------------------------------------
# Read surface — the deterministic role→profile table.
# ---------------------------------------------------------------------------
def role_table() -> list[dict]:
    """The full role→profile mapping as a deterministic, sorted list of dicts.

    Byte-stable across calls (sorted by role). This is the data an operator reads
    to see how BC would map plan roles onto AgentConnect profiles.
    """
    return [
        {
            "role": m.role,
            "ac_profile": m.ac_profile,
            "capability_class": m.capability_class,
            "role_kind": m.role_kind,
            "reviews": m.reviews,
            "primary_producer": m.primary_producer,
        }
        for m in sorted(ROLE_TABLE, key=lambda m: m.role)
    ]


# ---------------------------------------------------------------------------
# The assignment result.
# ---------------------------------------------------------------------------
@dataclass
class RoleAssignmentResult:
    task_id: str
    #: role → resolved profile, sorted by role. Known roles only.
    assignments: list[dict]
    #: unknown roles, fail-closed: {role, reason}. Never given a profile.
    refused_roles: list[dict]
    #: reviewer-independence recommendations (recorded, not enforced by BC).
    independence: list[dict]
    #: the subset of `independence` where reviewer and implementer share a profile
    #: (elevated same-agent risk) — the collision FLAG.
    collisions: list[dict]
    #: True iff every requested role resolved (no refusals).
    ok: bool
    provenance_ref: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "assignments": self.assignments,
            "refused_roles": self.refused_roles,
            "independence": self.independence,
            "collisions": self.collisions,
            "ok": self.ok,
            "errors": list(self.errors),
        }


#: Tags on the provenance candidate. `orchestration-decision` binds the record to
#: the same family as the Lane-4 delegation provenance; NONE is a promotion.
PROVENANCE_TAGS = ("orchestration-decision", "role-assignment")


def _resolve_profile(mapping: RoleMapping, overrides: dict[str, str]) -> str:
    """The AC profile for a role: the table's, unless a caller override re-points
    it (provider-portability). An override is validated against AC_PROFILES by the
    caller before we get here — this only applies it."""
    return overrides.get(mapping.role, mapping.ac_profile)


def _independence_findings(assignments: list[dict]) -> list[dict]:
    """Recommendations that each reviewer/verifier stay independent of every
    producer role (not only the primary-producer implementer) it might review.

    Fully data-driven: it reads the `reviews` flag and `role_kind` carried on each
    assignment, never a role name. The producer set is EVERY role whose kind is
    `KIND_PRODUCER` (implementer, research_agent, integration_agent, …) — not just
    the single `primary_producer` implementer — so a reviewer/verifier that shares
    ANY producer role's AC profile is surfaced. For every (reviewer, producer) pair
    it emits a recommendation; `same_profile` is the elevated-risk collision flag
    (the two share an AC profile, so AC might hand both to one agent). Deterministic
    order: sorted by (reviewer_role, producer_role).
    """
    reviewers = [a for a in assignments if a["reviews"]]
    producers = [a for a in assignments if a["role_kind"] == KIND_PRODUCER]
    findings: list[dict] = []
    for r in reviewers:
        for p in producers:
            if r["role"] == p["role"]:
                continue  # a role is not asked to be independent of itself
            same = r["ac_profile"] == p["ac_profile"]
            if same:
                rec = (
                    f"{r['role']} ({r['ac_profile']}) shares the implementer's AC "
                    f"profile ({p['role']} → {p['ac_profile']}). AgentConnect/Decima "
                    "MUST assign a DISTINCT agent so review stays independent; BC "
                    "recommends re-pointing one role to a different profile.")
            else:
                rec = (
                    f"{r['role']} ({r['ac_profile']}) already uses a different AC "
                    f"profile from the implementer ({p['role']} → {p['ac_profile']}); "
                    "AgentConnect/Decima should still ensure a distinct agent "
                    "executes it to preserve independent review.")
            findings.append({
                "reviewer_role": r["role"],
                "reviewer_kind": r["role_kind"],
                "reviewer_profile": r["ac_profile"],
                "implementer_role": p["role"],
                "implementer_profile": p["ac_profile"],
                "same_profile": same,
                "recommendation": rec,
            })
    findings.sort(key=lambda f: (f["reviewer_role"], f["implementer_role"]))
    return findings


def assign_roles(repo: Repo, task_id: str, roles, *,
                 profile_overrides: dict[str, str] | None = None,
                 record: bool = True, proposed_by: str = "role-assigner",
                 proposed_by_type: str = "tool") -> RoleAssignmentResult:
    """Map requested agent roles → AC profiles, flag reviewer-independence, record.

    * Known roles resolve through the DATA table (plus any validated override).
    * An UNKNOWN role is FAIL-CLOSED: it is refused (recorded in `refused_roles`
      with a reason), never given a profile, and `ok` becomes False. It never
      crashes the caller.
    * Reviewer/verifier independence vs the implementer is flagged as a
      RECOMMENDATION; BC does not enforce it (AC/Decima do at execution).
    * The whole assignment is recorded as ordinary PENDING provenance — never
      trusted, never auto-promoted.

    Zero model calls; spawns nothing; assigns no ownership.
    """
    if not str(task_id or "").strip():
        raise RoleError("role assignment requires a task_id")
    overrides = dict(profile_overrides or {})
    # Validate overrides fail-closed: an override to an unknown profile, or of an
    # unknown role, is a hard caller error (never silently applied / dropped).
    for role, profile in sorted(overrides.items()):
        if role not in _BY_ROLE:
            raise RoleError(
                f"cannot override unknown role {role!r}; supported roles: "
                f"{', '.join(SUPPORTED_ROLES)}")
        if profile not in AC_PROFILES:
            raise RoleError(
                f"override for {role!r} names unknown AC profile {profile!r}; "
                f"expected one of {', '.join(sorted(AC_PROFILES))}")

    requested = list(roles or [])
    # An EMPTY role request is a clean no-op / soft-refusal: there is nothing to
    # map, so BC does NOT file a vacuous "0 role(s) mapped" provenance candidate.
    # It is signalled clearly (ok=False + an explanatory note) and never raises.
    if not requested:
        return RoleAssignmentResult(
            task_id=str(task_id).strip(),
            assignments=[], refused_roles=[], independence=[], collisions=[],
            ok=False, provenance_ref=None,
            errors=["empty role request: nothing to map — no-op, no provenance "
                    "candidate recorded"])
    assignments: list[dict] = []
    refused: list[dict] = []
    seen: set[str] = set()
    for role in requested:
        name = str(role or "").strip()
        if not name:
            refused.append({"role": role, "reason": "empty role name"})
            continue
        if name in seen:
            continue  # a duplicate request is deterministically collapsed
        seen.add(name)
        mapping = _BY_ROLE.get(name)
        if mapping is None:
            # FAIL CLOSED: unknown role is refused, never mapped to a profile.
            refused.append({
                "role": name,
                "reason": (f"unknown role; not in the supported set "
                           f"({', '.join(SUPPORTED_ROLES)}) — refused, not mapped"),
            })
            continue
        assignments.append({
            "role": mapping.role,
            "ac_profile": _resolve_profile(mapping, overrides),
            "capability_class": mapping.capability_class,
            "role_kind": mapping.role_kind,
            "reviews": mapping.reviews,
            "primary_producer": mapping.primary_producer,
            "overridden": mapping.role in overrides,
        })

    assignments.sort(key=lambda a: a["role"])
    refused.sort(key=lambda r: str(r["role"]))
    independence = _independence_findings(assignments)
    collisions = [f for f in independence if f["same_profile"]]

    result = RoleAssignmentResult(
        task_id=str(task_id).strip(),
        assignments=assignments,
        refused_roles=refused,
        independence=independence,
        collisions=collisions,
        ok=not refused,
    )
    if record:
        result.provenance_ref = _record_provenance(
            repo, result, proposed_by=proposed_by, proposed_by_type=proposed_by_type)
    return result


def _record_provenance(repo: Repo, result: RoleAssignmentResult, *,
                       proposed_by: str, proposed_by_type: str) -> str | None:
    """File the role-assignment as an ordinary PENDING memory candidate.

    Never trusted, never promoted (LEDGER_SPEC §2). Scoped to the task so it never
    leaks into another task's recall. The full assignment (profiles, refusals,
    independence findings) lives in `metadata` for later explainability; a
    deterministic fingerprint keeps two distinct assignments from colliding on the
    ingest content hash while an exact re-assignment dedups. Degrade-never-crash:
    any capture failure is recorded as a note, never propagated (the assignment is
    already computed and returned).
    """
    decision = result.as_dict()
    fp = hashlib.sha256(
        json.dumps(decision, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    n_coll = len(result.collisions)
    text = (
        f"Agent-role assignment for task {result.task_id}: mapped "
        f"{len(result.assignments)} role(s) to AgentConnect profiles"
        + (f", refused {len(result.refused_roles)} unknown role(s)"
           if result.refused_roles else "")
        + (f", flagged {n_coll} reviewer-independence collision(s)"
           if n_coll else "")
        + ". BC RECOMMENDS + RECORDS; AgentConnect executes and enforces "
        "ownership/independence. This is recorded provenance, not a trusted "
        f"capability claim. [assignment-fingerprint {fp}]")
    metadata = {
        "kind": "role-assignment",
        "assignment": decision,
        "provenance_only": True,
        "trusted": False,
        "assignment_fingerprint": fp,
    }
    try:
        res = api.capture_candidate(repo, {
            "text": text,
            "proposed_by": proposed_by,
            "proposed_by_type": proposed_by_type,
            "task_id": result.task_id,
            "proposed_scopes": [f"task:{result.task_id}"],
            "tags": list(PROVENANCE_TAGS),
            "metadata": metadata,
        })
    except ingest.IngestError as e:
        result.errors.append(f"provenance: {e}")
        return None
    except candidates.SafetyRefused as e:
        result.errors.append(f"provenance: safety-refused ({e})")
        return None
    except Exception as e:  # noqa: BLE001 — degrade-never-crash boundary.
        result.errors.append(
            f"provenance: capture failed ({type(e).__name__}: {e})")
        return None
    return res.candidate_id


def assign_to_dict(repo: Repo, task_id: str, roles, **kw) -> dict:
    """Convenience for the CLI/JSON path."""
    return assign_roles(repo, task_id, roles, **kw).as_dict()


# Fail fast on a bad data edit — validate the table at import time.
_validate_table()
