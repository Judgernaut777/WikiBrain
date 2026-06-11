"""Phase 2 renderer: DB → deterministic Obsidian markdown.

Output is byte-deterministic given identical DB state + synthesis text. The only
free prose is pages.synthesis, injected verbatim between markers. No model calls.
Wall-clock time never appears in page bodies; frontmatter `updated` is derived
from claim timestamps so re-rendering an unchanged DB yields zero git diff.
"""
from __future__ import annotations

import json
from pathlib import Path

from .db import Repo
from .entities import page_kind_for
from . import util

SYNTH_START = "<!-- synthesis:start -->"
SYNTH_END = "<!-- synthesis:end -->"


# --- dirty tracking (imported by review/gate) -------------------------------
def mark_dirty_path(repo: Repo, path: str):
    repo.ex("UPDATE pages SET dirty = 1 WHERE path = ?", (path,))


def mark_dirty_for_entity(repo: Repo, entity_id: int):
    repo.ex("UPDATE pages SET dirty = 1 WHERE entity_id = ?", (entity_id,))


def mark_dirty_for_source(repo: Repo, source_id: int):
    path = _source_page_path(repo, source_id)
    mark_dirty_path(repo, path)


def mark_dirty_for_claim(repo: Repo, claim_id: int):
    """Mark every page that depends on this claim dirty."""
    row = repo.one("SELECT source_id FROM claims WHERE id = ?", (claim_id,))
    if row:
        mark_dirty_for_source(repo, row["source_id"])
    for r in repo.q("SELECT entity_id FROM claim_entities WHERE claim_id = ?", (claim_id,)):
        mark_dirty_for_entity(repo, r["entity_id"])
    # relations whose evidence is this claim touch both endpoints
    for r in repo.q("SELECT src, dst FROM relations WHERE claim_id = ?", (claim_id,)):
        mark_dirty_for_entity(repo, r["src"])
        mark_dirty_for_entity(repo, r["dst"])


# --- path helpers -----------------------------------------------------------
def _unique_slug(repo: Repo, base: str, want_path_prefix: str, page_id_hint=None) -> str:
    s = base
    n = 1
    while True:
        path = f"{want_path_prefix}/{s}.md"
        existing = repo.one("SELECT id, entity_id FROM pages WHERE path = ?", (path,))
        if not existing:
            return s
        n += 1
        s = f"{base}-{n}"


def _entity_page_path(repo: Repo, entity_row) -> str:
    existing = repo.one("SELECT path FROM pages WHERE entity_id = ?", (entity_row["id"],))
    if existing:
        return existing["path"]
    sub = "concepts" if page_kind_for(entity_row["kind"]) == "concept" else "entities"
    s = _unique_slug(repo, util.slug(entity_row["name"]), f"wiki/{sub}")
    return f"wiki/{sub}/{s}.md"


def _source_page_path(repo: Repo, source_id: int) -> str:
    # Source pages have no entity_id; their source is recorded via a marker in
    # synthesis_input_hash ('src:<id>'), keeping the path stable across renders.
    row = repo.one("SELECT path FROM pages WHERE kind='source' AND synthesis_input_hash = ?",
                   (f"src:{source_id}",))
    if row:
        return row["path"]
    src = repo.one("SELECT * FROM sources WHERE id = ?", (source_id,))
    base = util.slug(src["title"] or f"source-{source_id}")
    s = _unique_slug(repo, base, "wiki/sources")
    return f"wiki/sources/{s}.md"


# --- frontmatter ------------------------------------------------------------
def _yaml(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        if isinstance(v, list):
            inner = ", ".join(json.dumps(x) for x in v)
            lines.append(f"{k}: [{inner}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _rel_link(from_path: str, to_path: str) -> str:
    import posixpath
    rel = posixpath.relpath(to_path, posixpath.dirname(from_path))
    return rel


# --- content builders -------------------------------------------------------
def _entity_updated(repo: Repo, entity_id: int) -> str:
    row = repo.one(
        """SELECT MAX(COALESCE(c.reviewed_at, c.created_at)) AS u
           FROM claims c JOIN claim_entities ce ON ce.claim_id = c.id
           WHERE ce.entity_id = ? AND c.status = 'promoted'""",
        (entity_id,),
    )
    return (row["u"] if row and row["u"] else "")


def _entity_input_hash(repo: Repo, entity_id: int) -> str:
    cids = [r["id"] for r in repo.q(
        """SELECT c.id FROM claims c JOIN claim_entities ce ON ce.claim_id = c.id
           WHERE ce.entity_id = ? AND c.status='promoted' ORDER BY c.id""",
        (entity_id,))]
    rids = [r["id"] for r in repo.q(
        """SELECT id FROM relations WHERE src = ? OR dst = ? ORDER BY id""",
        (entity_id, entity_id))]
    return util.sha256_text(
        "c:" + ",".join(map(str, cids)) + "|r:" + ",".join(map(str, rids)))


def _render_entity(repo: Repo, page) -> tuple[str, str, str]:
    """Return (content, input_hash, updated)."""
    ent = repo.one("SELECT * FROM entities WHERE id = ?", (page["entity_id"],))
    path = page["path"]
    claims = repo.q(
        """SELECT c.*, s.title AS s_title, s.origin AS s_origin, s.id AS s_id
           FROM claims c JOIN claim_entities ce ON ce.claim_id = c.id
           JOIN sources s ON s.id = c.source_id
           WHERE ce.entity_id = ? AND c.status = 'promoted'
           ORDER BY c.id""",
        (ent["id"],))
    source_count = len({c["s_id"] for c in claims})
    updated = _entity_updated(repo, ent["id"])
    tags = [ent["kind"]]

    fm = _yaml({
        "name": json.dumps(ent["name"]),
        "kind": ent["kind"],
        "tags": tags,
        "source_count": source_count,
        "updated": updated or '""',
    })

    out = [fm, "", f"# {ent['name']}", ""]
    # infobox
    aliases = json.loads(ent["aliases"] or "[]")
    out += ["| field | value |", "|---|---|",
            f"| kind | {ent['kind']} |",
            f"| aliases | {', '.join(aliases) if aliases else '—'} |",
            f"| sources | {source_count} |", ""]

    # synthesis block
    out += [SYNTH_START]
    if page["synthesis"].strip():
        out += [page["synthesis"].rstrip()]
    out += [SYNTH_END, ""]

    # promoted claims
    out += ["## Claims", ""]
    if claims:
        for c in claims:
            src_path = _source_page_path(repo, c["s_id"])
            link = _rel_link(path, src_path)
            title = c["s_title"] or f"source #{c['s_id']}"
            out.append(
                f"- {c['text']} ([{title}]({link}), origin: {c['s_origin']}) "
                f"`#{c['id']}`")
    else:
        out.append("_No promoted claims yet._")
    out += [""]

    # relations grouped by rel type
    rels = repo.q(
        """SELECT r.rel, r.src, r.dst, r.claim_id,
                  es.name AS src_name, es.kind AS src_kind,
                  ed.name AS dst_name, ed.kind AS dst_kind
           FROM relations r
           JOIN entities es ON es.id = r.src
           JOIN entities ed ON ed.id = r.dst
           WHERE (r.src = ? OR r.dst = ?)
           ORDER BY r.rel, ed.name, es.name, r.id""",
        (ent["id"], ent["id"]))
    # only relations whose evidence claim is promoted or null
    def evidence_ok(claim_id):
        if claim_id is None:
            return True
        row = repo.one("SELECT status FROM claims WHERE id = ?", (claim_id,))
        return bool(row) and row["status"] == "promoted"
    rels = [r for r in rels if evidence_ok(r["claim_id"])]
    out += ["## Relations", ""]
    if rels:
        cur_rel = None
        for r in rels:
            if r["rel"] != cur_rel:
                cur_rel = r["rel"]
                out.append(f"### {cur_rel}")
            if r["src"] == ent["id"]:
                other_name, other_kind = r["dst_name"], r["dst_kind"]
                arrow = "→"
            else:
                other_name, other_kind = r["src_name"], r["src_kind"]
                arrow = "←"
            other_page = _entity_page_path(
                repo, {"id": r["src"] if r["src"] != ent["id"] else r["dst"],
                       "name": other_name, "kind": other_kind})
            other_slug = Path(other_page).stem
            ev = f" `#{r['claim_id']}`" if r["claim_id"] else ""
            out.append(f"- {arrow} [[{other_slug}|{other_name}]]{ev}")
    else:
        out.append("_None._")
    out += [""]

    # open questions
    questions = repo.q("SELECT id, question FROM research_queue WHERE status='open' ORDER BY id")
    matched = [qr for qr in questions if ent["name"].lower() in qr["question"].lower()]
    out += ["## Open questions", ""]
    if matched:
        for qr in matched:
            out.append(f"- {qr['question']} `q#{qr['id']}`")
    else:
        out.append("_None._")
    out += [""]

    content = "\n".join(out).rstrip() + "\n"
    return content, _entity_input_hash(repo, ent["id"]), updated


def _render_source(repo: Repo, page) -> tuple[str, str, str]:
    sid = int(page["synthesis_input_hash"].split(":")[1]) if page["synthesis_input_hash"] and page["synthesis_input_hash"].startswith("src:") else None
    # resolve source id from marker
    src = repo.one("SELECT * FROM sources WHERE id = ?", (sid,))
    path = page["path"]
    summ = repo.one("SELECT * FROM summaries WHERE source_id = ?", (sid,))
    claims = repo.q("SELECT * FROM claims WHERE source_id = ? ORDER BY id", (sid,))
    promoted = [c for c in claims if c["status"] == "promoted"]
    pending = [c for c in claims if c["status"] == "pending"]
    updated = src["ingested_at"] or ""

    fm = _yaml({
        "title": json.dumps(src["title"] or f"source-{sid}"),
        "origin": src["origin"],
        "status": src["status"],
        "url": json.dumps(src["url"]) if src["url"] else '""',
        "tags": ["source"],
        "updated": updated or '""',
    })
    out = [fm, "", f"# {src['title'] or f'Source #{sid}'}", ""]
    out += ["| field | value |", "|---|---|",
            f"| origin | {src['origin']} |",
            f"| status | {src['status']} |",
            f"| url | {src['url'] or '—'} |",
            f"| hash | `{src['hash'][:12]}` |",
            f"| raw | [{src['path']}]({_rel_link(path, src['path'])}) |", ""]
    out += ["## Summary", ""]
    if summ:
        out += [summ["text"].rstrip(), ""]
    else:
        out += ["_No summary._", ""]
    out += ["## Claims", ""]
    if promoted:
        for c in promoted:
            out.append(f"- {c['text']} `#{c['id']}`")
    else:
        out.append("_No promoted claims._")
    out += [""]
    if pending:
        out += ["## Pending (not yet promoted)", ""]
        for c in pending:
            out.append(f"- {c['text']} _(conf {c['confidence']:.2f})_ `#{c['id']}`")
        out += [""]
    content = "\n".join(out).rstrip() + "\n"
    return content, page["synthesis_input_hash"], updated


def _render_synthesis(repo: Repo, page) -> tuple[str, str, str]:
    path = page["path"]
    name = Path(path).stem
    fm = _yaml({"title": json.dumps(name), "tags": ["synthesis"], "updated": '""'})
    out = [fm, "", f"# {name}", "", SYNTH_START]
    if page["synthesis"].strip():
        out.append(page["synthesis"].rstrip())
    out += [SYNTH_END, ""]
    return "\n".join(out).rstrip() + "\n", page["synthesis_input_hash"] or "", ""


def _render_digest(repo: Repo, page) -> tuple[str, str, str]:
    """A 'what the brain learned on <day>' page. Deterministic: the body derives
    only from DB rows whose timestamps fall on `day` (an input), never wall-clock,
    so re-rendering an unchanged DB yields identical bytes. The day is stored in
    synthesis_input_hash as 'digest:<YYYY-MM-DD>'."""
    marker = page["synthesis_input_hash"] or ""
    day = marker.split(":", 1)[1] if marker.startswith("digest:") else Path(page["path"]).stem
    claims = repo.q(
        """SELECT c.id, c.text, s.title AS s_title, s.id AS s_id
           FROM claims c JOIN sources s ON s.id = c.source_id
           WHERE c.status = 'promoted'
             AND substr(COALESCE(c.reviewed_at, c.created_at), 1, 10) = ?
           ORDER BY c.id""", (day,))
    sources = repo.q(
        """SELECT id, title, origin FROM sources
           WHERE substr(ingested_at, 1, 10) = ? ORDER BY id""", (day,))
    fm = _yaml({"title": json.dumps(f"digest {day}"), "tags": ["digest"],
                "day": day, "updated": '""'})
    out = [fm, "", f"# Digest — {day}", "", "## Promoted claims", ""]
    if claims:
        for c in claims:
            src = c["s_title"] or f"source #{c['s_id']}"
            out.append(f"- {c['text']} `#{c['id']}` ({src})")
    else:
        out.append("_None promoted this day._")
    out += ["", "## New sources", ""]
    if sources:
        for s in sources:
            name = s["title"] or f"source #{s['id']}"
            out.append(f"- {name} ({s['origin']}) `#{s['id']}`")
    else:
        out.append("_None ingested this day._")
    out += [""]
    return "\n".join(out).rstrip() + "\n", marker, ""


def ensure_digest(repo: Repo, day: str | None = None) -> str:
    """Create (or re-dirty) the digest page for `day` (default: today, in the
    same UTC basis as stored timestamps). Returns the page path. Call render()
    afterwards to write it."""
    day = day or util.now_iso()[:10]
    path = f"wiki/digests/{day}.md"
    marker = f"digest:{day}"
    row = repo.one(
        "SELECT id FROM pages WHERE kind='digest' AND synthesis_input_hash = ?", (marker,))
    if row:
        repo.ex("UPDATE pages SET dirty = 1 WHERE id = ?", (row["id"],))
    else:
        repo.ex(
            "INSERT INTO pages(path, kind, entity_id, dirty, synthesis_input_hash) "
            "VALUES (?, 'digest', NULL, 1, ?)", (path, marker))
    repo.conn.commit()
    return path


# --- reconciliation ---------------------------------------------------------
def _qualifying_entities(repo: Repo) -> list[int]:
    return [r["id"] for r in repo.q(
        """SELECT DISTINCT e.id FROM entities e
           JOIN claim_entities ce ON ce.entity_id = e.id
           JOIN claims c ON c.id = ce.claim_id
           WHERE c.status = 'promoted' ORDER BY e.id""")]


def _content_sources(repo: Repo) -> list[int]:
    rows = repo.q(
        """SELECT DISTINCT s.id FROM sources s
           WHERE EXISTS (SELECT 1 FROM claims c WHERE c.source_id = s.id)
              OR EXISTS (SELECT 1 FROM summaries su WHERE su.source_id = s.id)
           ORDER BY s.id""")
    return [r["id"] for r in rows]


def _reconcile(repo: Repo) -> bool:
    """Ensure page rows exist for the desired set; remove stale ones. Returns
    True if any page row was created or deleted."""
    changed = False
    # entity/concept pages
    want_ent = set(_qualifying_entities(repo))
    have_ent = {r["entity_id"]: r for r in repo.q(
        "SELECT * FROM pages WHERE kind IN ('entity','concept')")}
    for eid in want_ent:
        if eid not in have_ent:
            ent = repo.one("SELECT * FROM entities WHERE id = ?", (eid,))
            path = _entity_page_path(repo, ent)
            kind = page_kind_for(ent["kind"])
            repo.ex("INSERT INTO pages(path, kind, entity_id, dirty) VALUES (?,?,?,1)",
                    (path, kind, eid))
            changed = True
    for eid, row in have_ent.items():
        if eid not in want_ent:
            _delete_page(repo, row)
            changed = True
    # source pages (marker stored in synthesis_input_hash as 'src:<id>')
    want_src = set(_content_sources(repo))
    have_src = {}
    for r in repo.q("SELECT * FROM pages WHERE kind='source'"):
        marker = r["synthesis_input_hash"] or ""
        if marker.startswith("src:"):
            have_src[int(marker.split(":")[1])] = r
    for sid in want_src:
        if sid not in have_src:
            path = _source_page_path(repo, sid)
            repo.ex(
                "INSERT INTO pages(path, kind, entity_id, dirty, synthesis_input_hash) "
                "VALUES (?, 'source', NULL, 1, ?)", (path, f"src:{sid}"))
            changed = True
    for sid, row in have_src.items():
        if sid not in want_src:
            _delete_page(repo, row)
            changed = True
    # index page
    if not repo.one("SELECT 1 FROM pages WHERE kind='index'"):
        repo.ex("INSERT INTO pages(path, kind, dirty) VALUES ('wiki/index.md','index',1)")
        changed = True
    return changed


def _delete_page(repo: Repo, row):
    fp = repo.root / row["path"]
    if fp.exists():
        fp.unlink()
    repo.ex("DELETE FROM pages WHERE id = ?", (row["id"],))


# --- index ------------------------------------------------------------------
def _render_index(repo: Repo) -> str:
    out = ["---", "title: index", "tags: [index]", "---", "", "# Wiki Index", ""]
    sections = [
        ("Entities", "entity"),
        ("Concepts", "concept"),
        ("Sources", "source"),
        ("Syntheses", "synthesis"),
        ("Digests", "digest"),
    ]
    for label, kind in sections:
        rows = repo.q("SELECT * FROM pages WHERE kind = ? ORDER BY path", (kind,))
        out.append(f"## {label} ({len(rows)})")
        out.append("")
        if not rows:
            out.append("_None._")
            out.append("")
            continue
        for r in rows:
            link = _rel_link("wiki/index.md", r["path"])
            desc = _index_desc(repo, r)
            name = Path(r["path"]).stem
            out.append(f"- [{name}]({link}) — {desc}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _index_desc(repo: Repo, page) -> str:
    if page["entity_id"]:
        ent = repo.one("SELECT * FROM entities WHERE id = ?", (page["entity_id"],))
        n = repo.one(
            """SELECT COUNT(*) AS n FROM claims c
               JOIN claim_entities ce ON ce.claim_id=c.id
               WHERE ce.entity_id=? AND c.status='promoted'""", (page["entity_id"],))
        return f"{ent['kind']}, {n['n']} promoted claim(s)"
    if page["kind"] == "source":
        marker = page["synthesis_input_hash"] or ""
        if marker.startswith("src:"):
            src = repo.one("SELECT * FROM sources WHERE id=?", (int(marker.split(':')[1]),))
            if src:
                return f"{src['origin']}, status {src['status']}"
    if page["kind"] == "digest":
        return "daily digest"
    return page["kind"]


# --- top-level render -------------------------------------------------------
def render(repo: Repo, all_pages: bool = False) -> dict:
    created_or_deleted = _reconcile(repo)
    rows = repo.q("SELECT * FROM pages ORDER BY kind, path")
    report = {"rendered": [], "needs_synthesis_review": [], "fresh": [], "changed": created_or_deleted}

    for page in rows:
        if not all_pages and not page["dirty"] and not created_or_deleted:
            # still must render dependent pages flagged dirty; skip clean ones
            if not page["dirty"]:
                continue
        if page["kind"] in ("entity", "concept"):
            content, cur_hash, _ = _render_entity(repo, page)
            stored = page["synthesis_input_hash"]
            if stored != cur_hash:
                report["needs_synthesis_review"].append(page["path"])
            else:
                report["fresh"].append(page["path"])
        elif page["kind"] == "source":
            content, _, _ = _render_source(repo, page)
        elif page["kind"] == "synthesis":
            content, cur_hash, _ = _render_synthesis(repo, page)
        elif page["kind"] == "digest":
            content, _, _ = _render_digest(repo, page)
        elif page["kind"] == "index":
            content = _render_index(repo)
        else:
            continue

        fp = repo.root / page["path"]
        fp.parent.mkdir(parents=True, exist_ok=True)
        old = fp.read_text(encoding="utf-8") if fp.exists() else None
        if old != content:
            fp.write_text(content, encoding="utf-8")
            report["changed"] = True
        report["rendered"].append(page["path"])
        if page["dirty"]:
            repo.ex("UPDATE pages SET dirty = 0 WHERE id = ?", (page["id"],))
            report["changed"] = True

    # index always reflects current set; render it when anything changed
    if report["changed"] or all_pages:
        idx = repo.one("SELECT * FROM pages WHERE kind='index'")
        if idx:
            content = _render_index(repo)
            fp = repo.root / idx["path"]
            old = fp.read_text(encoding="utf-8") if fp.exists() else None
            if old != content:
                fp.write_text(content, encoding="utf-8")
                report["changed"] = True

    if report["changed"]:
        repo.finalize(
            "render",
            f"{len(report['rendered'])} pages; "
            f"{len(report['needs_synthesis_review'])} need synthesis review")
    else:
        repo.conn.commit()
    return report


# --- synthesis get/set ------------------------------------------------------
def synthesis_get(repo: Repo, page_path: str) -> str:
    page_path = _normalize_page_path(repo, page_path)
    row = repo.one("SELECT synthesis FROM pages WHERE path = ?", (page_path,))
    if not row:
        raise SystemExit(f"error: no page {page_path}")
    return row["synthesis"]


def synthesis_set(repo: Repo, page_path: str, text: str) -> None:
    page_path = _normalize_page_path(repo, page_path)
    row = repo.one("SELECT * FROM pages WHERE path = ?", (page_path,))
    if not row:
        # allow creating standalone synthesis pages under wiki/syntheses/
        if page_path.startswith("wiki/syntheses/"):
            repo.ex(
                "INSERT INTO pages(path, kind, dirty, synthesis) VALUES (?, 'synthesis', 1, ?)",
                (page_path, text))
            repo.finalize("synthesis-set", f"created {page_path}")
            return
        raise SystemExit(f"error: no page {page_path}")
    # compute the input hash now so the page is considered 'reviewed/fresh'
    if row["kind"] in ("entity", "concept"):
        new_hash = _entity_input_hash(repo, row["entity_id"])
    else:
        new_hash = row["synthesis_input_hash"]
    repo.ex("UPDATE pages SET synthesis = ?, synthesis_input_hash = ?, dirty = 1 WHERE id = ?",
            (text, new_hash, row["id"]))
    repo.finalize("synthesis-set", f"{page_path} ({len(text)} chars)")


def _normalize_page_path(repo: Repo, page_path: str) -> str:
    p = page_path.replace("\\", "/")
    if not p.startswith("wiki/"):
        p = "wiki/" + p.lstrip("/")
    if not p.endswith(".md"):
        p += ".md"
    return p
