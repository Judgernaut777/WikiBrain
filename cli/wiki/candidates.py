"""Memory candidates — the proposal half of the gate (LEDGER_SPEC.md §5.2).

Agents propose; humans (or an approved librarian workflow) promote. This module is
where that asymmetry is *enforced in code* rather than merely by which MCP tools a
mode happens to expose:

  * `create()` writes `status='pending'`, always. There is no argument that makes
    it write anything else.
  * `promote()` refuses a reviewer whose actor type is an agent.

It is also where two of the three safety surfaces are enforced, for the same
reason: a check that lives at the MCP tool protects the MCP tool, while a check
here protects the ledger. Capture scans before the text becomes an inbox artifact;
promotion scans before the text becomes trusted.

Safety and trust remain independent. A clean scan never promotes anything, and a
promoted claim is never assumed safe to expose — see `docs/SAFETY.md`.

Pure code, zero model calls.
"""
from __future__ import annotations

import json

from .db import Repo
from . import confidence as conf
from . import ingest, refs, safety, scopes, util
from .scopes import Scope

# Who may propose. Anyone, including agents — that is the point of a candidate.
PROPOSER_TYPES = ("human", "manager", "worker", "librarian", "agent", "tool")

# Who may *promote*. An agent proposing its own memory into trusted recall is the
# exact failure this ledger exists to prevent, so the agent types are absent.
REVIEWER_TYPES = ("human", "librarian")

STATUSES = ("pending", "promoted", "rejected", "archived")

# Only a pending candidate is reviewable. A promoted one already has a claim of
# record; a rejected/archived one must be re-proposed rather than silently revived.
_REVIEWABLE_FROM = ("pending",)

# The evidence type recorded on the claim_sources row a promotion creates: an
# agent *asserted* this text, a librarian did not extract it from the source.
PROMOTION_EVIDENCE_TYPE = "asserted"


class CandidateError(Exception):
    pass


class SafetyRefused(CandidateError):
    """Safety policy refused an operation. Carries the audit-safe result.

    A subclass of `CandidateError` so that every existing caller — the MCP tools,
    the CLI, the API facade — already surfaces it as a user error rather than a
    traceback. `result.summary()` contains no matched text.
    """

    def __init__(self, message: str, result: "safety.SafetyResult") -> None:
        super().__init__(message)
        self.result = result


def _require(repo: Repo, cid: int):
    row = repo.one("SELECT * FROM memory_candidates WHERE id = ?", (cid,))
    if not row:
        raise CandidateError(f"no candidate {refs.candidate(cid)}")
    return row


def _require_reviewable(row, action: str):
    if row["status"] not in _REVIEWABLE_FROM:
        raise CandidateError(
            f"candidate {refs.candidate(row['id'])} is {row['status']}; "
            f"cannot {action} (only pending candidates are reviewable)")


# --- propose ----------------------------------------------------------------
def create(repo: Repo, text: str, **kw) -> int:
    """File a PENDING memory candidate. Returns its id.

    See `create_checked` for the safety verdict; callers that want to report what
    was masked or quarantined should use that instead.
    """
    return create_checked(repo, text, **kw)[0]


def create_checked(repo: Repo, text: str, *, proposed_by: str, proposed_by_type: str,
                   source_id: int | None = None, source_ref: str | None = None,
                   task_id: str | None = None,
                   proposed_scopes: list[Scope] | None = None,
                   tags: list[str] | None = None, metadata: dict | None = None,
                   harness: str | None = None) -> tuple[int, "safety.SafetyResult"]:
    """File a PENDING memory candidate. Never promotes, by construction.

    Returns `(candidate_id, safety_verdict)`. The stored text is the verdict's
    text: identical to the input unless policy called for masking.

    When `source_id` is omitted the text is filed as its own evidence source (an
    `inbox/` capture, exactly as the pre-ledger `brain_capture` did), so a candidate
    always has provenance to point at. `source_ref` is an opaque external pointer
    (`agentconnect_attempt_123`) that WikiBrain stores and never resolves.
    """
    text = (text or "").strip()
    if not text:
        raise CandidateError("candidate text is empty")
    if proposed_by_type not in PROPOSER_TYPES:
        raise CandidateError(
            f"unknown proposer type {proposed_by_type!r}; "
            f"expected one of {', '.join(PROPOSER_TYPES)}")
    if not (proposed_by or "").strip():
        raise CandidateError("proposed_by is required (who is proposing this?)")

    # Safety runs *before* the text becomes an inbox artifact on disk or a row in
    # the ledger. A credential that is masked after it has been written to
    # `inbox/` has not been contained; it has been copied.
    verdict = safety.scan_for(repo, text, safety.MEMORY_CANDIDATE)
    if verdict.decision is safety.Decision.block:
        raise SafetyRefused(
            f"safety policy refused this capture: {verdict.reason()}", verdict)
    text = verdict.text
    quarantined = safety.at_least(verdict.decision, safety.Decision.quarantine)

    meta = dict(metadata or {})
    if not verdict.clean:
        # An audit-safe record that capture was attempted and what was seen. It
        # holds spans and rule names, never the matched value.
        meta["safety"] = verdict.summary()
    if quarantined:
        meta["quarantined"] = True

    if source_id is None:
        # ingest.capture files the inbox artifact + a `new` source and finalizes.
        source_id = ingest.capture(repo, harness or util.slug(proposed_by, 40), text)
    elif not repo.one("SELECT 1 FROM sources WHERE id = ?", (source_id,)):
        raise CandidateError(f"no source {refs.source(source_id)}")

    cur = repo.ex(
        """INSERT INTO memory_candidates
             (text, proposed_by, proposed_by_type, source_id, source_ref, task_id,
              proposed_scopes, tags, created_at, status, metadata)
           VALUES (?,?,?,?,?,?,?,?,?,'pending',?)""",
        (text, proposed_by, proposed_by_type, source_id, source_ref, task_id,
         scopes.dumps(proposed_scopes or []),
         json.dumps(sorted(tags or [])),
         util.now_iso(),
         json.dumps(meta, sort_keys=True)))
    cid = cur.lastrowid
    note = f"{refs.candidate(cid)} pending, proposed by {proposed_by}"
    if quarantined:
        note += f" [QUARANTINED: {verdict.reason()}]"
    elif verdict.redacted:
        note += f" [redacted: {verdict.reason()}]"
    repo.finalize("capture-candidate", note)
    return cid, verdict


# --- read -------------------------------------------------------------------
def get(repo: Repo, cid: int) -> dict:
    row = _require(repo, cid)
    out = dict(row)
    out["ref"] = refs.candidate(row["id"])
    out["proposed_scopes"] = [s.as_dict() for s in scopes.loads(row["proposed_scopes"])]
    out["tags"] = json.loads(row["tags"] or "[]")
    out["metadata"] = json.loads(row["metadata"] or "{}")
    if row["promoted_claim_id"]:
        out["promoted_claim"] = refs.claim(row["promoted_claim_id"])
    return out


def listing(repo: Repo, status: str | None = "pending", limit: int = 50) -> list[dict]:
    if status and status not in STATUSES:
        raise CandidateError(f"unknown status {status!r}")
    if status:
        rows = repo.q("SELECT * FROM memory_candidates WHERE status = ?"
                      " ORDER BY id LIMIT ?", (status, limit))
    else:
        rows = repo.q("SELECT * FROM memory_candidates ORDER BY id LIMIT ?", (limit,))
    return [get(repo, r["id"]) for r in rows]


# --- review (human-gated) ---------------------------------------------------
def _safety_gate(repo: Repo, row, *, reviewer: str, reviewer_type: str,
                 override: bool, override_reason: str | None) -> dict:
    """The second gate. Returns the metadata to record on the candidate.

    The human gate asks *should this be trusted*. This asks *is this safe to make
    trusted*, and the two are independent: a correct, well-sourced claim that
    carries a live credential fails here and should.

    A candidate quarantined at capture stays blocked even if the re-scan comes back
    quiet, because the reason it was quarantined — an injection lure, a tool-control
    directive — may have been masked, argued away, or simply not re-detected. The
    capture-time verdict is evidence, and evidence does not expire.
    """
    verdict = safety.scan_for(repo, row["text"], safety.MEMORY_PROMOTION)
    meta = json.loads(row["metadata"] or "{}")
    was_quarantined = bool(meta.get("quarantined"))
    blocked = verdict.decision is safety.Decision.block or was_quarantined

    if blocked and not override:
        why = verdict.reason() if not verdict.clean else "quarantined at capture"
        raise SafetyRefused(
            f"safety policy blocks promoting {refs.candidate(row['id'])}: {why}. "
            "Promoting it anyway requires an explicit override with a reason.",
            verdict)

    if override:
        if not blocked:
            raise CandidateError(
                "safety_override was passed but nothing is blocking this "
                "promotion; do not override a gate that is open")
        if not (override_reason or "").strip():
            raise CandidateError(
                "a safety override requires a reason (what did you verify?)")
        # The override records that a human accepted a known risk. It never
        # relabels the finding as clean, and the findings are retained verbatim.
        meta["safety_override"] = {
            "actor": reviewer, "actor_type": reviewer_type,
            "reason": override_reason.strip(), "at": util.now_iso(),
            "quarantined_at_capture": was_quarantined,
            "findings_at_promotion": verdict.summary(),
        }
    if not verdict.clean:
        meta["safety_at_promotion"] = verdict.summary()
    return meta


def promote(repo: Repo, cid: int, *, reviewer: str, confidence: str, scope: Scope,
            reviewer_type: str = "human", note: str | None = None,
            safety_override: bool = False,
            override_reason: str | None = None) -> int:
    """Promote a pending candidate into a scoped, trusted claim. Returns claim id.

    `reviewer_type` must not name an agent: promotion is the human gate, and an
    agent laundering its own proposal into trusted recall is precisely what this
    refuses. The MCP surface additionally hides these tools outside `--review`,
    but the check here holds even if a caller reaches the Python API directly.

    Safety is a *second* gate, checked after the human one. It can only ever
    subtract: passing it does not promote anything, and `safety_override` is
    available only to the same non-agent reviewers, requires a reason, and is
    recorded. There is no path by which a scanner makes a claim trusted.
    """
    if reviewer_type not in REVIEWER_TYPES:
        raise CandidateError(
            f"reviewer type {reviewer_type!r} may not promote; promotion is "
            f"human-gated (allowed: {', '.join(REVIEWER_TYPES)})")
    if not (reviewer or "").strip():
        raise CandidateError("reviewer is required (who is promoting this?)")
    label = confidence
    numeric = conf.to_numeric(label)  # raises ConfidenceError on a bad label

    row = _require(repo, cid)
    _require_reviewable(row, "promote")
    meta = _safety_gate(repo, row, reviewer=reviewer, reviewer_type=reviewer_type,
                        override=bool(safety_override),
                        override_reason=override_reason)

    src = repo.one("SELECT origin FROM sources WHERE id = ?", (row["source_id"],))
    if not src:
        raise CandidateError(
            f"candidate {refs.candidate(cid)} has no evidence source; refusing to "
            "promote a claim with no provenance")
    now = util.now_iso()
    cur = repo.ex(
        """INSERT INTO claims
             (text, source_id, confidence, origin, status, created_at, reviewed_at,
              scope_type, scope_id, tags, confidence_label, learned_at,
              last_verified_at, promoted_by, candidate_id)
           VALUES (?,?,?,?,'promoted',?,?,?,?,?,?,?,?,?,?)""",
        (row["text"], row["source_id"], numeric, src["origin"], now, now,
         scope.scope_type, scope.scope_id, row["tags"], label,
         row["created_at"], now, reviewer, cid))
    claim_id = cur.lastrowid
    repo.ex(
        """INSERT INTO claim_sources
             (claim_id, source_id, evidence_type, quote_or_pointer, created_at)
           VALUES (?,?,?,?,?)""",
        (claim_id, row["source_id"], PROMOTION_EVIDENCE_TYPE, row["source_ref"], now))
    repo.ex(
        """UPDATE memory_candidates
              SET status='promoted', promoted_claim_id=?, reviewed_at=?,
                  reviewed_by=?, review_reason=?, metadata=?
            WHERE id=?""",
        (claim_id, now, reviewer, note, json.dumps(meta, sort_keys=True), cid))
    line = (f"{refs.candidate(cid)} -> {refs.claim(claim_id)} "
            f"({scope}, {label}) by {reviewer}")
    if meta.get("safety_override"):
        line += " [SAFETY OVERRIDE]"
    repo.finalize("promote-candidate", line)
    return claim_id


def reject(repo: Repo, cid: int, *, reviewer: str, reason: str,
           reviewer_type: str = "human") -> None:
    if reviewer_type not in REVIEWER_TYPES:
        raise CandidateError(
            f"reviewer type {reviewer_type!r} may not reject; review is human-gated")
    if not (reason or "").strip():
        raise CandidateError("a rejection reason is required")
    row = _require(repo, cid)
    _require_reviewable(row, "reject")
    repo.ex(
        """UPDATE memory_candidates
              SET status='rejected', reviewed_at=?, reviewed_by=?, review_reason=?
            WHERE id=?""",
        (util.now_iso(), reviewer, reason, cid))
    repo.finalize("reject-candidate", f"{refs.candidate(cid)}: {reason}")


def archive(repo: Repo, cid: int, *, reviewer: str, reason: str = "") -> None:
    """Retire a candidate without judging it. Captured items are not permanent."""
    row = _require(repo, cid)
    _require_reviewable(row, "archive")
    repo.ex(
        """UPDATE memory_candidates
              SET status='archived', reviewed_at=?, reviewed_by=?, review_reason=?
            WHERE id=?""",
        (util.now_iso(), reviewer, reason or None, cid))
    repo.finalize("archive-candidate", refs.candidate(cid))
