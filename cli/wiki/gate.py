"""Phase 5 two-speed gate (BUILD_SPEC.md §7.1).

Auto-promotes a pending claim iff ALL hold:
  - confidence >= gate.auto_promote_confidence
  - no open contradiction touches it
  - corroborated: >= 2 independent sources assert it, OR origin == 'clip'
  - it does not conflict with a promoted claim (approximated via the
    contradiction check, plus an explicit opposite-polarity FTS scan)
Everything else stays pending for the human. Pure code; no model calls.
"""
from __future__ import annotations

from .db import Repo
from . import render, util

CORROBORATION_JACCARD = 0.5


def _has_open_contradiction(repo: Repo, cid: int) -> bool:
    return bool(repo.one(
        "SELECT 1 FROM contradictions WHERE status='open' AND (claim_a=? OR claim_b=?)",
        (cid, cid)))


def _corroborating_sources(repo: Repo, claim) -> int:
    """Distinct source ids (incl. this claim's) asserting a similar fact."""
    try:
        rows = repo.q(
            """SELECT c.id, c.text, c.source_id FROM claims_fts f
               JOIN claims c ON c.id = f.rowid
               WHERE claims_fts MATCH ? AND c.status IN ('promoted','pending')""",
            (util.fts_or_query(claim["text"]),))
    except Exception:
        rows = []
    sources = {claim["source_id"]}
    for r in rows:
        if r["id"] == claim["id"]:
            continue
        if util.jaccard(claim["text"], r["text"]) >= CORROBORATION_JACCARD:
            sources.add(r["source_id"])
    return len(sources)


def _conflicts_with_promoted(repo: Repo, claim) -> bool:
    try:
        rows = repo.q(
            """SELECT c.text FROM claims_fts f JOIN claims c ON c.id = f.rowid
               WHERE claims_fts MATCH ? AND c.status='promoted'""",
            (util.fts_or_query(claim["text"]),))
    except Exception:
        rows = []
    neg = util.has_negation(claim["text"])
    for r in rows:
        if util.jaccard(claim["text"], r["text"]) >= 0.4 and util.has_negation(r["text"]) != neg:
            return True
    return False


def gate(repo: Repo) -> dict:
    thresh = float(repo.cfg.gate("auto_promote_confidence"))
    pending = repo.q("SELECT * FROM claims WHERE status = 'pending' ORDER BY id")
    promoted, held = [], []
    for c in pending:
        reasons = []
        if c["confidence"] < thresh:
            reasons.append(f"confidence {c['confidence']:.2f} < {thresh}")
        if _has_open_contradiction(repo, c["id"]):
            reasons.append("open contradiction")
        corroborated = c["origin"] == "clip" or _corroborating_sources(repo, c) >= 2
        if not corroborated:
            reasons.append("not corroborated (need 2 sources or origin=clip)")
        if _conflicts_with_promoted(repo, c):
            reasons.append("conflicts with promoted claim")
        if reasons:
            held.append({"id": c["id"], "reasons": reasons})
            continue
        repo.ex("UPDATE claims SET status='promoted', reviewed_at=? WHERE id=?",
                (util.now_iso(), c["id"]))
        render.mark_dirty_for_claim(repo, c["id"])
        promoted.append(c["id"])

    if promoted:
        repo.finalize("gate", f"auto-promoted {len(promoted)}; held {len(held)}")
    else:
        repo.conn.commit()
    return {"promoted": promoted, "held": held}
