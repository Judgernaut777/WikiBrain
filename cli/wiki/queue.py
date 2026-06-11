"""research_queue management: add | list | next | done | attempt."""
from __future__ import annotations

from .db import Repo
from . import util

PARK_AFTER = 3


def add(repo: Repo, question: str, *, priority: float = 0.5, origin: str = "user") -> int:
    question = question.strip()
    if not question:
        raise SystemExit("error: empty question")
    cur = repo.ex(
        """INSERT INTO research_queue(question, priority, origin, status, created_at)
           VALUES (?,?,?, 'open', ?)""",
        (question, priority, origin, util.now_iso()),
    )
    repo.finalize("queue-add", f"#{cur.lastrowid} ({origin}) {question[:60]}")
    return cur.lastrowid


def listing(repo: Repo, status: str | None = None) -> list:
    if status:
        return repo.q(
            "SELECT * FROM research_queue WHERE status = ? ORDER BY priority DESC, id",
            (status,),
        )
    return repo.q("SELECT * FROM research_queue ORDER BY status, priority DESC, id")


def next_open(repo: Repo):
    return repo.one(
        """SELECT * FROM research_queue WHERE status = 'open'
           ORDER BY priority DESC, id LIMIT 1"""
    )


def done(repo: Repo, qid: int, note: str | None = None) -> None:
    row = repo.one("SELECT * FROM research_queue WHERE id = ?", (qid,))
    if not row:
        raise SystemExit(f"error: no queue item #{qid}")
    repo.ex("UPDATE research_queue SET status = 'done' WHERE id = ?", (qid,))
    summary = f"#{qid} done" + (f" | {note}" if note else "")
    repo.finalize("queue-done", summary)


def attempt(repo: Repo, qid: int) -> str:
    """Increment attempts; park at PARK_AFTER. Returns new status."""
    row = repo.one("SELECT * FROM research_queue WHERE id = ?", (qid,))
    if not row:
        raise SystemExit(f"error: no queue item #{qid}")
    attempts = (row["attempts"] or 0) + 1
    status = "parked" if attempts >= PARK_AFTER else row["status"]
    repo.ex("UPDATE research_queue SET attempts = ?, status = ? WHERE id = ?",
            (attempts, status, qid))
    repo.finalize("queue-attempt", f"#{qid} attempts={attempts} status={status}")
    return status
