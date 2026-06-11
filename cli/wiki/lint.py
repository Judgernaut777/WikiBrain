"""Phase 3 `wiki lint`: structural checks over DB + generated wiki.

Each check yields machine-readable findings; question-shaped findings are
auto-appended to research_queue (origin 'lint'). Pure code.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .db import Repo
from .render import _entity_input_hash, _qualifying_entities
from . import util

WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# Secret-shaped patterns (avoid matching the prose "API key" in docs).
_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b(?:ANTHROPIC|OPENAI)_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
]
# Directories not authored by this project — skip when scanning for secrets.
_SCAN_SKIP = {".git", ".venv", "venv", "raw", "inbox", "wiki", "db", "__pycache__"}


def _scan_api_keys(repo) -> list[dict]:
    out = []
    for fp in repo.root.rglob("*"):
        if not fp.is_file():
            continue
        parts = set(fp.relative_to(repo.root).parts)
        if parts & _SCAN_SKIP:
            continue
        if fp.suffix in {".pyc", ".db", ".sqlite", ".png", ".jpg"}:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in _KEY_PATTERNS:
            if pat.search(text):
                out.append({"check": "api_key_present",
                            "page": fp.relative_to(repo.root).as_posix(),
                            "message": f"possible API key in {fp.relative_to(repo.root).as_posix()} "
                                       "(subscription-only: no keys allowed)"})
                break
    return out


def _iso_age_days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _wiki_pages(repo: Repo) -> dict[str, str]:
    """stem -> relative path for every generated .md page."""
    out = {}
    wikidir = repo.root / "wiki"
    for fp in wikidir.rglob("*.md"):
        out[fp.stem] = fp.relative_to(repo.root).as_posix()
    return out


def lint(repo: Repo) -> dict:
    findings: list[dict] = []
    pages = _wiki_pages(repo)
    stems = set(pages)

    # 1. broken wikilinks
    inbound: dict[str, set[str]] = {s: set() for s in stems}
    for stem, rel in pages.items():
        text = (repo.root / rel).read_text(encoding="utf-8")
        for m in WIKILINK.finditer(text):
            target = m.group(1).strip()
            if target not in stems:
                findings.append({"check": "broken_wikilink", "page": rel,
                                 "target": target,
                                 "message": f"{rel} links to missing [[{target}]]"})
            else:
                inbound[target].add(stem)

    # 2. orphan pages (entity/concept/synthesis with no inbound wikilink)
    for p in repo.q("SELECT * FROM pages WHERE kind IN ('entity','concept','synthesis')"):
        stem = p["path"].split("/")[-1][:-3]
        if stem in inbound and not inbound[stem]:
            findings.append({"check": "orphan_page", "page": p["path"],
                             "message": f"orphan: nothing links to {p['path']}"})

    # 3. entities with promoted claims but no page
    have_pages = {r["entity_id"] for r in repo.q(
        "SELECT entity_id FROM pages WHERE entity_id IS NOT NULL")}
    for eid in _qualifying_entities(repo):
        if eid not in have_pages:
            ent = repo.one("SELECT name FROM entities WHERE id=?", (eid,))
            findings.append({"check": "entity_without_page", "entity_id": eid,
                             "message": f"entity '{ent['name']}' has promoted claims but no page"})

    # 4. promoted claims whose source is quarantined
    for r in repo.q(
        """SELECT c.id, s.id AS sid FROM claims c JOIN sources s ON s.id=c.source_id
           WHERE c.status='promoted' AND s.status='quarantined'"""):
        findings.append({"check": "promoted_from_quarantined", "claim_id": r["id"],
                         "message": f"promoted claim #{r['id']} comes from quarantined source #{r['sid']}"})

    # 5. stale candidates: old promoted claim with newer pending claim on same entity
    stale_days = float(repo.cfg.lint_cfg("stale_days"))
    questions = []
    for r in repo.q("SELECT * FROM claims WHERE status='promoted'"):
        age = _iso_age_days(r["created_at"])
        if age is None or age < stale_days:
            continue
        newer = repo.one(
            """SELECT c2.id FROM claims c2
               JOIN claim_entities a ON a.claim_id = c2.id
               JOIN claim_entities b ON b.entity_id = a.entity_id
               WHERE b.claim_id = ? AND c2.status='pending'
                 AND c2.created_at > ? LIMIT 1""",
            (r["id"], r["created_at"]))
        if newer:
            findings.append({"check": "stale_candidate", "claim_id": r["id"],
                             "message": f"promoted claim #{r['id']} ({age:.0f}d old) has newer pending rival #{newer['id']}"})
            questions.append(f"Is promoted claim #{r['id']} superseded by newer pending claim #{newer['id']}?")

    # 6. contradictions open beyond threshold (age proxy = newest involved claim)
    cdays = float(repo.cfg.lint_cfg("contradiction_days"))
    for r in repo.q("SELECT * FROM contradictions WHERE status='open'"):
        ages = []
        for k in ("claim_a", "claim_b"):
            cr = repo.one("SELECT created_at FROM claims WHERE id=?", (r[k],))
            a = _iso_age_days(cr["created_at"]) if cr else None
            if a is not None:
                ages.append(a)
        age = min(ages) if ages else None
        if age is not None and age > cdays:
            findings.append({"check": "stale_contradiction", "contradiction_id": r["id"],
                             "message": f"contradiction #{r['id']} open {age:.0f}d (> {cdays:.0f})"})
            questions.append(f"Resolve open contradiction #{r['id']} (claims #{r['claim_a']} vs #{r['claim_b']}).")

    # 7. synthesis input changed but not reviewed
    for p in repo.q("SELECT * FROM pages WHERE kind IN ('entity','concept')"):
        cur = _entity_input_hash(repo, p["entity_id"])
        if p["synthesis_input_hash"] != cur:
            has_syn = bool(p["synthesis"].strip())
            findings.append({
                "check": "synthesis_unreviewed", "page": p["path"],
                "has_synthesis": has_syn,
                "message": (f"{p['path']}: inputs changed since synthesis "
                            + ("was written" if has_syn else "(never written)"))})

    # 8. billing guardrail (BUILD_SPEC §10): no API keys in repo/config/env.
    for f in _scan_api_keys(repo):
        findings.append(f)

    # auto-append question findings to research_queue (origin lint)
    appended = 0
    for q in questions:
        if repo.one("SELECT 1 FROM research_queue WHERE question=? AND status='open'", (q,)):
            continue
        repo.ex("""INSERT INTO research_queue(question, priority, origin, status, created_at)
                   VALUES (?, 0.6, 'lint', 'open', ?)""", (q, util.now_iso()))
        appended += 1
    if appended:
        repo.finalize("lint", f"{len(findings)} findings; +{appended} queue items")
    else:
        repo.conn.commit()

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["check"]] = counts.get(f["check"], 0) + 1
    return {"findings": findings, "counts": counts, "queued": appended}
