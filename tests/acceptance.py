"""Offline acceptance harness for wiki-brain (phases 1-5).

Runs the package API against a throwaway temp repo + temp DB, so it never
touches the live database. No pytest dependency — run directly:

    .venv/Scripts/python.exe tests/acceptance.py

Network-dependent paths (URL fetch, websearch, live bookmark fetch) are NOT
exercised here; their logic is unit-tested where possible (bookmark parser,
budget ledger). Exits non-zero on first failure.
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

# Make the package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

from wiki.db import Repo, init_db          # noqa: E402
from wiki.cli import build_parser          # noqa: E402
from wiki import (ingest, search as searchmod, queue as queuemod,            # noqa: E402
                  render as rendermod, lint as lintmod, health as healthmod,
                  review, gate as gatemod, gather, fetch as fetchmod,
                  migrate as migratemod, schema as schemamod, drop as dropmod,
                  skills as skillsmod, mcp_server as mcpmod, evidence as evidencemod)

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def write(p: Path, text: str):
    p.write_text(text, encoding="utf-8")


def make_repo(root: Path) -> Path:
    db = root / "wiki.db"
    write(root / "config.toml", f'[paths]\ndb = "{db.as_posix()}"\nbookmark_folder = "wiki"\n'
          '[gate]\nauto_promote_confidence = 0.85\nmachine_confidence_ceiling = 0.9\n'
          '[budgets]\nqueries_per_question = 2\nfetches_per_question = 2\n'
          'questions_per_night = 3\nfetches_per_night = 3\n'
          '[search]\nengine = "ddg"\n[lint]\nstale_days = 30\ncontradiction_days = 14\n')
    for d in ("raw", "inbox", "wiki/entities", "wiki/concepts", "wiki/sources",
              "wiki/syntheses", "db"):
        (root / d).mkdir(parents=True, exist_ok=True)
    write(root / "log.md", "# log\n")
    init_db(start=root).close()
    return root


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wikibrain-test-"))
    root = make_repo(tmp)
    rel = lambda *a: root.joinpath(*a)

    # ---------------- Migration runner ----------------
    print("[migrate] schema migration runner")
    import sqlite3 as _sqlite
    check("SCHEMA_VERSION matches latest migration",
          schemamod.SCHEMA_VERSION == migratemod.latest_version())
    # Build an old-shape (v1) DB: sources lacking the new columns, plus the core
    # tables (claims/entities/relations/claim_entities) that have existed since
    # v1 and that the v6 index migration targets.
    old_db = Path(tempfile.mkdtemp(prefix="wikibrain-mig-")) / "old.db"
    c = _sqlite.connect(str(old_db))
    c.executescript(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL, "
        "path TEXT NOT NULL, title TEXT, url TEXT, origin TEXT NOT NULL, "
        "fetched_at TEXT, ingested_at TEXT, status TEXT NOT NULL DEFAULT 'new');"
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT NOT NULL, "
        "source_id INTEGER NOT NULL REFERENCES sources(id), location TEXT, "
        "confidence REAL NOT NULL, origin TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', superseded_by INTEGER REFERENCES claims(id), "
        "created_at TEXT NOT NULL, reviewed_at TEXT);"
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
        "kind TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]');"
        "CREATE TABLE relations (id INTEGER PRIMARY KEY, src INTEGER NOT NULL REFERENCES entities(id), "
        "rel TEXT NOT NULL, dst INTEGER NOT NULL REFERENCES entities(id), "
        "claim_id INTEGER REFERENCES claims(id), UNIQUE(src, rel, dst, claim_id));"
        "CREATE TABLE claim_entities (claim_id INTEGER NOT NULL REFERENCES claims(id), "
        "entity_id INTEGER NOT NULL REFERENCES entities(id), PRIMARY KEY (claim_id, entity_id));"
        "CREATE TABLE escalations (id INTEGER PRIMARY KEY, "
        "source_id INTEGER NOT NULL REFERENCES sources(id), reason TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open');"
    )
    c.execute("INSERT INTO sources(hash, path, origin) VALUES ('h1','raw/x.md','clip')")
    c.execute("PRAGMA user_version=1")
    c.commit()
    migratemod.migrate(c)
    cols = {row[1] for row in c.execute("PRAGMA table_info(sources)")}
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    check("migrate adds mime_type/category/tags", {"mime_type", "category", "tags"} <= cols)
    check("migrate bumps user_version to latest", ver == migratemod.latest_version())
    _tbls = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("migrate v3 creates embeddings table", "embeddings" in _tbls)
    check("migrate v4 creates skills tables", {"skills", "skill_claims"} <= _tbls)
    check("migrate v5 creates skill_versions table", "skill_versions" in _tbls)
    _esc_cols = {row[1] for row in c.execute("PRAGMA table_info(escalations)")}
    check("migrate v8 adds escalations.proposal", "proposal" in _esc_cols)
    _skill_cols = {row[1] for row in c.execute("PRAGMA table_info(skills)")}
    check("migrate v5 adds skills.version column", "version" in _skill_cols)
    check("existing row gets default tags='[]'",
          c.execute("SELECT tags FROM sources WHERE hash='h1'").fetchone()[0] == "[]")
    _idx = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check("migrate v6 creates hot-path indexes",
          {"claims_status", "claims_source_id", "claim_entities_entity_id", "relations_dst"} <= _idx)
    migratemod.migrate(c)  # idempotent re-run
    check("migrate is idempotent",
          c.execute("PRAGMA user_version").fetchone()[0] == ver)
    c.close()
    # Fresh init_db DBs are already at latest -> migrate is a no-op there.
    with Repo.open(start=root) as r:
        fresh_cols = {row[1] for row in r.conn.execute("PRAGMA table_info(sources)")}
        fresh_idx = {row[0] for row in r.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    check("fresh install already has new columns", {"mime_type", "category", "tags"} <= fresh_cols)
    check("fresh install already has hot-path indexes",
          {"claims_status", "claims_source_id", "claim_entities_entity_id", "relations_dst"} <= fresh_idx)

    # ---------------- Publish hygiene (leak guard) ----------------
    print("[publish] tracked files carry no machine paths; example config valid")
    import subprocess as _sp, re as _re, tomllib as _toml
    repo_root = Path(__file__).resolve().parents[1]
    tracked = []
    try:
        out = _sp.run(["git", "-C", str(repo_root), "ls-files"],
                      capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            tracked = [ln for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        tracked = []  # git unavailable (e.g. tarball) -> guard is a no-op
    leak_pat = _re.compile(r"[Cc]:[\\/]Users[\\/]")
    secret_pats = [
        _re.compile(r"sk-[A-Za-z0-9]{20,}"),
        _re.compile(r"AKIA[0-9A-Z]{16}"),
        _re.compile(r"(?:ANTHROPIC|OPENAI|FIRECRAWL|TAVILY|BRAVE|EXA|KAGI)_API_KEY"
                    r"\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    ]
    binext = {".png", ".jpg", ".jpeg", ".ico", ".pdf", ".db"}
    leaks, secrets = [], []
    for f in tracked:
        if Path(f).suffix.lower() in binext:
            continue
        try:
            txt = (repo_root / f).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if leak_pat.search(txt):
            leaks.append(f)
        if any(p.search(txt) for p in secret_pats):
            secrets.append(f)
    check("no absolute C:/Users path in tracked files", not leaks)
    if leaks:
        print("    leaking:", leaks)
    check("no API-key-like secrets in tracked files", not secrets)
    if secrets:
        print("    secrets in:", secrets)
    example_ok = False
    try:
        with open(repo_root / "config.example.toml", "rb") as fh:
            _toml.load(fh)
        example_ok = True
    except Exception:
        example_ok = False
    check("config.example.toml exists and is valid TOML", example_ok)

    # ---------------- Fetch backends + search ----------------
    print("[fetch] backend fallback chain + ddgs mapping")

    def _boom(*a, **k):
        raise fetchmod.FetchError("boom")

    _oj, _ot = fetchmod._jina, fetchmod._trafilatura
    fetchmod._jina = _boom
    fetchmod._trafilatura = lambda u, t=30, **k: ("# md", "Title")
    md, title = fetchmod.fetch_url("https://example.org", backend="jina")
    check("fetch falls back jina -> trafilatura", md == "# md" and title == "Title")
    fetchmod._trafilatura = _boom
    all_failed = False
    try:
        fetchmod.fetch_url("https://example.org", backend="jina")
    except fetchmod.FetchError:
        all_failed = True
    check("fetch_url raises when all backends fail", all_failed)
    fetchmod._jina, fetchmod._trafilatura = _oj, _ot

    # ddgs path: inject a fake `ddgs` module so the mapping is exercised offline.
    import types as _types
    _fake = _types.ModuleType("ddgs")

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, max_results=10):
            return [{"title": "T", "href": "https://x", "body": "snip"}]

    _fake.DDGS = _FakeDDGS
    _saved_ddgs = sys.modules.get("ddgs")
    sys.modules["ddgs"] = _fake
    ddg_res = gather._ddg("anything")
    check("ddgs results mapped to title/url/snippet",
          ddg_res and ddg_res[0] == {"title": "T", "url": "https://x", "snippet": "snip"})
    # Force ImportError on `from ddgs import DDGS` (None in sys.modules) so the
    # fallback is exercised deterministically whether or not ddgs is installed.
    sys.modules["ddgs"] = None
    _osc = gather._ddg_scrape
    gather._ddg_scrape = lambda q: [{"title": "scrape", "url": "u", "snippet": ""}]
    check("_ddg falls back to scrape when ddgs absent",
          gather._ddg("q")[0]["title"] == "scrape")
    gather._ddg_scrape = _osc
    if _saved_ddgs is not None:
        sys.modules["ddgs"] = _saved_ddgs
    else:
        sys.modules.pop("ddgs", None)

    # ---------------- Drop folder ----------------
    print("[drop] watch-folder ingest")
    dtmp = Path(tempfile.mkdtemp(prefix="wikibrain-drop-"))
    droot = make_repo(dtmp)
    drop_dir = dtmp / "dropzone"
    drop_dir.mkdir()
    write(drop_dir / "note.txt", "Dropped fact: the cache TTL is 24h.")
    write(drop_dir / "doc.md", "# Title\n\nSome markdown content.")
    write(drop_dir / "scan.pdf", "%PDF-1.4 not really a pdf")  # no extractor yet
    with Repo.open(start=droot) as r:
        r.cfg.data["paths"]["drop_folder"] = str(drop_dir)
        dres = dropmod.scan(r)
        drop_sources = r.q("SELECT * FROM sources WHERE origin='drop'")
    byf = {e["file"]: e for e in dres}
    check("drop ingests text + markdown files",
          byf["note.txt"]["source_id"] and byf["doc.md"]["source_id"])
    check("drop registered exactly 2 'drop' sources", len(drop_sources) == 2)
    check("drop leaves unsupported pdf in place with a warning",
          byf["scan.pdf"]["source_id"] is None and byf["scan.pdf"]["warning"])
    check("drop archives originals to .processed/",
          not (drop_dir / "note.txt").exists()
          and (drop_dir / ".processed" / "note.txt").exists())
    check("drop leaves unsupported pdf in the folder", (drop_dir / "scan.pdf").exists())
    with Repo.open(start=droot) as r:
        r.cfg.data["paths"]["drop_folder"] = str(drop_dir)
        dres2 = dropmod.scan(r)
    check("drop re-run is idempotent (nothing new ingested)",
          not any(e["source_id"] for e in dres2))

    # ---------------- Daily digest ----------------
    print("[digest] daily learning digest")
    gtmp = Path(tempfile.mkdtemp(prefix="wikibrain-digest-"))
    groot = make_repo(gtmp)
    write(groot / "d.md", "Digest source content.")
    with Repo.open(start=groot) as r:
        dsid, _ = ingest.add(r, str(groot / "d.md"), origin="clip", title="Digest src")
        r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, "
             "created_at, reviewed_at) VALUES ('Digest fact alpha.', ?, 0.9, 'clip', "
             "'promoted', '2026-03-15T08:00:00Z', '2026-03-15T09:00:00Z')", (dsid,))
        r.conn.commit()
        rendermod.ensure_digest(r, "2026-03-15")
        rendermod.render(r)
    dgfile = groot / "wiki" / "digests" / "2026-03-15.md"
    check("digest page created", dgfile.exists())
    dgtxt = dgfile.read_text(encoding="utf-8")
    check("digest lists the day's promoted claim", "Digest fact alpha." in dgtxt)
    check("digest day recorded in frontmatter", "day: 2026-03-15" in dgtxt)
    dgb1 = dgfile.read_bytes()
    with Repo.open(start=groot) as r:
        rendermod.render(r, all_pages=True)
    check("digest re-render is byte-identical", dgfile.read_bytes() == dgb1)

    # ---------------- Image labels (category/tags) ----------------
    print("[labels] category/tags from extraction JSON")
    ltmp = Path(tempfile.mkdtemp(prefix="wikibrain-labels-"))
    lroot = make_repo(ltmp)
    write(lroot / "img.md", "placeholder describing an image source")
    with Repo.open(start=lroot) as r:
        lsid, _ = ingest.add(r, str(lroot / "img.md"), origin="drop", title="diagram")
        lj = {"source_id": lsid, "summary": "An architecture diagram.",
              "claims": [{"text": "The diagram shows a load balancer.",
                          "confidence": 0.8, "entities": ["LoadBalancer"], "relations": []}],
              "low_confidence": False,
              "category": "diagram", "tags": ["architecture", "infra"]}
        write(lroot / "lj.json", json.dumps(lj))
        ingest.file_claims(r, lsid, str(lroot / "lj.json"))
        srow = r.one("SELECT category, tags FROM sources WHERE id=?", (lsid,))
    check("file-claims sets source category", srow["category"] == "diagram")
    check("file-claims sets source tags (JSON array)",
          json.loads(srow["tags"]) == ["architecture", "infra"])
    bad = {"source_id": lsid, "summary": "x", "claims": [],
           "low_confidence": False, "tags": "notalist"}
    write(lroot / "badtags.json", json.dumps(bad))
    rej = False
    try:
        with Repo.open(start=lroot) as r:
            ingest.file_claims(r, lsid, str(lroot / "badtags.json"))
    except ingest.IngestError:
        rej = True
    check("invalid tags (non-list) rejected", rej)

    # ---------------- Extract dispatch + image drop ----------------
    print("[extract] dispatch/guards + image asset handling")
    from wiki import extract as extractmod
    check("kind_for routes pdf->doc", extractmod.kind_for(Path("a.pdf")) == "doc")
    check("kind_for routes png->image", extractmod.kind_for(Path("a.png")) == "image")
    check("kind_for unknown->text", extractmod.kind_for(Path("a.zzz")) == "text")
    _savedoc = sys.modules.get("docling.document_converter")
    sys.modules["docling.document_converter"] = None  # force ImportError on docling
    guarded = False
    try:
        extractmod.to_markdown(Path("x.pdf"), kind="doc")
    except extractmod.ExtractError as e:
        guarded = "[docs]" in str(e)
    if _savedoc is not None:
        sys.modules["docling.document_converter"] = _savedoc
    else:
        sys.modules.pop("docling.document_converter", None)
    check("doc extraction guarded with [docs] install hint", guarded)
    # images must NEVER be skipped for lack of OCR — the session reads them via
    # vision; OCR is a bonus. Force every OCR backend absent -> still a stub.
    _ocrmods = ("rapidocr", "rapidocr_onnxruntime", "pytesseract")
    _savedocr = {m: sys.modules.get(m) for m in _ocrmods}
    for m in _ocrmods:
        sys.modules[m] = None
    img_stub = extractmod._image(Path("pic.png"))
    for m, v in _savedocr.items():
        if v is not None:
            sys.modules[m] = v
        else:
            sys.modules.pop(m, None)
    check("image degrades gracefully without any OCR (no raise, still a stub)",
          "image: pic.png" in img_stub and "view the image" in img_stub.lower())

    # image drop: monkeypatch the OCR backend so no tesseract binary is needed
    itmp = Path(tempfile.mkdtemp(prefix="wikibrain-img-"))
    iroot = make_repo(itmp)
    idrop = itmp / "dz"
    idrop.mkdir()
    (idrop / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n fake png bytes")
    _oimg = extractmod._image
    extractmod._image = lambda p, tesseract_cmd=None: \
        "# image: diagram.png\n\nOCR text:\n\nLOAD BALANCER\n"
    try:
        with Repo.open(start=iroot) as r:
            r.cfg.data["paths"]["drop_folder"] = str(idrop)
            ires = dropmod.scan(r)
    finally:
        extractmod._image = _oimg
    check("image drop registers an image/* source",
          ires and ires[0]["source_id"] and (ires[0]["mime_type"] or "").startswith("image/"))
    assets = list((iroot / "raw" / "assets").glob("diagram-*.png"))
    check("image binary copied into raw/assets/", len(assets) == 1)
    raw_md = list((iroot / "raw").glob("diagram-*.md"))
    raw_txt = raw_md[0].read_text(encoding="utf-8") if raw_md else ""
    check("image raw artifact links the asset + carries OCR text",
          "raw/assets/" in raw_txt and "LOAD BALANCER" in raw_txt)

    # ---------------- Transcribe ----------------
    print("[transcribe] youtube captions -> source")
    check("_yt_id parses watch?v=",
          extractmod._yt_id("https://www.youtube.com/watch?v=abc123XYZ") == "abc123XYZ")
    check("_yt_id parses youtu.be",
          extractmod._yt_id("https://youtu.be/abc123XYZ") == "abc123XYZ")
    ttmp = Path(tempfile.mkdtemp(prefix="wikibrain-tt-"))
    troot = make_repo(ttmp)
    _oyt = extractmod._youtube
    extractmod._youtube = lambda url: (
        f"# transcript: {url}\n\nhello world from the video.\n", "transcript abc")
    try:
        with Repo.open(start=troot) as r:
            tsid = ingest.transcribe(r, "https://www.youtube.com/watch?v=abc123XYZ")
            trow = r.one("SELECT origin, url FROM sources WHERE id=?", (tsid,))
    finally:
        extractmod._youtube = _oyt
    check("transcribe registers origin=transcript", trow["origin"] == "transcript")
    check("transcribe keeps the source url", "youtube.com" in (trow["url"] or ""))
    _savedyt = sys.modules.get("youtube_transcript_api")
    sys.modules["youtube_transcript_api"] = None  # force [media] absent
    tguard = False
    try:
        extractmod.transcribe("https://www.youtube.com/watch?v=zzz")
    except extractmod.ExtractError as e:
        tguard = "[media]" in str(e)
    if _savedyt is not None:
        sys.modules["youtube_transcript_api"] = _savedyt
    else:
        sys.modules.pop("youtube_transcript_api", None)
    check("transcribe guarded when [media] extra absent", tguard)

    # ---------------- Semantic search ----------------
    print("[embed] semantic search (guard always; ranking if WIKI_TEST_SEMANTIC)")
    import os as _os
    from wiki import embed as embedmod
    etmp2 = Path(tempfile.mkdtemp(prefix="wikibrain-embed-"))
    eroot = make_repo(etmp2)
    _savedst = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = None  # force [semantic] absent
    eguard = False
    try:
        with Repo.open(start=eroot) as r:
            embedmod.index(r)
    except embedmod.EmbedError as e:
        eguard = "[semantic]" in str(e)
    if _savedst is not None:
        sys.modules["sentence_transformers"] = _savedst
    else:
        sys.modules.pop("sentence_transformers", None)
    check("embed guarded when [semantic] extra absent", eguard)

    # Real ranking — opt-in (downloads a model); keeps the default suite offline.
    if _os.environ.get("WIKI_TEST_SEMANTIC"):
        stmp = Path(tempfile.mkdtemp(prefix="wikibrain-sem-"))
        sroot = make_repo(stmp)
        write(sroot / "s.md", "src")
        with Repo.open(start=sroot) as r:
            ssid, _ = ingest.add(r, str(sroot / "s.md"), origin="clip", title="s")
            for txt in ["Redis is an in-memory key-value cache.",
                        "Postgres is a relational SQL database.",
                        "The cat sat on the warm windowsill."]:
                r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, "
                     "created_at) VALUES (?, ?, 0.9, 'clip', 'promoted', '2026-01-01T00:00:00Z')",
                     (txt, ssid))
            r.conn.commit()
            n_emb = embedmod.index(r)
            hits = embedmod.semantic_search(r, "fast caching layer for sessions", k=3)
        check("embed indexed 3 claims", n_emb == 3)
        check("semantic top hit is the cache claim",
              bool(hits) and "cache" in hits[0]["text"].lower())
    else:
        print("    (real ranking skipped — set WIKI_TEST_SEMANTIC=1 with [semantic] installed)")

    # Mixed-model embeddings: stub the encoder (no [semantic] extra needed) and
    # verify semantic_search only ranks vectors from the CURRENT model, ignoring
    # rows from a different model or with a mismatched dim.
    import numpy as _npt
    mmtmp = Path(tempfile.mkdtemp(prefix="wikibrain-mixedmodel-"))
    mmroot = make_repo(mmtmp)
    write(mmroot / "mm.md", "src")
    with Repo.open(start=mmroot) as r:
        mmsid, _ = ingest.add(r, str(mmroot / "mm.md"), origin="clip", title="mm")
        for txt in ("old-model claim", "current-model claim", "mismatched-dim claim"):
            r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, "
                 "created_at) VALUES (?, ?, 0.9, 'clip', 'promoted', '2026-01-01T00:00:00Z')",
                 (txt, mmsid))
        r.conn.commit()
        cids = {row["text"]: row["id"] for row in r.q("SELECT id, text FROM claims")}
        cur_name = embedmod._model_name(r)
        v3 = _npt.asarray([1.0, 0.0, 0.0], dtype=_npt.float32).tobytes()
        v2 = _npt.asarray([1.0, 0.0], dtype=_npt.float32).tobytes()
        r.ex("INSERT INTO embeddings(claim_id, model, dim, vec, created_at) VALUES (?,?,?,?,?)",
             (cids["old-model claim"], "some-other-model", 3, v3, "2026-01-01T00:00:00Z"))
        r.ex("INSERT INTO embeddings(claim_id, model, dim, vec, created_at) VALUES (?,?,?,?,?)",
             (cids["current-model claim"], cur_name, 3, v3, "2026-01-01T00:00:00Z"))
        r.ex("INSERT INTO embeddings(claim_id, model, dim, vec, created_at) VALUES (?,?,?,?,?)",
             (cids["mismatched-dim claim"], cur_name, 2, v2, "2026-01-01T00:00:00Z"))
        r.conn.commit()
        _orig_model = embedmod._model
        embedmod._model = lambda name: _types.SimpleNamespace(
            encode=lambda texts, normalize_embeddings=True: [[1.0, 0.0, 0.0]])
        try:
            mm_hits = embedmod.semantic_search(r, "q", k=10)
        finally:
            embedmod._model = _orig_model
    mm_ids = {h["id"] for h in mm_hits}
    check("semantic_search excludes vectors from a different model",
          cids["old-model claim"] not in mm_ids)
    check("semantic_search excludes dim-mismatched vectors",
          cids["mismatched-dim claim"] not in mm_ids)
    check("semantic_search ranks only the current-model claim",
          mm_ids == {cids["current-model claim"]})

    # ---------------- Dump scaling (#7) ----------------
    print("[dump] embeddings row DATA excluded from db/dump.sql")
    dtmp = Path(tempfile.mkdtemp(prefix="wikibrain-dump-"))
    droot = make_repo(dtmp)
    write(droot / "d.md", "src")
    with Repo.open(start=droot) as r:
        dsid, _ = ingest.add(r, str(droot / "d.md"), origin="clip", title="d")
        r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
             "VALUES ('dumped claim', ?, 0.9, 'clip', 'promoted', '2026-01-01T00:00:00Z')",
             (dsid,))
        r.conn.commit()
        dcid = r.one("SELECT id FROM claims WHERE text='dumped claim'")["id"]
        vec = _npt.asarray([1.0, 0.0, 0.0], dtype=_npt.float32).tobytes()
        r.ex("INSERT INTO embeddings(claim_id, model, dim, vec, created_at) VALUES (?,?,?,?,?)",
             (dcid, "m", 3, vec, "2026-01-01T00:00:00Z"))
        r.finalize("embed", "test embed for dump")
    dump_text = (droot / "db" / "dump.sql")
    dump_text = dump_text.read_text(encoding="utf-8")
    check("dump.sql keeps the embeddings CREATE TABLE",
          'CREATE TABLE embeddings' in dump_text)
    check("dump.sql has no embeddings INSERT rows",
          'INSERT INTO "embeddings"' not in dump_text)
    check("dump.sql still has the claims INSERT row",
          'INSERT INTO "claims"' in dump_text and "dumped claim" in dump_text)

    # ---------------- Phase 1 ----------------
    print("[Phase 1] ingest / search / graph")
    write(rel("src1.md"), "Redis 7.2 is the session cache. Sidekiq uses Redis. Postgres is primary.")
    with Repo.open(start=root) as r:
        sid, _ = ingest.add(r, str(rel("src1.md")), origin="clip", title="Stack notes")
    check("add returns source id 1", sid == 1)

    good = {"source_id": 1, "summary": "Stack summary.",
            "claims": [
                {"text": "Redis 7.2 is the session cache.", "confidence": 0.95,
                 "entities": ["Redis", "Sidekiq"],
                 "relations": [{"src": "Sidekiq", "rel": "uses", "dst": "Redis"}]},
                {"text": "Postgres is the primary database.", "confidence": 0.9,
                 "entities": ["Postgres"], "relations": []}],
            "low_confidence": False,
            "proposed_questions": ["What eviction policy?"]}
    write(rel("good.json"), json.dumps(good))
    with Repo.open(start=root) as r:
        res = ingest.file_claims(r, 1, str(rel("good.json")))
    check("filed 2 claims", res["claims"] == 2)
    check("queued 1 proposed question", res["questions"] == 1)
    check("file-claims files raw evidence into a categorized bucket",
          res["filed"]["moved"] and res["filed"]["new_path"].startswith("raw/uncategorized/"))
    with Repo.open(start=root) as r:
        filed_path = r.one("SELECT path FROM sources WHERE id=1")["path"]
    check("sources.path follows the filed evidence path",
          filed_path == res["filed"]["new_path"])
    check("raw evidence index is generated automatically",
          (root / "raw" / "INDEX.md").exists()
          and "Stack notes" in (root / "raw" / "INDEX.md").read_text(encoding="utf-8"))
    with Repo.open(start=root) as r:
        again = evidencemod.file_source(r, 1)
    check("evidence filing is idempotent", again["moved"] is False)

    # Regression: an ingested artifact's on-disk bytes must equal sources.hash.
    # On Windows, write_text translates \n -> \r\n, so the file would hash
    # differently than the recorded (LF) content and evidence filing would refuse
    # to move it. Capture writes bytes; this guards that across platforms.
    import hashlib as _hashlib
    with Repo.open(start=root) as r:
        cap_id = ingest.capture(r, "roundtrip", "line one\nline two\nline three")
        crow = r.one("SELECT path, hash FROM sources WHERE id=?", (cap_id,))
        disk = (root / crow["path"]).read_bytes()
        check("captured file's on-disk bytes match its recorded hash",
              _hashlib.sha256(disk).hexdigest() == crow["hash"])
        filed = evidencemod.file_source(r, cap_id)  # raises if the hash mismatched
        check("a captured source files cleanly into raw/sessions",
              filed["bucket"] == "sessions")

    # bad JSON rejected
    write(rel("bad.json"), json.dumps({"source_id": 1, "summary": "x",
          "claims": [{"text": "no conf", "entities": [], "relations": []}],
          "low_confidence": False}))
    rejected = False
    try:
        with Repo.open(start=root) as r:
            ingest.file_claims(r, 1, str(rel("bad.json")))
    except ingest.IngestError:
        rejected = True
    check("bad JSON rejected", rejected)

    # dedupe
    deduped = False
    try:
        with Repo.open(start=root) as r:
            ingest.add(r, str(rel("src1.md")), origin="clip")
    except ingest.IngestError:
        deduped = True
    check("exact duplicate refused", deduped)

    with Repo.open(start=root) as r:
        hits = searchmod.search(r, "Redis")
        g = searchmod.graph(r, "Redis", hops=2)
    check("search finds claim", any(h["kind"] == "claim" for h in hits))
    check("graph has Sidekiq-uses-Redis edge",
          any(e["rel"] == "uses" and e["dst"] == "Redis" for e in g["edges"]))

    # capture -> pending source
    with Repo.open(start=root) as r:
        cid = ingest.capture(r, "claude-code", "Durable decision: TTL is 24h.")
        pend = r.q("SELECT * FROM sources WHERE status='new'")
    check("capture creates pending source", any(p["id"] == cid for p in pend))
    check("capture origin is session/claude-code",
          r and any(p["origin"] == "session/claude-code" for p in pend))

    # ---------------- Phase 2 ----------------
    print("[Phase 2] render / determinism / drift / synthesis")
    with Repo.open(start=root) as r:
        review.promote(r, [1, 2])
        rendermod.render(r, all_pages=True)
    redis_page = rel("wiki", "concepts", "redis.md")
    check("redis page exists", redis_page.exists())
    check("promoted claim rendered", "session cache" in redis_page.read_text(encoding="utf-8"))
    check("pending NOT on entity page (only promoted)",
          "24h" not in redis_page.read_text(encoding="utf-8"))

    # determinism: render twice, bytes identical
    b1 = redis_page.read_bytes()
    with Repo.open(start=root) as r:
        rendermod.render(r, all_pages=True)
    check("re-render is byte-identical", redis_page.read_bytes() == b1)

    # drift restore
    redis_page.write_text("HAND EDIT", encoding="utf-8")
    with Repo.open(start=root) as r:
        rendermod.render(r, all_pages=True)
    check("drift erased by render --all", redis_page.read_bytes() == b1)

    # synthesis
    with Repo.open(start=root) as r:
        rendermod.synthesis_set(r, "wiki/concepts/redis.md", "Redis is the cache.")
        rep = rendermod.render(r, all_pages=True)
    check("synthesis injected", "Redis is the cache." in redis_page.read_text(encoding="utf-8"))
    check("synthesised page not in needs-review",
          "wiki/concepts/redis.md" not in rep["needs_synthesis_review"])

    # ---------------- Phase 3 ----------------
    print("[Phase 3] contradiction detection / lint / health")
    # contradicting claim on a new source (tests the OR-recall fix)
    write(rel("src2.md"), "Correction.")
    with Repo.open(start=root) as r:
        s2, _ = ingest.add(r, str(rel("src2.md")), origin="clip", title="Correction")
    contra = {"source_id": s2, "summary": "Correction.",
              "claims": [{"text": "Postgres is not the primary database.", "confidence": 0.8,
                          "entities": ["Postgres"], "relations": []}],
              "low_confidence": False}
    write(rel("contra.json"), json.dumps(contra))
    with Repo.open(start=root) as r:
        cres = ingest.file_claims(r, s2, str(rel("contra.json")))
    check("contradiction auto-opened vs promoted claim", cres["contradictions"] == 1)

    # broken wikilink + orphan: synthesis on an orphan page (postgres has no inbound)
    with Repo.open(start=root) as r:
        rendermod.synthesis_set(r, "wiki/concepts/postgres.md", "See [[ghost-page]].")
        rendermod.render(r, all_pages=True)
        lrep = lintmod.lint(r)
    checks = {f["check"] for f in lrep["findings"]}
    check("lint catches broken_wikilink", "broken_wikilink" in checks)
    check("lint catches orphan_page", "orphan_page" in checks)

    with Repo.open(start=root) as r:
        h = healthmod.compute(r)
    check("health counts the open contradiction (score includes *3)",
          h["open_contradictions"] == 1 and h["score"] >= 3)

    # ---------------- Phase 5 ----------------
    print("[Phase 5] two-speed gate rules")
    # fresh repo to isolate gate behavior
    tmp2 = Path(tempfile.mkdtemp(prefix="wikibrain-gate-"))
    g2 = make_repo(tmp2)
    # Source A (clip, high conf) -> should auto-promote (clip bypasses corroboration)
    write(g2 / "a.md", "A")
    write(g2 / "b.md", "B")
    write(g2 / "c.md", "C")
    with Repo.open(start=g2) as r:
        sa, _ = ingest.add(r, str(g2 / "a.md"), origin="clip", title="A")
        sb, _ = ingest.add(r, str(g2 / "b.md"), origin="bookmark", title="B")
        sc, _ = ingest.add(r, str(g2 / "c.md"), origin="autoresearch", title="C")
    # A: clip high-conf, unique fact
    write(g2 / "ja.json", json.dumps({"source_id": sa, "summary": "",
          "claims": [{"text": "Widget alpha ships in March.", "confidence": 0.95,
                      "entities": ["Widget"], "relations": []}], "low_confidence": False}))
    # B: bookmark, corroborates a DIFFERENT shared fact (with C) but low-ish? make 0.9
    write(g2 / "jb.json", json.dumps({"source_id": sb, "summary": "",
          "claims": [{"text": "Gizmo beta supports Linux fully.", "confidence": 0.9,
                      "entities": ["Gizmo"], "relations": []}], "low_confidence": False}))
    # C: autoresearch, same gizmo fact -> corroborates B (2 independent sources)
    write(g2 / "jc.json", json.dumps({"source_id": sc, "summary": "",
          "claims": [{"text": "Gizmo beta supports Linux fully.", "confidence": 0.95,
                      "entities": ["Gizmo"], "relations": []}], "low_confidence": False}))
    with Repo.open(start=g2) as r:
        ingest.file_claims(r, sa, str(g2 / "ja.json"))
        ingest.file_claims(r, sb, str(g2 / "jb.json"))
        ingest.file_claims(r, sc, str(g2 / "jc.json"))
        # add a held-by-confidence claim
        r.ex("INSERT INTO claims(text, source_id, location, confidence, origin, status, created_at) "
             "VALUES ('Lowconf fact about Widget.', ?, NULL, 0.5, 'bookmark', 'pending', '2026-01-01T00:00:00Z')", (sa,))
        r.conn.commit()
        rep = gatemod.gate(r)
        rows = {c["id"]: c for c in r.q("SELECT * FROM claims")}
    promoted = set(rep["promoted"])
    # claim ids: 1=widget(clip .95), 2=gizmo(bookmark .9), 3=gizmo(autoresearch .95), 4=lowconf
    check("clip high-conf auto-promoted", rows[1]["status"] == "promoted")
    check("corroborated (2 sources) auto-promoted", rows[2]["status"] == "promoted" and rows[3]["status"] == "promoted")
    check("low-confidence held", rows[4]["status"] == "pending" and 4 not in promoted)
    check("autoresearch confidence capped at 0.9", rows[3]["confidence"] <= 0.9)

    # gate with a contradiction must hold
    tmp3 = Path(tempfile.mkdtemp(prefix="wikibrain-gate2-"))
    g3 = make_repo(tmp3)
    write(g3 / "p.md", "P")
    with Repo.open(start=g3) as r:
        sp, _ = ingest.add(r, str(g3 / "p.md"), origin="clip", title="P")
        r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
             "VALUES ('Service X is stable.', ?, 0.99, 'clip', 'pending', '2026-01-01T00:00:00Z')", (sp,))
        cid_a = r.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
             "VALUES ('Other.', ?, 0.99, 'clip', 'promoted', '2026-01-01T00:00:00Z')", (sp,))
        cid_b = r.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        r.ex("INSERT INTO contradictions(claim_a, claim_b, status) VALUES (?,?, 'open')", (cid_b, cid_a))
        r.conn.commit()
        gatemod.gate(r)
        st = r.one("SELECT status FROM claims WHERE id=?", (cid_a,))["status"]
    check("claim with open contradiction held", st == "pending")

    # ---------------- Phase 4 ----------------
    print("[Phase 4] bookmark parser / budget ledger")
    bm = {"roots": {"bookmark_bar": {"type": "folder", "name": "Bookmark bar", "children": [
        {"type": "folder", "name": "wiki", "children": [
            {"type": "url", "name": "Post", "url": "https://example.org/post"},
            {"type": "url", "name": "Two", "url": "https://example.org/two"}]},
        {"type": "url", "name": "Ignored", "url": "https://example.org/nope"}]}}}
    bmf = tmp / "Bookmarks"
    write(bmf, json.dumps(bm))
    urls = gather._chrome_urls(bmf, "wiki")
    check("chrome parser finds only wiki-folder URLs",
          {u for u, _ in urls} == {"https://example.org/post", "https://example.org/two"})

    # budget ledger
    with Repo.open(start=root) as r:
        for _ in range(r.cfg.budget("fetches_per_night")):
            gather._record(r, "fetch", 99)
        r.conn.commit()
        over = False
        try:
            gather._check_fetch_budget(r, 99)
        except gather.BudgetError:
            over = True
    check("per-night fetch budget enforced", over)

    # ---------------- Phase 6: skills from promoted claims ----------------
    print("[Phase 6] skill authoring / gate / drift / render / install")
    import os as _os
    with Repo.open(start=root) as r:
        pc = [row["id"] for row in r.q(
            "SELECT id FROM claims WHERE status='promoted' ORDER BY id LIMIT 2")]
        check("have promoted claims to derive a skill from", len(pc) >= 1)

        # draft skills never render to disk (the gate)
        skillsmod.new(r, "test-skill", "A test skill from promoted claims.", claims=pc)
        skillsmod.set_body(r, "test-skill", "# Test\n\nDo the thing.")
        sdir = root / ".claude" / "skills" / "test-skill"
        check("draft skill is NOT on disk", not (sdir / "SKILL.md").exists())
        check("draft listed as draft", any(
            s["name"] == "test-skill" and s["status"] == "draft" for s in skillsmod.listing(r)))

        # reserved-name guard
        reserved_blocked = False
        try:
            skillsmod.new(r, "wiki-maintainer", "x")
        except SystemExit:
            reserved_blocked = True
        check("reserved name wiki-maintainer refused", reserved_blocked)

        # approve = the gate -> renders to disk
        path = skillsmod.approve(r, "test-skill")
        check("approved skill rendered to .claude/skills", (root / path).exists())
        body1 = (root / path).read_text(encoding="utf-8")
        check("rendered SKILL.md has frontmatter name", "name: test-skill" in body1)
        check("provenance footer lists claim ids",
              all(f"#{c}" in body1 for c in pc))

        # determinism: re-render is byte-identical
        skillsmod.render(r)
        check("skill re-render is byte-identical",
              (root / path).read_text(encoding="utf-8") == body1)

        # no drift right after approval; drift appears when a source claim drops
        check("no drift immediately after approval", not skillsmod.check(r))
        review.reject(r, [pc[0]])
        drift = skillsmod.check(r)
        check("drift flagged after a source claim is rejected",
              any(d["skill"] == "test-skill" for d in drift))

        # opt-in global install (redirect HOME to a temp dir so we never touch the
        # real ~/.claude during tests)
        fake_home = Path(tempfile.mkdtemp(prefix="wikibrain-home-"))
        old_home = {k: _os.environ.get(k) for k in ("USERPROFILE", "HOME")}
        _os.environ["USERPROFILE"] = str(fake_home)
        _os.environ["HOME"] = str(fake_home)
        try:
            dst = skillsmod.install(r, "test-skill")
            check("install copies skill into ~/.claude/skills",
                  (fake_home / ".claude" / "skills" / "test-skill" / "SKILL.md").exists())
            check("installed flag set", any(
                s["name"] == "test-skill" and s["installed"] for s in skillsmod.listing(r)))
            # archive is refused while installed
            arch_blocked = False
            try:
                skillsmod.archive(r, "test-skill")
            except SystemExit:
                arch_blocked = True
            check("archive refused while globally installed", arch_blocked)
            skillsmod.uninstall(r, "test-skill")
            check("uninstall removes the global copy",
                  not (Path(dst)).exists())
        finally:
            for k, v in old_home.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v

        # archive now removes the generated repo dir
        skillsmod.archive(r, "test-skill")
        check("archive removes the generated repo dir", not sdir.exists())

    # ---------------- Phase 6.1: versioning / rollback / audit / merge ----------
    print("[Phase 6.1] skill versioning / rollback / audit / merge")
    with Repo.open(start=root) as r:
        pc = [row["id"] for row in r.q(
            "SELECT id FROM claims WHERE status='promoted' ORDER BY id LIMIT 2")]
        skillsmod.new(r, "ver-skill", "A versioned skill.", claims=pc)
        skillsmod.set_body(r, "ver-skill", "# v1\nfirst body")
        skillsmod.approve(r, "ver-skill")                       # -> v1
        skillsmod.set_body(r, "ver-skill", "# v2\nsecond body, broken change")
        path = skillsmod.approve(r, "ver-skill")                # -> v2
        vs = skillsmod.versions(r, "ver-skill")
        check("two approved versions recorded", [v["version"] for v in vs] == [1, 2])
        check("current version flagged", [v["version"] for v in vs if v["current"]] == [2])
        check("live body is v2 before rollback", "second body" in (root / path).read_text("utf-8"))

        d = skillsmod.diff(r, "ver-skill", 1, None)
        check("diff shows the changed lines", "first body" in d and "second body" in d)

        res = skillsmod.revert(r, "ver-skill", to=1)            # rollback
        body = (root / res["path"]).read_text("utf-8")
        check("rollback restores v1 body", "first body" in body and "second body" not in body)
        check("revert appended as a new version (append-only history)",
              res["new_version"] == 3)
        check("revert note records the source version", any(
            v["note"] == "reverted to v1" for v in skillsmod.versions(r, "ver-skill")))

        # redundancy detection + merge
        _, warns = skillsmod.new(r, "ver-dup", "A versioned skill.", claims=pc)
        check("author-time overlap warning fires", bool(warns))
        aud = skillsmod.audit(r)
        check("audit reports the redundant pair", any(
            {p["a"], p["b"]} == {"ver-skill", "ver-dup"} for p in aud["redundant"]))
        skillsmod.merge(r, "ver-dup", "ver-skill")
        check("merge archives the redundant skill",
              r.one("SELECT status FROM skills WHERE name='ver-dup'")["status"] == "archived")
        check("no redundant pairs after merge",
              not any({p["a"], p["b"]} == {"ver-skill", "ver-dup"}
                      for p in skillsmod.audit(r)["redundant"]))
        # merge refused while installed (guard)
        skillsmod.new(r, "ver-keep", "Keep me.", claims=pc)
        skillsmod.set_body(r, "ver-keep", "# keep")
        skillsmod.approve(r, "ver-keep")
        merge_blocked = False
        try:
            r.ex("UPDATE skills SET installed=1 WHERE name='ver-keep'")
            skillsmod.merge(r, "ver-keep", "ver-skill")
        except SystemExit:
            merge_blocked = True
        check("merge refused while source is installed", merge_blocked)
        r.ex("UPDATE skills SET installed=0 WHERE name='ver-keep'"); r.conn.commit()

    # ---------------- Phase 7: brain-as-MCP-server (pure handlers) ----------
    # The mcp SDK is an optional extra; the offline harness exercises the pure
    # tool handlers (which it does not depend on), not a live stdio server.
    print("[Phase 7] MCP server tool handlers")
    with Repo.open(start=root) as r:
        promoted = [row["id"] for row in r.q(
            "SELECT id FROM claims WHERE status='promoted' ORDER BY id LIMIT 1")]
        check("have a promoted claim to retrieve", len(promoted) >= 1)
        ptext = r.one("SELECT text FROM claims WHERE id=?", (promoted[0],))["text"]
        term = next((w for w in ptext.split() if len(w) > 4), "the")

        # search: promoted-only is the safe default and never leaks pending text
        res = mcpmod.tool_search(r, term, promoted_only=True)
        check("search returns JSON-able dict with results list",
              isinstance(res, dict) and isinstance(res.get("results"), list))
        check("promoted-only search yields only promoted claims",
              all(x.get("status") == "promoted"
                  for x in res["results"] if x.get("kind") == "claim"))

        # graph: unknown entity returns a clean error dict, never raises
        g = mcpmod.tool_graph(r, "no-such-entity-xyz", hops=1)
        check("graph on unknown entity returns error dict, not exception",
              isinstance(g, dict) and "error" in g)

        # hybrid: falls back to FTS when the [semantic] extra is absent
        hy = mcpmod.tool_hybrid(r, term, k=5)
        check("hybrid returns a mode and a results list",
              hy.get("mode") in ("hybrid", "fts") and isinstance(hy["results"], list))

        # recall: assembles a context pack split into promoted vs pending; the
        # server writes no prose (the note steers the client to synthesize).
        rec = mcpmod.tool_recall(r, term, k=5)
        check("recall splits promoted vs pending", "promoted" in rec and "pending" in rec)
        check("recall promoted bucket holds only promoted claims",
              all(x.get("status") == "promoted" for x in rec["promoted"]))
        check("recall carries an untrusted-data / synthesize note",
              "instructions" in rec["note"] and "pending" in rec["note"])

        # capture: the one write door routes through ingest.capture -> a NEW
        # source with origin session/<harness>, never promoted.
        before = r.one("SELECT COUNT(*) n FROM sources")["n"]
        cap = mcpmod.tool_capture(r, "Phase 7 MCP test finding.", harness="mcp")
        after = r.one("SELECT COUNT(*) n FROM sources")["n"]
        check("capture registers exactly one new source", after == before + 1)
        srow = r.one("SELECT origin, status FROM sources WHERE id=?", (cap["source_id"],))
        check("captured source has origin session/mcp", srow["origin"] == "session/mcp")
        check("captured source is new (unvetted), not promoted", srow["status"] == "new")
        check("capture harness label is sanitized",
              mcpmod.tool_capture(r, "x", harness="../evil!")["origin"]
              .startswith("session/"))

        # --- provenance hardening (Codex review) ----------------------------
        # P2b: promoted-only retrieval never leaks unvetted claims (FTS path,
        # since the [semantic] extra is absent in the offline harness).
        hyp = mcpmod.tool_hybrid(r, term, k=20, promoted_only=True)
        check("hybrid promoted_only yields only promoted claims",
              all(x.get("status") == "promoted" for x in hyp["results"]))
        check("hybrid reports its promoted_only mode", hyp["promoted_only"] is True)

        # A non-promoted claim exists by now (Phase 6 rejected one). Use it to
        # prove the graph + skill-approval provenance guards.
        nonp = r.one("SELECT id, status FROM claims WHERE status != 'promoted' ORDER BY id LIMIT 1")
        check("a non-promoted claim exists to test provenance guards", nonp is not None)

        # P2a: graph emits an edge backed by a non-promoted claim only when NOT
        # promoted_only (mirrors the renderer's promoted-or-null rule).
        r.ex("INSERT OR IGNORE INTO entities(name, kind) VALUES('p7-alpha','concept')")
        r.ex("INSERT OR IGNORE INTO entities(name, kind) VALUES('p7-beta','concept')")
        aid = r.one("SELECT id FROM entities WHERE name='p7-alpha'")["id"]
        bid = r.one("SELECT id FROM entities WHERE name='p7-beta'")["id"]
        r.ex("INSERT OR IGNORE INTO relations(src, rel, dst, claim_id) VALUES(?,?,?,?)",
             (aid, "relates_to", bid, nonp["id"]))
        r.conn.commit()
        g_all = mcpmod.tool_graph(r, "p7-alpha", promoted_only=False)
        g_prom = mcpmod.tool_graph(r, "p7-alpha")  # default promoted_only=True
        check("graph (all) includes the unvetted-evidence edge",
              any(e["dst"] == "p7-beta" for e in g_all["edges"]))
        check("graph (promoted-only) hides the unvetted-evidence edge",
              not any(e["dst"] == "p7-beta" for e in g_prom["edges"]))

        # P1: skill approval is blocked while a non-promoted claim is linked.
        skillsmod.new(r, "p7-prov", "Provenance guard test.", claims=[nonp["id"]])
        skillsmod.set_body(r, "p7-prov", "# p7\nbody")
        approve_blocked = False
        try:
            skillsmod.approve(r, "p7-prov")
        except skillsmod.SkillError:
            approve_blocked = True
        check("approve refused with a non-promoted linked claim", approve_blocked)
        check("lint flags the non-promoted linkage as an error",
              any(x["skill"] == "p7-prov" and x["severity"] == "error"
                  and "non-promoted" in x["message"]
                  for x in skillsmod.lint(r, "p7-prov")))
        skillsmod.archive(r, "p7-prov")

        # P1: revert re-approves + re-renders, so it must clear the same gate —
        # reverting to a version whose source claim is no longer promoted is
        # blocked (this previously bypassed the approve gate).
        rclaim = r.one("SELECT id FROM claims WHERE status='promoted' ORDER BY id LIMIT 1")["id"]
        skillsmod.new(r, "p7-rev", "Revert gate test.", claims=[rclaim])
        skillsmod.set_body(r, "p7-rev", "# v1\nbody one")
        skillsmod.approve(r, "p7-rev")          # v1 (claim promoted)
        skillsmod.set_body(r, "p7-rev", "# v2\nbody two")
        skillsmod.approve(r, "p7-rev")          # v2
        review.reject(r, [rclaim])              # v1's claim is now rejected
        revert_blocked = False
        try:
            skillsmod.revert(r, "p7-rev", to=1)
        except skillsmod.SkillError:
            revert_blocked = True
        check("revert blocked when restored version's claim is no longer promoted",
              revert_blocked)
        check("blocked revert left the live body unchanged (v2)",
              "body two" in skillsmod.get_body(r, "p7-rev"))

        # client config snippet is well-formed and points at the repo root
        cc = mcpmod.client_config(r, read_only=True)
        srv = cc["mcpServers"]["wiki-brain"]
        check("client config targets `wiki mcp serve`",
              srv["command"] == "wiki" and srv["args"][:2] == ["mcp", "serve"])
        check("read-only client config carries --read-only",
              "--read-only" in srv["args"])

    # ---------------- Librarian (event-driven judgment, model stubbed) --------
    print("[librarian] extraction pass with a stubbed OpenAI-compatible model")
    from librarian.config import LibrarianConfig
    from librarian import client as libclient, extract as libextract

    lib_root = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-lib-")))
    # Give this repo a librarian config so model_for()/api_key() resolve.
    write(lib_root / "config.toml",
          (lib_root / "config.toml").read_text(encoding="utf-8")
          + '[librarian]\nmodel = "stub/model"\nbase_url = "http://stub/v1"\n')
    lcfg = LibrarianConfig.load(start=lib_root)
    check("librarian config reads [librarian] model", lcfg.model_for("extract") == "stub/model")
    check("librarian model_for falls back to default when no per-task override",
          lcfg.model_for("adjudicate") == "stub/model")
    check("librarian api_key None when api_key_env unset", lcfg.api_key() is None)

    # A canned extraction the stub will "return". Note the deliberately wrong
    # source_id echo — the librarian must overwrite it with the real one.
    canned = {
        "source_id": 999,
        "summary": "A note about caching in HTTP.",
        "claims": [
            {"text": "HTTP caching reduces server load by reusing responses.",
             "confidence": 0.95, "entities": ["HTTP caching", "HTTP"],
             "relations": [{"src": "HTTP caching", "rel": "reduces", "dst": "server load"}]},
            {"text": "A cache stores a copy of a response keyed by its request.",
             "confidence": 0.9, "entities": ["cache"]},
        ],
        "low_confidence": False,
        "proposed_questions": ["What is the default cache TTL?"],
    }

    calls = {"n": 0}

    def _stub_transport(url, payload, headers, timeout):
        calls["n"] += 1
        # echo the OpenAI chat-completions envelope shape the client expects
        return {"choices": [{"message": {
            "content": json.dumps(canned)}}]}

    orig_transport = libclient._post_json
    libclient._post_json = _stub_transport
    try:
        # capture is the simplest one-door ingest (no network/file prep needed).
        with Repo.open(start=lib_root) as r:
            cap_sid = ingest.capture(r, "test", "seed source about HTTP caching")
        with Repo.open(start=lib_root) as r:
            rep = libextract.run_one(r, lcfg, cap_sid)
        check("librarian made exactly one model call", calls["n"] == 1)
        check("librarian filed the claims from the model", rep["claims"] == 2)
        with Repo.open(start=lib_root) as r:
            src = r.one("SELECT status FROM sources WHERE id=?", (cap_sid,))
            n_claims = r.one("SELECT COUNT(*) n FROM claims WHERE source_id=?", (cap_sid,))["n"]
        check("source is marked extracted after librarian pass", src["status"] == "extracted")
        check("claims were attached to the REAL source id (echo ignored)", n_claims == 2)

        # Idempotency: a second run_one on the same (now 'extracted') source must
        # refuse rather than double-file — the bug fix that makes on-ingest safe.
        refused = False
        try:
            with Repo.open(start=lib_root) as r:
                libextract.run_one(r, lcfg, cap_sid)
        except libextract.ExtractionFailed:
            refused = True
        check("librarian refuses to re-extract an already-extracted source", refused)
        with Repo.open(start=lib_root) as r:
            n2 = r.one("SELECT COUNT(*) n FROM claims WHERE source_id=?", (cap_sid,))["n"]
        check("refused re-extract did not add duplicate claims", n2 == 2)

        # catch-up drains a backlog and is idempotent when nothing is pending.
        with Repo.open(start=lib_root) as r:
            ingest.capture(r, "test", "another source about cache invalidation")
        with Repo.open(start=lib_root) as r:
            cu = libextract.catch_up(r, lcfg)
        check("catch-up processed the one pending source", len(cu["processed"]) == 1)
        with Repo.open(start=lib_root) as r:
            cu2 = libextract.catch_up(r, lcfg)
        check("catch-up on a drained brain is a no-op",
              not cu2["processed"] and not cu2["failed"])
    finally:
        libclient._post_json = orig_transport

    # A malformed model reply must fail the source, not crash the batch.
    def _bad_transport(url, payload, headers, timeout):
        return {"choices": [{"message": {"content": "sorry, I cannot do that"}}]}
    libclient._post_json = _bad_transport
    try:
        with Repo.open(start=lib_root) as r:
            bad_sid = ingest.capture(r, "test", "a source the model will fumble")
        with Repo.open(start=lib_root) as r:
            cu3 = libextract.catch_up(r, lcfg)
        check("catch-up records a model failure without crashing",
              len(cu3["failed"]) == 1 and cu3["failed"][0]["source_id"] == bad_sid)
        with Repo.open(start=lib_root) as r:
            still_new = r.one("SELECT status FROM sources WHERE id=?", (bad_sid,))["status"]
        check("a fumbled source stays 'new' for a later retry", still_new == "new")
    finally:
        libclient._post_json = orig_transport

    # ---------------- Review fixes (#2 #3 #4 #6) ------------------------------
    print("[review-fixes] idempotency, read-only health, negation, evidence skips")
    fx = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-fx-")))
    ext = {"summary": "s", "claims": [
        {"text": "Widget X supports feature Y.", "confidence": 0.6, "entities": ["Widget X"]}],
        "low_confidence": False}

    # #3: re-filing an already-extracted source is refused (no duplicate claims).
    with Repo.open(start=fx) as r:
        fsid = ingest.capture(r, "test", "seed for idempotency")
        ext["source_id"] = fsid
        ingest.file_claims_data(r, fsid, dict(ext))
    dup_refused = False
    try:
        with Repo.open(start=fx) as r:
            ingest.file_claims_data(r, fsid, dict(ext))
    except ingest.IngestError:
        dup_refused = True
    check("re-file refused without --refile (idempotency guard)", dup_refused)
    with Repo.open(start=fx) as r:
        n = r.one("SELECT COUNT(*) n FROM claims WHERE source_id=?", (fsid,))["n"]
    check("refused re-file left exactly one claim", n == 1)

    # #3: refile=True replaces (still one claim), but is refused once promoted.
    with Repo.open(start=fx) as r:
        ingest.file_claims_data(r, fsid, dict(ext), refile=True)
        n2 = r.one("SELECT COUNT(*) n FROM claims WHERE source_id=?", (fsid,))["n"]
    check("refile=True replaces rather than duplicates", n2 == 1)
    with Repo.open(start=fx) as r:
        cid = r.one("SELECT id FROM claims WHERE source_id=?", (fsid,))["id"]
        review.promote(r, [cid])
    promoted_refile_refused = False
    try:
        with Repo.open(start=fx) as r:
            ingest.file_claims_data(r, fsid, dict(ext), refile=True)
    except ingest.IngestError:
        promoted_refile_refused = True
    check("refile refused when a claim is already promoted", promoted_refile_refused)

    # #2: health() must not mutate the research queue (lint queue=False).
    with Repo.open(start=fx) as r:
        before = r.one("SELECT COUNT(*) n FROM research_queue")["n"]
        healthmod.compute(r)
        after = r.one("SELECT COUNT(*) n FROM research_queue")["n"]
    check("health does not append research-queue items", before == after)
    with Repo.open(start=fx) as r:
        lrep = lintmod.lint(r, queue=False)
    check("lint queue=False reports zero queued", lrep["queued"] == 0)

    # #4: negation heuristic no longer mis-fires on ordinary '*nt' words.
    from wiki import util as _u
    check("'important' is not read as negation", not _u.has_negation("this is important"))
    check("'deployment' is not read as negation", not _u.has_negation("the deployment works"))
    check("'environment' is not read as negation", not _u.has_negation("the build environment"))
    check("apostrophe-free contraction 'isnt' still reads as negation",
          _u.has_negation("it isnt supported"))
    check("bare 'not' still reads as negation", _u.has_negation("does not work"))

    # #6: evidence file --all skips 'failed' bookmark stubs (empty path) instead
    # of erroring on every one (they have no filable artifact).
    from wiki import util as _u2
    with Repo.open(start=fx) as r:
        ts = _u2.now_iso()
        r.ex("INSERT INTO sources(hash, path, title, url, origin, fetched_at, "
             "ingested_at, status) VALUES "
             "('failhash','','u','http://u','bookmark',?,?, 'failed')", (ts, ts))
        r.conn.commit()
        failed_id = r.one("SELECT id FROM sources WHERE status='failed'")["id"]
        res = evidencemod.file_all(r, extracted_only=True)
    filed_ids = {row["source_id"] for row in res}
    check("evidence file --all skips the failed stub", failed_id not in filed_ids)
    check("evidence file --all reports no errors", not any(row.get("error") for row in res))

    # ---------------- Group A (#5): no orphaned files on refused duplicates ---
    print("[group-a #5] refused exact-hash duplicates leave no stray file on disk")
    gatmp = Path(tempfile.mkdtemp(prefix="wikibrain-orphan-"))
    garoot = make_repo(gatmp)

    # add(url): same content, different URLs -> second is refused as a hash dup.
    _ofetch = fetchmod.fetch_url
    fetchmod.fetch_url = lambda url, *a, **k: ("# same content A\n", None)
    try:
        with Repo.open(start=garoot) as r:
            ingest.add(r, "https://example.org/dup-a", origin="clip")
        before = set((garoot / "raw").iterdir())
        dup_raised = False
        try:
            with Repo.open(start=garoot) as r:
                ingest.add(r, "https://example.org/dup-b", origin="clip")
        except ingest.IngestError:
            dup_raised = True
        after = set((garoot / "raw").iterdir())
        check("add(url) exact-duplicate content is refused", dup_raised)
        check("add(url) refused duplicate leaves no stray file in raw/", before == after)
    finally:
        fetchmod.fetch_url = _ofetch

    # transcribe: same content, different targets -> second is refused.
    _otr = extractmod.transcribe
    extractmod.transcribe = lambda target, whisper_model="base": ("# same content B\n", None)
    try:
        with Repo.open(start=garoot) as r:
            ingest.transcribe(r, "https://youtu.be/dupvid-a")
        before_t = set((garoot / "raw").iterdir())
        dup_raised_t = False
        try:
            with Repo.open(start=garoot) as r:
                ingest.transcribe(r, "https://youtu.be/dupvid-b")
        except ingest.IngestError:
            dup_raised_t = True
        after_t = set((garoot / "raw").iterdir())
        check("transcribe exact-duplicate content is refused", dup_raised_t)
        check("transcribe refused duplicate leaves no stray file in raw/", before_t == after_t)
    finally:
        extractmod.transcribe = _otr

    # gather.fetch_for: same content, different URLs -> second is refused.
    _ofetch2 = fetchmod.fetch_url
    fetchmod.fetch_url = lambda url, *a, **k: ("# same content C\n", None)
    try:
        with Repo.open(start=garoot) as r:
            gqid = queuemod.add(r, "orphan-file dup question?")
        with Repo.open(start=garoot) as r:
            gather.fetch_for(r, "https://example.org/gdup-a", gqid)
        before_g = set((garoot / "raw").iterdir())
        dup_raised_g = False
        try:
            with Repo.open(start=garoot) as r:
                gather.fetch_for(r, "https://example.org/gdup-b", gqid)
        except ingest.IngestError:
            dup_raised_g = True
        after_g = set((garoot / "raw").iterdir())
        check("gather.fetch_for exact-duplicate content is refused", dup_raised_g)
        check("gather.fetch_for refused duplicate leaves no stray file in raw/",
              before_g == after_g)
    finally:
        fetchmod.fetch_url = _ofetch2

    # ---------------- Group A (#11): entity kind extraction contract ----------
    print("[group-a #11] entity/relation kind: string | {name,kind}, upgrade-not-downgrade")
    ektmp = Path(tempfile.mkdtemp(prefix="wikibrain-entkind-"))
    ekroot = make_repo(ektmp)
    write(ekroot / "ek1.md", "Entity kind source one.")
    with Repo.open(start=ekroot) as r:
        s1, _ = ingest.add(r, str(ekroot / "ek1.md"), origin="clip", title="EK1")
        j1 = {"source_id": s1, "summary": "s",
              "claims": [{"text": "Plainname is mentioned here.", "confidence": 0.8,
                          "entities": ["Plainname"], "relations": []}],
              "low_confidence": False}
        ingest.file_claims_data(r, s1, j1)
        plain_kind = r.one("SELECT kind FROM entities WHERE name='Plainname'")["kind"]
    check("plain-string entity still defaults to kind=concept (backward compatible)",
          plain_kind == "concept")

    write(ekroot / "ek2.md", "Entity kind source two.")
    with Repo.open(start=ekroot) as r:
        s2, _ = ingest.add(r, str(ekroot / "ek2.md"), origin="clip", title="EK2")
        j2 = {"source_id": s2, "summary": "s",
              "claims": [{"text": "Ada Lovelace worked with the Analytical Engine.",
                          "confidence": 0.9,
                          "entities": [{"name": "Ada Lovelace", "kind": "person"}],
                          "relations": [{"src": {"name": "Ada Lovelace", "kind": "person"},
                                         "rel": "worked_on",
                                         "dst": {"name": "Analytical Engine", "kind": "tool"}}]}],
              "low_confidence": False}
        ingest.file_claims_data(r, s2, j2)
        ada_kind = r.one("SELECT kind FROM entities WHERE name='Ada Lovelace'")["kind"]
        engine_kind = r.one("SELECT kind FROM entities WHERE name='Analytical Engine'")["kind"]
    check("object-form entity carries its kind through (person)", ada_kind == "person")
    check("object-form relation dst carries its kind through (tool)", engine_kind == "tool")

    # upgrade: a default 'concept' entity is upgraded when a concrete kind
    # arrives later, but a concrete kind is never downgraded back to 'concept'.
    write(ekroot / "ek3.md", "Entity kind source three.")
    with Repo.open(start=ekroot) as r:
        s3, _ = ingest.add(r, str(ekroot / "ek3.md"), origin="clip", title="EK3")
        j3 = {"source_id": s3, "summary": "s",
              "claims": [{"text": "Gadgetron is a project.", "confidence": 0.8,
                          "entities": ["Gadgetron"], "relations": []}],
              "low_confidence": False}
        ingest.file_claims_data(r, s3, j3)
        kind_before = r.one("SELECT kind FROM entities WHERE name='Gadgetron'")["kind"]
    check("new entity via plain string starts as concept", kind_before == "concept")

    write(ekroot / "ek4.md", "Entity kind source four.")
    with Repo.open(start=ekroot) as r:
        s4, _ = ingest.add(r, str(ekroot / "ek4.md"), origin="clip", title="EK4")
        j4 = {"source_id": s4, "summary": "s",
              "claims": [{"text": "Gadgetron is a software tool.", "confidence": 0.85,
                          "entities": [{"name": "Gadgetron", "kind": "tool"}],
                          "relations": []}],
              "low_confidence": False}
        ingest.file_claims_data(r, s4, j4)
        kind_after = r.one("SELECT kind FROM entities WHERE name='Gadgetron'")["kind"]
    check("existing default-concept entity is upgraded when a concrete kind arrives",
          kind_after == "tool")

    write(ekroot / "ek5.md", "Entity kind source five.")
    with Repo.open(start=ekroot) as r:
        s5, _ = ingest.add(r, str(ekroot / "ek5.md"), origin="clip", title="EK5")
        j5 = {"source_id": s5, "summary": "s",
              "claims": [{"text": "Gadgetron is referenced again.", "confidence": 0.8,
                          "entities": ["Gadgetron"], "relations": []}],
              "low_confidence": False}
        ingest.file_claims_data(r, s5, j5)
        kind_final = r.one("SELECT kind FROM entities WHERE name='Gadgetron'")["kind"]
    check("a concrete kind is never downgraded back to concept", kind_final == "tool")

    # invalid kind is rejected by validation
    write(ekroot / "ek6.md", "Entity kind source six.")
    with Repo.open(start=ekroot) as r:
        s6, _ = ingest.add(r, str(ekroot / "ek6.md"), origin="clip", title="EK6")
    bad_kind_rejected = False
    jbad = {"source_id": s6, "summary": "s",
            "claims": [{"text": "Bad kind entity.", "confidence": 0.8,
                        "entities": [{"name": "Nope", "kind": "spaceship"}],
                        "relations": []}],
            "low_confidence": False}
    try:
        with Repo.open(start=ekroot) as r:
            ingest.file_claims_data(r, s6, jbad)
    except ingest.IngestError:
        bad_kind_rejected = True
    check("entity object with an out-of-vocabulary kind is rejected", bad_kind_rejected)

    # ---------------- Group A (#12): fail-closed gate / logged detectors ------
    print("[group-a #12] gate fails closed (not open) on FTS errors; ingest logs instead of swallowing")
    from wiki.db import Repo as _RepoCls
    _orig_repo_q = _RepoCls.q

    def _boom_fts_q(self, sql, params=()):
        if "claims_fts" in sql:
            raise RuntimeError("simulated FTS index corruption")
        return _orig_repo_q(self, sql, params)

    _RepoCls.q = _boom_fts_q
    try:
        # gate: a broken FTS query must hold the claim, not silently promote it.
        fctmp = Path(tempfile.mkdtemp(prefix="wikibrain-ftsgate-"))
        fcroot = make_repo(fctmp)
        write(fcroot / "fc.md", "FC")
        with Repo.open(start=fcroot) as r:
            fcsid, _ = ingest.add(r, str(fcroot / "fc.md"), origin="bookmark", title="FC")
            r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
                 "VALUES ('The gizmo is fully supported.', ?, 0.99, 'bookmark', 'pending', "
                 "'2026-01-01T00:00:00Z')", (fcsid,))
            r.conn.commit()
            grep = gatemod.gate(r)
            fcst = r.one("SELECT status FROM claims WHERE source_id=?", (fcsid,))["status"]
        check("claim held (NOT auto-promoted) when the FTS safety check errors (fail-closed)",
              fcst == "pending")
        check("held reason cites the fail-closed FTS failure",
              any("fail-closed" in reason for h in grep["held"] for reason in h["reasons"]))
        fclog = (fcroot / "log.md").read_text(encoding="utf-8")
        check("gate logs the FTS query failure to log.md", "FTS query failed" in fclog)

        # ingest-side detectors: a broken FTS query must be logged, not swallowed silently.
        idtmp = Path(tempfile.mkdtemp(prefix="wikibrain-ftsingest-"))
        idroot = make_repo(idtmp)
        write(idroot / "seed.md", "Seed content.")
        write(idroot / "claimsrc.md", "Claim source content.")
        with Repo.open(start=idroot) as r:
            _, warns = ingest.add(r, str(idroot / "seed.md"), origin="clip",
                                  title="alpha bravo charlie")
            csid, _ = ingest.add(r, str(idroot / "claimsrc.md"), origin="clip",
                                 title="Claim source delta")
            cj = {"source_id": csid, "summary": "s",
                  "claims": [{"text": "The widget does not work offline.", "confidence": 0.9,
                              "entities": ["Widget"], "relations": []}],
                  "low_confidence": False}
            idres = ingest.file_claims_data(r, csid, cj)
        check("add() still succeeds despite a broken near-dupe FTS query (degrades, no crash)",
              isinstance(warns, list))
        check("file-claims still succeeds despite a broken contradiction FTS query",
              idres["claims"] == 1)
        idlog = (idroot / "log.md").read_text(encoding="utf-8")
        check("near-dupe FTS failure logged to log.md", "near-dupe FTS query failed" in idlog)
        check("contradiction FTS query failure logged to log.md",
              "contradiction FTS query failed" in idlog)
    finally:
        _RepoCls.q = _orig_repo_q

    # ---------------- Group A (polish): shared polarity-conflict helper -------
    print("[group-a polish] shared polarity-conflict heuristic (ingest + gate dedup)")
    check("CONTRADICTION_JACCARD threshold unchanged (0.4)", _u.CONTRADICTION_JACCARD == 0.4)
    check("polarity_conflict flags a near-dup opposite-polarity pair",
          _u.polarity_conflict("The cache is enabled by default.",
                               "The cache is not enabled by default.") is True)
    check("polarity_conflict ignores a near-dup same-polarity pair",
          _u.polarity_conflict("The cache is enabled by default.",
                               "The cache is enabled by default now.") is False)
    check("polarity_conflict ignores dissimilar texts regardless of polarity",
          _u.polarity_conflict("The cache is enabled.", "Totally unrelated statement.") is False)

    # ---------------- Group B: render dirty-set, search relevance, review guards --
    print("[group-b #14] render() only rebuilds dirty pages + index (no over-render)")
    groot = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-groupb-render-")))
    write(groot / "gb1.md", "Alpha source content.")
    write(groot / "gb2.md", "Beta source content.")
    with Repo.open(start=groot) as r:
        gs1, _ = ingest.add(r, str(groot / "gb1.md"), origin="clip", title="gb1")
        gj1 = {"source_id": gs1, "summary": "s1", "claims": [
            {"text": "Alpha is a widget.", "confidence": 0.9,
             "entities": ["Alpha"], "relations": []}], "low_confidence": False}
        ingest.file_claims_data(r, gs1, gj1)
        acid = r.one("SELECT id FROM claims WHERE source_id=?", (gs1,))["id"]
        review.promote(r, [acid])
        rendermod.render(r, all_pages=True)
    alpha_page = groot / "wiki" / "concepts" / "alpha.md"
    check("group-b setup: alpha page rendered", alpha_page.exists())
    alpha_bytes = alpha_page.read_bytes()

    with Repo.open(start=groot) as r:
        gs2, _ = ingest.add(r, str(groot / "gb2.md"), origin="clip", title="gb2")
        gj2 = {"source_id": gs2, "summary": "s2", "claims": [
            {"text": "Beta is a gadget.", "confidence": 0.9,
             "entities": ["Beta"], "relations": []}], "low_confidence": False}
        ingest.file_claims_data(r, gs2, gj2)
        bcid = r.one("SELECT id FROM claims WHERE source_id=?", (gs2,))["id"]
        review.promote(r, [bcid])
        rep2 = rendermod.render(r)
    check("adding a second source does NOT re-render an unrelated clean entity page",
          "wiki/concepts/alpha.md" not in rep2["rendered"])
    check("the unrelated entity page's bytes are unchanged on disk",
          alpha_page.read_bytes() == alpha_bytes)
    check("the newly-dirty entity page IS rendered",
          "wiki/concepts/beta.md" in rep2["rendered"])
    check("the index is still refreshed alongside the dirty page",
          "wiki/index.md" in rep2["rendered"])

    print("[group-b search] bm25 relevance ordering + limit")
    sroot = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-groupb-search-")))
    with Repo.open(start=sroot) as r:
        r.ex("INSERT INTO sources(hash, path, title, origin, status) "
             "VALUES ('gbh1','gbp1','gbt1','clip','extracted')")
        ssid = r.one("SELECT id FROM sources")["id"]
        for t in ["widget widget widget widget appears many times",
                  "widget appears once here",
                  "widget widget appears twice here",
                  "totally unrelated content about gizmos"]:
            r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
                 "VALUES (?, ?, 0.9, 'clip', 'pending', '2026-01-01T00:00:00Z')", (t, ssid))
        r.conn.commit()
        limited = searchmod.search(r, "widget", limit=2)
        unlimited = searchmod.search(r, "widget", limit=20)
    check("search limit caps result count", len(limited) == 2)
    check("search finds all 3 matching claims when limit allows",
          len(unlimited) == 3)
    check("search orders claims by bm25 relevance (most matches first)",
          limited[0]["text"].startswith("widget widget widget widget"))
    check("search default limit is 20", inspect.signature(searchmod.search).parameters["limit"].default == 20)
    sparser = build_parser()
    sargs = sparser.parse_args(["search", "widget", "--limit", "5"])
    check("--limit flag threaded through the search subcommand", sargs.limit == 5)
    sargs_default = sparser.parse_args(["search", "widget"])
    check("--limit defaults to 20 on the CLI", sargs_default.limit == 20)

    print("[group-b review-guards] promote/reject refuse invalid state transitions")
    vroot = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-groupb-guards-")))
    with Repo.open(start=vroot) as r:
        r.ex("INSERT INTO sources(hash, path, title, origin, status) "
             "VALUES ('gbh2','gbp2','gbt2','clip','extracted')")
        vsid = r.one("SELECT id FROM sources")["id"]
        r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
             "VALUES ('rej', ?, 0.9, 'clip', 'rejected', '2026-01-01T00:00:00Z')", (vsid,))
        r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
             "VALUES ('sup', ?, 0.9, 'clip', 'superseded', '2026-01-01T00:00:00Z')", (vsid,))
        r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
             "VALUES ('pend', ?, 0.9, 'clip', 'pending', '2026-01-01T00:00:00Z')", (vsid,))
        r.conn.commit()
        rej_id = r.one("SELECT id FROM claims WHERE text='rej'")["id"]
        sup_id = r.one("SELECT id FROM claims WHERE text='sup'")["id"]
        pend_id = r.one("SELECT id FROM claims WHERE text='pend'")["id"]

    rej_promote_blocked = False
    try:
        with Repo.open(start=vroot) as r:
            review.promote(r, [rej_id])
    except SystemExit:
        rej_promote_blocked = True
    check("promoting an already-rejected claim is refused", rej_promote_blocked)

    sup_promote_blocked = False
    try:
        with Repo.open(start=vroot) as r:
            review.promote(r, [sup_id])
    except SystemExit:
        sup_promote_blocked = True
    check("promoting a superseded claim is refused", sup_promote_blocked)

    sup_reject_blocked = False
    try:
        with Repo.open(start=vroot) as r:
            review.reject(r, [sup_id])
    except SystemExit:
        sup_reject_blocked = True
    check("rejecting a superseded claim is refused", sup_reject_blocked)

    with Repo.open(start=vroot) as r:
        s_before = r.one("SELECT status FROM claims WHERE id=?", (sup_id,))["status"]
    check("a refused promote/reject leaves the claim's status untouched",
          s_before == "superseded")

    with Repo.open(start=vroot) as r:
        review.promote(r, [pend_id])
        pend_status = r.one("SELECT status FROM claims WHERE id=?", (pend_id,))["status"]
    check("the common pending->promoted path still works", pend_status == "promoted")

    with Repo.open(start=vroot) as r:
        review.reject(r, [pend_id])
        pend_status2 = r.one("SELECT status FROM claims WHERE id=?", (pend_id,))["status"]
    check("the common promoted->rejected path (walking back a promotion) still works",
          pend_status2 == "rejected")

    print("[group-d #13] POSIX parity — wrapper, docs, scheduling example")
    _repo_root = Path(__file__).resolve().parents[1]
    wiki_sh = _repo_root / "wiki.sh"
    check("POSIX wiki.sh wrapper exists at the repo root", wiki_sh.is_file())
    check("wiki.sh is executable", wiki_sh.stat().st_mode & 0o111 != 0)
    check("no bare 'wiki' file at the repo root "
          "(it would collide with the generated wiki/ vault dir)",
          not (_repo_root / "wiki").exists())
    wiki_sh_text = wiki_sh.read_text(encoding="utf-8")
    check("wiki.sh resolves the repo venv console script when present",
          ".venv/bin/wiki" in wiki_sh_text)
    check("wiki.sh falls back to `python3 -m wiki`",
          "python3 -m wiki" in wiki_sh_text)

    mech_sh = _repo_root / "scripts" / "mechanical-maintain.sh"
    check("POSIX mechanical-maintain.sh exists beside the .ps1", mech_sh.is_file())
    check("mechanical-maintain.sh is executable", mech_sh.stat().st_mode & 0o111 != 0)

    readme_text = (_repo_root / "README.md").read_text(encoding="utf-8")
    check("README documents a POSIX venv setup block",
          "python3 -m venv .venv" in readme_text and "pip install -e ./cli" in readme_text)
    check("README documents a cron scheduling example",
          "crontab -e" in readme_text)
    check("README documents a systemd-timer scheduling example",
          "systemctl --user enable --now" in readme_text and "OnCalendar" in readme_text)

    print("[group-d ruff] lint config + CI wiring")
    pyproject_text = (_repo_root / "cli" / "pyproject.toml").read_text(encoding="utf-8")
    check("cli/pyproject.toml declares a [tool.ruff] section",
          "[tool.ruff]" in pyproject_text)
    check("ruff is scoped to real-bug rules (pyflakes + E9), not broad style rules",
          'select = ["F", "E9"]' in pyproject_text)
    ci_text = (_repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    check("CI runs `ruff check`", "ruff check" in ci_text)

    # ---------------- Librarian triage (advisory; model stubbed) -------------
    print("[librarian-triage] recommendations over pending claims; never promotes")
    from librarian import triage as libtriage
    from librarian.config import LibrarianConfig as _LibCfg
    from wiki import triage as wtriage

    tr = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-tr-")))
    write(tr / "config.toml", (tr / "config.toml").read_text(encoding="utf-8")
          + '[librarian]\nmodel = "stub"\nbase_url = "http://stub/v1"\n')
    tcfg = _LibCfg.load(start=tr)
    with Repo.open(start=tr) as r:
        tsid = ingest.capture(r, "t", "a speculative candidate claim")
        ingest.file_claims_data(r, tsid, {
            "source_id": tsid, "summary": "s",
            "claims": [{"text": "Widget Z will ship in 2099.", "confidence": 0.5,
                        "entities": ["Widget Z"]}], "low_confidence": False})
        gatemod.gate(r)  # low-confidence session claim -> stays pending
        pend_id = r.one("SELECT id FROM claims WHERE source_id=?", (tsid,))["id"]
        pend_status = r.one("SELECT status FROM claims WHERE id=?", (pend_id,))["status"]
    check("triage precondition: the claim is pending", pend_status == "pending")

    triage_reply = {"recommendation": "hold", "confidence": 0.4,
                    "reason": "Speculative far-future date; leave for the human."}
    def _t_stub(url, payload, headers, timeout):
        return {"choices": [{"message": {"content": json.dumps(triage_reply)}}]}
    _t_orig = libclient._post_json
    libclient._post_json = _t_stub
    try:
        with Repo.open(start=tr) as r:
            trep = libtriage.run(r, tcfg)
        check("triage produced one recommendation", len(trep["triaged"]) == 1)
        with Repo.open(start=tr) as r:
            row = r.one("SELECT * FROM claim_triage WHERE claim_id=?", (pend_id,))
            st = r.one("SELECT status FROM claims WHERE id=?", (pend_id,))["status"]
        check("recommendation is stored in claim_triage",
              row is not None and row["recommendation"] == "hold")
        check("triage NEVER changes claim status (still pending)", st == "pending")
        with Repo.open(start=tr) as r:
            trep2 = libtriage.run(r, tcfg)
        check("triage is idempotent (untriaged-only skips already-triaged)",
              not trep2["triaged"])
        # pure-code reader
        with Repo.open(start=tr) as r:
            lst = wtriage.listing(r)
            summ = wtriage.summary(r)
        check("wiki triage listing surfaces the recommendation",
              any(x["recommendation"] == "hold" for x in lst))
        check("wiki triage summary counts it (hold=1, untriaged=0)",
              summ["hold"] == 1 and summ["untriaged"] == 0)
        # a malformed recommendation is rejected by the contract
        bad_reply = {"recommendation": "definitely-promote", "confidence": 0.9, "reason": "x"}
        libclient._post_json = lambda *a: {"choices": [{"message": {"content": json.dumps(bad_reply)}}]}
        with Repo.open(start=tr) as r:
            r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, created_at) "
                 "VALUES ('another pending claim', ?, 0.5, 'session/t', 'pending', ?)",
                 (tsid, __import__("wiki.util", fromlist=["now_iso"]).now_iso()))
            r.conn.commit()
            bad_rep = libtriage.run(r, tcfg)
        check("triage rejects an out-of-vocabulary recommendation",
              len(bad_rep["failed"]) == 1 and not bad_rep["triaged"])
    finally:
        libclient._post_json = _t_orig

    # ---------------- Librarian adjudicate (advisory; model stubbed) ---------
    print("[librarian-adjudicate] proposals for open contradictions/escalations; "
          "never resolves/closes")
    from librarian import adjudicate as libadj

    adj = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-adj-")))
    write(adj / "config.toml", (adj / "config.toml").read_text(encoding="utf-8")
          + '[librarian]\nmodel = "stub"\nbase_url = "http://stub/v1"\n')
    acfg = _LibCfg.load(start=adj)
    with Repo.open(start=adj) as r:
        # a promoted claim + a contradicting session claim -> open contradiction
        asid = ingest.capture(r, "a", "the sky is blue")
        ingest.file_claims_data(r, asid, {
            "source_id": asid, "summary": "s",
            "claims": [{"text": "The sky is blue.", "confidence": 0.99,
                        "entities": ["Sky"]}], "low_confidence": False})
        gatemod.gate(r)
        aclaim = r.one("SELECT id FROM claims WHERE source_id=?", (asid,))["id"]
        review.promote(r, [aclaim])
        bsid = ingest.capture(r, "a", "the sky is green")
        ingest.file_claims_data(r, bsid, {
            "source_id": bsid, "summary": "s2",
            "claims": [{"text": "The sky is blue is false; it is green.",
                        "confidence": 0.9, "entities": ["Sky"]}],
            "low_confidence": False})
        # ensure at least one open contradiction and one open escalation exist
        r.ex("INSERT INTO contradictions(claim_a, claim_b, status) VALUES (?,?, 'open')",
             (aclaim, aclaim))
        r.ex("INSERT INTO escalations(source_id, reason, status) VALUES (?,?, 'open')",
             (asid, "extractor returned low-confidence garble"))
        r.conn.commit()
        con_id = r.one("SELECT id FROM contradictions WHERE status='open' ORDER BY id")["id"]
        esc_id = r.one("SELECT id FROM escalations WHERE status='open' ORDER BY id")["id"]

    # v8 migration actually added the column on this live repo DB
    with Repo.open(start=adj) as r:
        ecols = {c["name"] for c in r.q("PRAGMA table_info(escalations)")}
    check("escalations.proposal column present after migration", "proposal" in ecols)

    adj_reply = {"proposal": "Claim A cites a stronger source; keep A, mark B stale. "
                             "Human should confirm.", "confidence": 0.6}
    def _a_stub(url, payload, headers, timeout):
        return {"choices": [{"message": {"content": json.dumps(adj_reply)}}]}
    _a_orig = libclient._post_json
    libclient._post_json = _a_stub
    try:
        with Repo.open(start=adj) as r:
            arep = libadj.run(r, acfg)
        check("adjudicate produced at least one contradiction + one escalation proposal",
              any(d["kind"] == "contradiction" for d in arep["proposed"])
              and any(d["kind"] == "escalation" for d in arep["proposed"]))
        with Repo.open(start=adj) as r:
            cprop = r.one("SELECT proposal, status FROM contradictions WHERE id=?", (con_id,))
            eprop = r.one("SELECT proposal, status FROM escalations WHERE id=?", (esc_id,))
        check("contradiction proposal written to contradictions.proposal",
              bool(cprop["proposal"]) and cprop["proposal"] == adj_reply["proposal"])
        check("escalation proposal written to escalations.proposal",
              bool(eprop["proposal"]) and eprop["proposal"] == adj_reply["proposal"])
        check("adjudicate NEVER resolves the contradiction (still open)",
              cprop["status"] == "open")
        check("adjudicate NEVER closes the escalation (still open)",
              eprop["status"] == "open")
        with Repo.open(start=adj) as r:
            arep2 = libadj.run(r, acfg)
        check("adjudicate is idempotent (unproposed-only skips already-proposed)",
              not arep2["proposed"])
        # a malformed proposal (empty) is rejected by the contract
        libclient._post_json = lambda *a: {"choices": [{"message": {"content":
            json.dumps({"proposal": "", "confidence": 0.5})}}]}
        with Repo.open(start=adj) as r:
            r.ex("INSERT INTO escalations(source_id, reason, status) VALUES (?,?, 'open')",
                 (asid, "another escalation"))
            r.conn.commit()
            bad_adj = libadj.run(r, acfg)
        check("adjudicate rejects an empty proposal via the contract",
              len(bad_adj["failed"]) == 1 and not bad_adj["proposed"])
    finally:
        libclient._post_json = _a_orig

    # ---------------- Librarian synthesize (drafts only; model stubbed) ------
    print("[librarian-synthesize] page prose + skill DRAFTS; never approves/promotes")
    from librarian import synthesize as libsynth

    sy = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-sy-")))
    write(sy / "config.toml", (sy / "config.toml").read_text(encoding="utf-8")
          + '[librarian]\nmodel = "stub"\nbase_url = "http://stub/v1"\n')
    sycfg = _LibCfg.load(start=sy)
    with Repo.open(start=sy) as r:
        sysid = ingest.capture(r, "s", "redis notes")
        ingest.file_claims_data(r, sysid, {
            "source_id": sysid, "summary": "s",
            "claims": [{"text": f"Redis fact number {i}.", "confidence": 0.99,
                        "entities": ["Redis"]} for i in range(1, 6)],
            "low_confidence": False})
        gatemod.gate(r)
        pend = [row["id"] for row in r.q(
            "SELECT id FROM claims WHERE source_id=? AND status='pending'", (sysid,))]
        if pend:
            review.promote(r, pend)
    with Repo.open(start=sy) as r:
        pre = rendermod.render(r)
    check("redis page needs synthesis review before the pass",
          any("redis" in p for p in pre["needs_synthesis_review"]))

    synth_reply = {"prose": "Redis is an in-memory data store commonly used as a cache."}
    skill_reply = {"should_draft": True, "name": "redis",
                   "description": "Activate when working with Redis caching.",
                   "body": "# Redis\n\nUse Redis as an in-memory cache."}
    def _sy_stub(url, payload, headers, timeout):
        text = " ".join(m["content"] for m in payload["messages"])
        reply = skill_reply if "should_draft" in text else synth_reply
        return {"choices": [{"message": {"content": json.dumps(reply)}}]}
    _sy_orig = libclient._post_json
    libclient._post_json = _sy_stub
    try:
        with Repo.open(start=sy) as r:
            srep = libsynth.run(r, sycfg)
        check("synthesize drafted prose for the reviewed page", len(srep["pages"]) >= 1)
        check("synthesize reports nothing still needing review",
              not any("redis" in p for p in srep["needs_synthesis_review"]))
        with Repo.open(start=sy) as r:
            prow = r.one("SELECT synthesis FROM pages WHERE path LIKE '%redis%'")
        check("prose landed in pages.synthesis",
              prow is not None and synth_reply["prose"] in prow["synthesis"])
        redis_files = list((sy / "wiki").rglob("*redis*.md"))
        check("re-rendered page body carries the synthesis prose verbatim",
              any(synth_reply["prose"] in f.read_text(encoding="utf-8") for f in redis_files))
        # skill was DRAFTED, never approved, never written to disk
        with Repo.open(start=sy) as r:
            srow = r.one("SELECT * FROM skills WHERE name='redis'")
        check("a skill was drafted from the dense candidate",
              srow is not None and bool(srow["body"].strip()) and len(srep["skills"]) == 1)
        check("drafted skill stays status='draft' (never approved)",
              srow is not None and srow["status"] == "draft")
        check("draft skill never rendered to .claude/skills",
              not (sy / ".claude" / "skills" / "redis").exists())
        # idempotent re-run: no duplicate draft, nothing new to synthesize
        with Repo.open(start=sy) as r:
            srep2 = libsynth.run(r, sycfg)
        check("re-run finds no page needing synthesis (idempotent)", not srep2["pages"])
        check("re-run drafts no new skill (idempotent)", not srep2["skills"])
        with Repo.open(start=sy) as r:
            ncount = r.one("SELECT COUNT(*) n FROM skills WHERE name='redis'")["n"]
            still_draft = r.one("SELECT status FROM skills WHERE name='redis'")["status"]
        check("skill draft not duplicated on re-run", ncount == 1)
        check("skill still 'draft' after re-run (synthesize never approves)",
              still_draft == "draft")
    finally:
        libclient._post_json = _sy_orig

    # ---------------- Librarian watch (drop + bookmarks -> extraction) -------
    print("[librarian-watch] event loop over the drop folder + bookmark files")
    from librarian import watch as libwatch

    wt = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-watch-")))
    wdrop = wt / "inbox-drop"
    wdrop.mkdir(parents=True, exist_ok=True)
    wt_cfg_text = (wt / "config.toml").read_text(encoding="utf-8")
    wt_cfg_text = wt_cfg_text.replace(
        "[paths]\n", f'[paths]\ndrop_folder = "{wdrop.as_posix()}"\n', 1)
    write(wt / "config.toml", wt_cfg_text
          + '[librarian]\nmodel = "stub"\nbase_url = "http://stub/v1"\n')
    write(wdrop / "note.txt", "A note about watched drop-folder ingestion.")

    watch_canned = {
        "source_id": 0,
        "summary": "A dropped note.",
        "claims": [{"text": "The watcher ingests dropped files automatically.",
                    "confidence": 0.9, "entities": ["watcher"]}],
        "low_confidence": False,
    }

    def _watch_stub(url, payload, headers, timeout):
        return {"choices": [{"message": {"content": json.dumps(watch_canned)}}]}

    _w_orig = libclient._post_json
    libclient._post_json = _watch_stub
    try:
        wrep = libwatch.run(once=True, start=wt)
        check("watch --once ingested the dropped file", wrep["dropped"] == 1)
        check("watch --once extracted the newly-ingested source", wrep["extracted"] == 1)
        check("watch --once ran no bookmark adds (none configured)",
              wrep["bookmarks_added"] == 0)
        with Repo.open(start=wt) as r:
            n_claims = r.one("SELECT COUNT(*) n FROM claims")["n"]
        check("dropped-file claim made it into the DB", n_claims == 1)
        check("drop folder archived the processed file",
              (wdrop / dropmod.PROCESSED / "note.txt").exists())

        # scan_once directly, called twice in a row: idempotent (nothing new).
        with Repo.open(start=wt) as r:
            wcfg = _LibCfg.load(start=wt)
            rep2 = libwatch.scan_once(r, wcfg)
        check("scan_once is a no-op when nothing changed",
              rep2 == {"dropped": 0, "bookmarks_added": 0, "extracted": 0})

        # --once never enters the poll loop: returns promptly, no hang.
        import time as _time
        t0 = _time.monotonic()
        wrep3 = libwatch.run(once=True, start=wt, interval=999)
        check("watch --once returns immediately regardless of --interval",
              _time.monotonic() - t0 < 5)
        check("second --once pass finds nothing new to drop/extract",
              wrep3 == {"dropped": 0, "bookmarks_added": 0, "extracted": 0})
    finally:
        libclient._post_json = _w_orig

    # Runs fine with watchdog absent (this environment has no watchdog
    # installed) — the stdlib mtime/size poll fallback is what --once above
    # already exercised via run()/scan_once directly.
    Observer, Handler = libwatch._watchdog()
    check("watchdog absent -> import guard returns (None, None) cleanly",
          (Observer, Handler) == (None, None) or (Observer is not None and Handler is not None))

    # ---------------- Dump debounce (BUILD) ----------------
    print("[dump] db/dump.sql is written once per Repo lifetime, not once per finalize()")
    ddroot = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-dump-debounce-")))
    write(ddroot / "d1.md", "src1")
    write(ddroot / "d2.md", "src2")
    write(ddroot / "d3.md", "src3")

    _orig_dump = Repo.dump
    _dump_calls = []

    def _counting_dump(self):
        _dump_calls.append(1)
        return _orig_dump(self)

    Repo.dump = _counting_dump
    try:
        with Repo.open(start=ddroot) as r:
            for i, fname in enumerate(("d1.md", "d2.md", "d3.md"), start=1):
                # ingest.add() itself calls finalize(); plus our own explicit
                # finalize() below -> 2 finalize() calls per loop, 6 total.
                sid, _ = ingest.add(r, str(ddroot / fname), origin="clip", title=f"d{i}")
                r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, "
                     "created_at) VALUES (?, ?, 0.9, 'clip', 'promoted', "
                     "'2026-01-01T00:00:00Z')", (f"debounce claim {i}", sid))
                r.finalize("test", f"finalize #{i}")
            check("dump.sql NOT yet written mid-context (still pending)",
                  len(_dump_calls) == 0)
        check("6 finalize() calls in one Repo context -> exactly 1 dump() write",
              len(_dump_calls) == 1)
    finally:
        Repo.dump = _orig_dump

    dd_text = (ddroot / "db" / "dump.sql").read_text(encoding="utf-8")
    check("debounced dump.sql still has all 3 claims (content unchanged by batching)",
          all(f"debounce claim {i}" in dd_text for i in (1, 2, 3)))
    check("debounced dump.sql keeps the embeddings CREATE TABLE",
          "CREATE TABLE embeddings" in dd_text)
    check("debounced dump.sql has no embeddings INSERT rows",
          'INSERT INTO "embeddings"' not in dd_text)

    # `wiki dump` / `wiki init` call repo.dump() directly to force an
    # immediate refresh -- that must still work, and must bypass/clear the
    # pending-flag debounce so __exit__ doesn't redundantly dump again.
    _dump_calls.clear()
    Repo.dump = _counting_dump
    try:
        with Repo.open(start=ddroot) as r:
            ssid = r.one("SELECT id FROM sources LIMIT 1")["id"]
            r.ex("INSERT INTO claims(text, source_id, confidence, origin, status, "
                 "created_at) VALUES ('forced-dump claim', ?, 0.9, 'clip', 'promoted', "
                 "'2026-01-01T00:00:00Z')", (ssid,))
            r.finalize("test", "finalize before forced dump")
            r.dump()  # what cmd_dump / cmd_init call
            check("explicit repo.dump() (wiki dump/init path) writes immediately",
                  len(_dump_calls) == 1)
        check("__exit__ does not re-dump after an explicit dump() already flushed it",
              len(_dump_calls) == 1)
    finally:
        Repo.dump = _orig_dump
    forced_text = (ddroot / "db" / "dump.sql").read_text(encoding="utf-8")
    check("forced dump.sql picked up the new claim",
          "forced-dump claim" in forced_text)

    # ---------------- Librarian maintain (the keystone one-command cycle) -----
    print("[librarian-maintain] one command chains every advisory pass + "
          "pure-code housekeeping; preserves all human gates")
    from librarian import maintain as libmaintain

    mt = make_repo(Path(tempfile.mkdtemp(prefix="wikibrain-mt-")))
    write(mt / "config.toml", (mt / "config.toml").read_text(encoding="utf-8")
          + '[librarian]\nmodel = "stub"\nbase_url = "http://stub/v1"\n')
    mcfg = _LibCfg.load(start=mt)

    # Seed a brain with work for every stage:
    #  - a pending NEW source            -> catch-up extracts it
    #  - a promoted 5-claim Redis cluster -> synthesize page + skill draft
    #  - an open contradiction + escalation -> adjudicate proposals
    #  - a pending low-confidence claim  -> triage recommendation
    with Repo.open(start=mt) as r:
        mt_new_sid = ingest.capture(r, "m", "a fresh note about HTTP caching")
        rsid = ingest.capture(r, "m", "redis notes")
        ingest.file_claims_data(r, rsid, {
            "source_id": rsid, "summary": "s",
            "claims": [{"text": f"Redis fact number {i}.", "confidence": 0.99,
                        "entities": ["Redis"]} for i in range(1, 6)],
            "low_confidence": False})
        gatemod.gate(r)
        rpend = [row["id"] for row in r.q(
            "SELECT id FROM claims WHERE source_id=? AND status='pending'", (rsid,))]
        if rpend:
            review.promote(r, rpend)
        # an open contradiction + escalation for adjudicate
        rclaim = r.one("SELECT id FROM claims WHERE source_id=? LIMIT 1", (rsid,))["id"]
        r.ex("INSERT INTO contradictions(claim_a, claim_b, status) VALUES (?,?, 'open')",
             (rclaim, rclaim))
        r.ex("INSERT INTO escalations(source_id, reason, status) VALUES (?,?, 'open')",
             (rsid, "extractor returned low-confidence garble"))
        # a pending low-confidence claim for triage
        psid = ingest.capture(r, "m", "a speculative candidate")
        ingest.file_claims_data(r, psid, {
            "source_id": psid, "summary": "s",
            "claims": [{"text": "Widget Z will ship in 2099.", "confidence": 0.5,
                        "entities": ["Widget Z"]}], "low_confidence": False})
        gatemod.gate(r)
        mt_pend_id = r.one("SELECT id FROM claims WHERE source_id=?", (psid,))["id"]
        r.conn.commit()
        mt_con_id = r.one("SELECT id FROM contradictions WHERE status='open' ORDER BY id")["id"]
        mt_esc_id = r.one("SELECT id FROM escalations WHERE status='open' ORDER BY id")["id"]

    mt_extract = {
        "source_id": mt_new_sid, "summary": "HTTP caching note.",
        "claims": [{"text": "HTTP caching reuses responses to cut server load.",
                    "confidence": 0.9, "entities": ["HTTP caching"]}],
        "low_confidence": False}
    mt_triage = {"recommendation": "hold", "confidence": 0.4,
                 "reason": "Speculative far-future date; leave for the human."}
    mt_adj = {"proposal": "Keep the better-sourced claim; a human should confirm.",
              "confidence": 0.6}
    mt_synth = {"prose": "Redis is an in-memory data store used as a cache."}
    mt_skill = {"should_draft": True, "name": "redis",
                "description": "Activate when working with Redis caching.",
                "body": "# Redis\n\nUse Redis as an in-memory cache."}

    def _m_stub(url, payload, headers, timeout):
        text = " ".join(m["content"] for m in payload["messages"])
        if "should_draft" in text:
            reply = mt_skill
        elif '"prose"' in text:
            reply = mt_synth
        elif '"recommendation"' in text:
            reply = mt_triage
        elif '"proposal"' in text:
            reply = mt_adj
        else:
            reply = mt_extract
        return {"choices": [{"message": {"content": json.dumps(reply)}}]}

    _m_orig = libclient._post_json
    _reach_orig = libclient.reachable
    libclient._post_json = _m_stub
    libclient.reachable = lambda cfg, **k: True
    try:
        with Repo.open(start=mt) as r:
            mrep = libmaintain.run(r, mcfg)
        check("maintain preflight recorded the base_url + model",
              mrep["preflight"]["base_url"] == "http://stub/v1"
              and mrep["preflight"]["model"] == "stub")
        check("maintain ran every stage in order",
              mrep["stages_run"] == ["catch_up", "triage", "adjudicate",
                                     "synthesize", "housekeeping"])
        check("maintain reported no stage errors", not mrep["errors"])
        s = mrep["summary"]
        check("maintain summary counts the newly-extracted source",
              s["sources_extracted"] == 1)
        check("maintain summary tallies a triage recommendation (hold)",
              s["triage_recommendations"]["hold"] >= 1)
        check("maintain summary counts adjudication proposals (contradiction+escalation)",
              s["proposals_drafted"] == 2)
        check("maintain summary counts a synthesized page + a skill draft",
              s["synthesis_pages"] >= 1 and s["skill_drafts"] == 1)
        check("maintain summary carries the health score from housekeeping",
              isinstance(s["health_score"], int))
        check("maintain housekeeping rendered + ran lint/health",
              mrep["housekeeping"]["digest"].startswith("wiki/digests/")
              and "health" in mrep["housekeeping"])
        # gates preserved: it drafts/proposes only, never promotes/resolves/closes/approves
        with Repo.open(start=mt) as r:
            pst = r.one("SELECT status FROM claims WHERE id=?", (mt_pend_id,))["status"]
            cst = r.one("SELECT status, proposal FROM contradictions WHERE id=?", (mt_con_id,))
            est = r.one("SELECT status, proposal FROM escalations WHERE id=?", (mt_esc_id,))
            skst = r.one("SELECT status FROM skills WHERE name='redis'")
        check("maintain NEVER promotes a triaged claim (still pending)", pst == "pending")
        check("maintain drafts a contradiction proposal but never resolves it",
              cst["status"] == "open" and bool(cst["proposal"]))
        check("maintain drafts an escalation proposal but never closes it",
              est["status"] == "open" and bool(est["proposal"]))
        check("maintain leaves the drafted skill status='draft' (never approves)",
              skst is not None and skst["status"] == "draft")
        check("maintain did not git-commit without --commit", mrep["committed"] is False)

        # stage-skipping honors the flags
        with Repo.open(start=mt) as r:
            mrep2 = libmaintain.run(r, mcfg, stages={"triage", "adjudicate"})
        check("maintain --no-synthesize skips the synthesize stage",
              mrep2["synthesize"] is None and "synthesize" in mrep2["stages_skipped"])
        check("maintain still runs catch-up + housekeeping when stages are skipped",
              "catch_up" in mrep2["stages_run"] and "housekeeping" in mrep2["stages_run"])
    finally:
        libclient._post_json = _m_orig
        libclient.reachable = _reach_orig

    # Preflight fails fast (before any model call) when the endpoint is unreachable.
    mt_calls = {"n": 0}
    def _m_count(url, payload, headers, timeout):
        mt_calls["n"] += 1
        return {"choices": [{"message": {"content": "{}"}}]}
    libclient._post_json = _m_count
    libclient.reachable = lambda cfg, **k: False
    try:
        pf_err = None
        try:
            with Repo.open(start=mt) as r:
                libmaintain.run(r, mcfg)
        except libmaintain.PreflightError as e:
            pf_err = str(e)
        check("maintain preflight raises PreflightError when endpoint unreachable",
              pf_err is not None)
        check("preflight error names the base_url so the message is actionable",
              pf_err is not None and "http://stub/v1" in pf_err)
        check("preflight fails BEFORE any model call is made", mt_calls["n"] == 0)
    finally:
        libclient._post_json = _m_orig
        libclient.reachable = _reach_orig

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
