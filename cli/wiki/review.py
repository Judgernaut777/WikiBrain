"""Human/maintain-pass operations: promote, reject, supersede, contradictions,
escalations, summary promotion. These are the judgment levers the morning gate
(maintain.md) pulls; the CLI only performs the bookkeeping.
"""
from __future__ import annotations

from .db import Repo
from . import render, util


def _require_claim(repo: Repo, cid: int):
    row = repo.one("SELECT * FROM claims WHERE id = ?", (cid,))
    if not row:
        raise SystemExit(f"error: no claim #{cid}")
    return row


def promote(repo: Repo, cids: list[int]) -> None:
    for cid in cids:
        _require_claim(repo, cid)
        repo.ex("UPDATE claims SET status='promoted', reviewed_at=? WHERE id=?",
                (util.now_iso(), cid))
        render.mark_dirty_for_claim(repo, cid)
    repo.finalize("promote", "claims " + ",".join(f"#{c}" for c in cids))


def reject(repo: Repo, cids: list[int]) -> None:
    for cid in cids:
        _require_claim(repo, cid)
        repo.ex("UPDATE claims SET status='rejected', reviewed_at=? WHERE id=?",
                (util.now_iso(), cid))
        render.mark_dirty_for_claim(repo, cid)
    repo.finalize("reject", "claims " + ",".join(f"#{c}" for c in cids))


def supersede(repo: Repo, old_id: int, new_id: int) -> None:
    old = _require_claim(repo, old_id)
    new = _require_claim(repo, new_id)
    # session/* and autoresearch claims may never auto-supersede; this is a
    # human/maintain action so it is allowed, but we record provenance.
    repo.ex("UPDATE claims SET status='superseded', superseded_by=?, reviewed_at=? WHERE id=?",
            (new_id, util.now_iso(), old_id))
    if new["status"] == "pending":
        repo.ex("UPDATE claims SET status='promoted', reviewed_at=? WHERE id=?",
                (util.now_iso(), new_id))
    render.mark_dirty_for_claim(repo, old_id)
    render.mark_dirty_for_claim(repo, new_id)
    repo.finalize("supersede", f"#{old_id} superseded by #{new_id}")


def promote_summary(repo: Repo, source_id: int) -> None:
    row = repo.one("SELECT * FROM summaries WHERE source_id = ?", (source_id,))
    if not row:
        raise SystemExit(f"error: no summary for source #{source_id}")
    repo.ex("UPDATE summaries SET status='promoted' WHERE source_id = ?", (source_id,))
    render.mark_dirty_for_source(repo, source_id)
    repo.finalize("promote-summary", f"source #{source_id}")


# --- contradictions ---------------------------------------------------------
def contradiction_list(repo: Repo, status: str | None = "open") -> list:
    if status:
        return repo.q("SELECT * FROM contradictions WHERE status = ? ORDER BY id", (status,))
    return repo.q("SELECT * FROM contradictions ORDER BY id")


def contradiction_propose(repo: Repo, cid: int, proposal: str) -> None:
    if not repo.one("SELECT 1 FROM contradictions WHERE id = ?", (cid,)):
        raise SystemExit(f"error: no contradiction #{cid}")
    repo.ex("UPDATE contradictions SET proposal = ? WHERE id = ?", (proposal, cid))
    repo.finalize("contradiction-propose", f"#{cid}")


def contradiction_resolve(repo: Repo, cid: int, resolution: str) -> None:
    row = repo.one("SELECT * FROM contradictions WHERE id = ?", (cid,))
    if not row:
        raise SystemExit(f"error: no contradiction #{cid}")
    repo.ex("UPDATE contradictions SET status='resolved', resolution=? WHERE id=?",
            (resolution, cid))
    render.mark_dirty_for_claim(repo, row["claim_a"])
    render.mark_dirty_for_claim(repo, row["claim_b"])
    repo.finalize("contradiction-resolve", f"#{cid}")


# --- escalations ------------------------------------------------------------
def escalation_list(repo: Repo, status: str | None = "open") -> list:
    if status:
        return repo.q("SELECT * FROM escalations WHERE status = ? ORDER BY id", (status,))
    return repo.q("SELECT * FROM escalations ORDER BY id")


def escalation_close(repo: Repo, eid: int) -> None:
    if not repo.one("SELECT 1 FROM escalations WHERE id = ?", (eid,)):
        raise SystemExit(f"error: no escalation #{eid}")
    repo.ex("UPDATE escalations SET status='closed' WHERE id = ?", (eid,))
    repo.finalize("escalation-close", f"#{eid}")
