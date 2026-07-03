"""Local-embedding semantic search (the optional [semantic] extra).

LOCAL and key-free: a sentence-transformers model runs on your machine — no
billable LLM call, no API key, honoring the CLI boundary. Embeddings affect
*ranking only*, never the byte-deterministic render layer. Heavy deps
(sentence-transformers -> torch, numpy) are import-guarded so the core CLI runs
without them.

Vectors are L2-normalized and stored as packed float32 BLOBs, so cosine
similarity is a plain dot product. Brute-force over the claim set — fine at
personal-knowledge-base scale; no faiss/sqlite-vec dependency.
"""
from __future__ import annotations

from .db import Repo
from . import util

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_MODEL_CACHE: dict = {}


class EmbedError(Exception):
    pass


def _np():
    try:
        import numpy as np  # type: ignore
        return np
    except ImportError as e:
        raise EmbedError(
            "semantic search needs the [semantic] extra: pip install '.[semantic]'") from e


def _model(name: str):
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:
        raise EmbedError(
            "semantic search needs the [semantic] extra: pip install '.[semantic]'") from e
    if name not in _MODEL_CACHE:
        _MODEL_CACHE[name] = SentenceTransformer(name)
    return _MODEL_CACHE[name]


def _model_name(repo: Repo) -> str:
    return repo.cfg.embed_cfg("model") or DEFAULT_MODEL


def index(repo: Repo, *, only_missing: bool = True) -> int:
    """Embed claims into the embeddings table. Returns the number indexed."""
    np = _np()
    name = _model_name(repo)
    model = _model(name)
    if only_missing:
        rows = repo.q("SELECT id, text FROM claims WHERE id NOT IN "
                      "(SELECT claim_id FROM embeddings) ORDER BY id")
    else:
        rows = repo.q("SELECT id, text FROM claims ORDER BY id")
    if not rows:
        return 0
    vecs = model.encode([r["text"] for r in rows], normalize_embeddings=True)
    for r, v in zip(rows, vecs):
        arr = np.asarray(v, dtype=np.float32)
        repo.ex("INSERT OR REPLACE INTO embeddings(claim_id, model, dim, vec, created_at) "
                "VALUES (?,?,?,?,?)", (r["id"], name, int(arr.shape[0]),
                                       arr.tobytes(), util.now_iso()))
    repo.finalize("embed", f"indexed {len(rows)} claim(s)")
    return len(rows)


def semantic_search(repo: Repo, query: str, k: int = 10,
                    *, promoted_only: bool = False) -> list[dict]:
    """Top-k claims by cosine similarity to the query."""
    np = _np()
    name = _model_name(repo)
    sql = ("SELECT e.claim_id, e.vec, e.dim, c.text, c.status, c.source_id, c.origin "
           "FROM embeddings e JOIN claims c ON c.id = e.claim_id WHERE e.model = ?")
    params = [name]
    if promoted_only:
        sql += " AND c.status = 'promoted'"
    rows = repo.q(sql, params)
    if not rows:
        return []
    qv = np.asarray(_model(name).encode(
        [query], normalize_embeddings=True)[0], dtype=np.float32)
    scored = []
    for r in rows:
        if r["dim"] != qv.shape[0]:
            continue  # stale/mismatched vector (e.g. leftover from a prior model)
        v = np.frombuffer(r["vec"], dtype=np.float32)
        scored.append((float(np.dot(qv, v)), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"kind": "claim", "id": r["claim_id"], "text": r["text"],
             "status": r["status"], "source_id": r["source_id"],
             "origin": r["origin"], "score": round(s, 4)}
            for s, r in scored[:k]]


def hybrid_search(repo: Repo, query: str, k: int = 10,
                  *, promoted_only: bool = False) -> list[dict]:
    """Reciprocal-rank-fusion of keyword (FTS) and semantic results."""
    from . import search as searchmod
    fts = [r for r in searchmod.search(repo, query, promoted_only=promoted_only)
           if r.get("kind") == "claim"]
    sem = semantic_search(repo, query, k=max(k * 2, 20), promoted_only=promoted_only)
    score: dict = {}
    info: dict = {}
    for i, r in enumerate(fts):
        score[r["id"]] = score.get(r["id"], 0.0) + 1.0 / (60 + i)
        info[r["id"]] = r
    for i, r in enumerate(sem):
        score[r["id"]] = score.get(r["id"], 0.0) + 1.0 / (60 + i)
        info.setdefault(r["id"], r)
    ranked = sorted(score, key=lambda cid: score[cid], reverse=True)[:k]
    return [{**info[cid], "rrf": round(score[cid], 4)} for cid in ranked]
