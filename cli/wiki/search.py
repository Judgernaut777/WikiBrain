"""FTS search over claims + summaries, and graph walk over relations."""
from __future__ import annotations

from collections import deque

from .db import Repo
from .entities import find_entity
from . import util


def search(repo: Repo, terms: str, *, promoted_only: bool = False) -> list[dict]:
    q = util.fts_query(terms)
    results: list[dict] = []

    claim_sql = """
        SELECT c.id, c.text, c.status, c.origin, c.confidence,
               s.id AS source_id, s.title AS source_title, s.path AS source_path
        FROM claims_fts f
        JOIN claims c ON c.id = f.rowid
        JOIN sources s ON s.id = c.source_id
        WHERE claims_fts MATCH ?
    """
    if promoted_only:
        claim_sql += " AND c.status = 'promoted'"
    claim_sql += " ORDER BY c.id"
    for r in repo.q(claim_sql, (q,)):
        results.append({
            "kind": "claim", "id": r["id"], "text": r["text"],
            "status": r["status"], "origin": r["origin"],
            "confidence": r["confidence"],
            "source_id": r["source_id"], "source_title": r["source_title"],
            "source_path": r["source_path"],
        })

    sum_sql = """
        SELECT su.id, su.text, su.status, s.id AS source_id,
               s.title AS source_title, s.path AS source_path
        FROM summaries_fts f
        JOIN summaries su ON su.id = f.rowid
        JOIN sources s ON s.id = su.source_id
        WHERE summaries_fts MATCH ?
    """
    if promoted_only:
        sum_sql += " AND su.status = 'promoted'"
    sum_sql += " ORDER BY su.id"
    for r in repo.q(sum_sql, (q,)):
        snippet = r["text"]
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        results.append({
            "kind": "summary", "id": r["id"], "text": snippet,
            "status": r["status"], "origin": None, "confidence": None,
            "source_id": r["source_id"], "source_title": r["source_title"],
            "source_path": r["source_path"],
        })
    return results


def graph(repo: Repo, entity_name: str, hops: int = 1,
          *, promoted_only: bool = False) -> dict:
    start = find_entity(repo, entity_name)
    if not start:
        raise SystemExit(f"error: no entity named {entity_name!r}")

    names = {row["id"]: row["name"] for row in repo.q("SELECT id, name FROM entities")}
    # Mirror the renderer (render.py): an edge's evidence is valid if its claim
    # is promoted, or there is no evidence claim (claim_id is NULL). When
    # promoted_only, unvetted edges are neither emitted nor traversed.
    promoted = (None if not promoted_only else
                {r["id"] for r in repo.q("SELECT id FROM claims WHERE status='promoted'")})

    def evidence_ok(claim_id):
        return promoted is None or claim_id is None or claim_id in promoted

    edges = []
    seen_edges = set()
    visited = {start["id"]}
    frontier = deque([(start["id"], 0)])
    while frontier:
        node, depth = frontier.popleft()
        if depth >= hops:
            continue
        rows = repo.q(
            """SELECT src, rel, dst, claim_id FROM relations
               WHERE src = ? OR dst = ?
               ORDER BY rel, dst, src""",
            (node, node),
        )
        for r in rows:
            if not evidence_ok(r["claim_id"]):
                continue
            key = (r["src"], r["rel"], r["dst"], r["claim_id"])
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({
                "src": names.get(r["src"], "?"),
                "rel": r["rel"],
                "dst": names.get(r["dst"], "?"),
                "claim_id": r["claim_id"],
                "depth": depth + 1,
            })
            for nxt in (r["src"], r["dst"]):
                if nxt not in visited:
                    visited.add(nxt)
                    frontier.append((nxt, depth + 1))
    return {"entity": start["name"], "hops": hops, "edges": edges}
