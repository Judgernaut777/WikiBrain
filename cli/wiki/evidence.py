"""Evidence filing: keep raw primary sources navigable without losing hashes.

The DB remains the index of truth (`sources.path`), while files move from the
flat staging areas (`raw/`, `inbox/`) into deterministic buckets under `raw/`.
All moves verify the stored source hash before and after, so filing cannot
silently mutate evidence.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .db import Repo
from . import render as rendermod
from . import util


class EvidenceError(Exception):
    pass


BUCKETS = {
    "web", "documents", "images", "transcripts", "sessions",
    "datasets", "uncategorized",
}


def _year(src) -> str:
    stamp = src["ingested_at"] or src["fetched_at"] or util.now_iso()
    return stamp[:4] if len(stamp) >= 4 else "unknown"


def bucket_for(src) -> str:
    """Deterministic bucket from source metadata."""
    origin = src["origin"] or ""
    mime = src["mime_type"] or ""
    path = src["path"] or ""
    tags = []
    try:
        tags = json.loads(src["tags"] or "[]")
    except Exception:
        tags = []

    if origin.startswith("session/"):
        return "sessions"
    if origin == "transcript":
        return "transcripts"
    if mime.startswith("image/"):
        return "images"
    if src["url"]:
        return "web"
    if any(t.lower() in {"dataset", "data", "csv", "spreadsheet"} for t in tags):
        return "datasets"
    suffix = Path(path).suffix.lower()
    if suffix in {".csv", ".tsv", ".jsonl", ".parquet", ".xlsx", ".xls"}:
        return "datasets"
    if mime.startswith("application/") or suffix in {".pdf", ".docx", ".doc", ".pptx"}:
        return "documents"
    return "uncategorized"


def _source_path(repo: Repo, src) -> Path:
    p = repo.root / src["path"]
    try:
        p.resolve().relative_to(repo.root.resolve())
    except ValueError:
        raise EvidenceError(f"source #{src['id']} path escapes repo: {src['path']}")
    return p


def _verify_hash(src, path: Path) -> None:
    if not path.exists():
        raise EvidenceError(f"source #{src['id']} file missing: {path}")
    got = util.sha256_bytes(path.read_bytes())
    if got != src["hash"]:
        raise EvidenceError(
            f"source #{src['id']} hash mismatch at {path} "
            f"(expected {src['hash'][:12]}, got {got[:12]})")


def _target_path(repo: Repo, src) -> Path:
    bucket = bucket_for(src)
    year = _year(src)
    cur = Path(src["path"])
    name = cur.name
    # Keep a hash prefix in the filename if it is not already present. This makes
    # the filesystem navigable while the full hash remains in the DB.
    h8 = src["hash"][:8]
    if h8 not in name:
        name = f"{cur.stem}-{h8}{cur.suffix}"
    return repo.root / "raw" / bucket / year / name


def file_source(repo: Repo, source_id: int) -> dict:
    """Move one source artifact into its categorized raw bucket.

    Returns {source_id, bucket, old_path, new_path, moved}. Idempotent: if the
    source is already at its target path, only verifies the hash.
    """
    src = repo.one("SELECT * FROM sources WHERE id = ?", (source_id,))
    if not src:
        raise EvidenceError(f"no source #{source_id}")
    old = _source_path(repo, src)
    _verify_hash(src, old)
    target = _target_path(repo, src)
    bucket = bucket_for(src)
    if old.resolve() == target.resolve():
        return {
            "source_id": source_id, "bucket": bucket,
            "old_path": src["path"], "new_path": src["path"], "moved": False,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # If exact same content is already there, reuse it. Otherwise choose a
        # deterministic collision suffix.
        if util.sha256_bytes(target.read_bytes()) == src["hash"]:
            old.unlink()
        else:
            target = target.with_name(f"{target.stem}-{source_id}{target.suffix}")
            if target.exists():
                raise EvidenceError(f"target already exists: {target}")
            shutil.move(str(old), str(target))
    else:
        shutil.move(str(old), str(target))
    _verify_hash(src, target)
    new_rel = repo.rel(target)
    repo.ex("UPDATE sources SET path = ? WHERE id = ?", (new_rel, source_id))
    rendermod.mark_dirty_for_source(repo, source_id)
    return {
        "source_id": source_id, "bucket": bucket,
        "old_path": src["path"], "new_path": new_rel, "moved": True,
    }


def file_all(repo: Repo, *, extracted_only: bool = True) -> list[dict]:
    if extracted_only:
        rows = repo.q("SELECT id FROM sources WHERE status != 'new' ORDER BY id")
    else:
        rows = repo.q("SELECT id FROM sources ORDER BY id")
    out = []
    for r in rows:
        try:
            out.append(file_source(repo, r["id"]))
        except EvidenceError as e:
            out.append({
                "source_id": r["id"], "bucket": None,
                "old_path": None, "new_path": None,
                "moved": False, "error": str(e),
            })
    return out


def write_index(repo: Repo) -> str:
    """Write raw/INDEX.md from the sources table."""
    rows = repo.q(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM claims c WHERE c.source_id=s.id) AS claims,
                  (SELECT COUNT(*) FROM claims c
                   WHERE c.source_id=s.id AND c.status='promoted') AS promoted
           FROM sources s ORDER BY s.id""")
    out = [
        "# Raw Evidence Index",
        "",
        "Generated by `wiki evidence index`. The database `sources` table is the "
        "canonical index; this file is a navigable projection.",
        "",
        "| id | bucket | status | origin | title | path | claims | promoted | hash |",
        "|---:|---|---|---|---|---|---:|---:|---|",
    ]
    for r in rows:
        bucket = bucket_for(r)
        title = (r["title"] or f"source #{r['id']}").replace("|", "\\|")
        path = r["path"].replace("|", "\\|")
        out.append(
            f"| {r['id']} | {bucket} | {r['status']} | {r['origin']} | "
            f"{title} | [{path}]({path}) | {r['claims']} | {r['promoted']} | "
            f"`{r['hash'][:12]}` |")
    fp = repo.root / "raw" / "INDEX.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return repo.rel(fp)
