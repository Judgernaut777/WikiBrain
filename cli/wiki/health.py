"""Phase 3 `wiki health`: single composite score (lower = better).

score = open_contradictions*3 + orphans + unsourced_promoted*5
        + stale_candidates + fetch_failures
Logged to log.md so the trend is greppable (BUILD_SPEC.md §5.2).
"""
from __future__ import annotations

from .db import Repo
from . import lint as lintmod


def compute(repo: Repo) -> dict:
    open_contradictions = repo.one(
        "SELECT COUNT(*) n FROM contradictions WHERE status='open'")["n"]

    # reuse lint's structural findings for orphans + stale candidates, but do
    # not let health mutate the queue: run the read-only parts directly.
    findings = lintmod.lint(repo)["findings"]
    orphans = sum(1 for f in findings if f["check"] == "orphan_page")
    stale_candidates = sum(1 for f in findings if f["check"] == "stale_candidate")

    # unsourced_promoted: promoted claims with no valid (non-quarantined) source
    unsourced_promoted = repo.one(
        """SELECT COUNT(*) n FROM claims c
           WHERE c.status='promoted' AND (
                 c.source_id IS NULL
              OR NOT EXISTS (SELECT 1 FROM sources s
                             WHERE s.id=c.source_id AND s.status!='quarantined'))""")["n"]

    fetch_failures = repo.one(
        "SELECT COUNT(*) n FROM sources WHERE status='failed'")["n"]

    score = (open_contradictions * 3 + orphans + unsourced_promoted * 5
             + stale_candidates + fetch_failures)
    breakdown = {
        "open_contradictions": open_contradictions,
        "orphans": orphans,
        "unsourced_promoted": unsourced_promoted,
        "stale_candidates": stale_candidates,
        "fetch_failures": fetch_failures,
        "score": score,
    }
    repo.log("health", f"score {score} | " + " ".join(
        f"{k}={v}" for k, v in breakdown.items() if k != "score"))
    repo.conn.commit()
    return breakdown
