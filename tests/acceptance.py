"""Offline acceptance harness for wiki-brain (phases 1-5).

Runs the package API against a throwaway temp repo + temp DB, so it never
touches the live database. No pytest dependency — run directly:

    .venv/Scripts/python.exe tests/acceptance.py

Network-dependent paths (URL fetch, websearch, live bookmark fetch) are NOT
exercised here; their logic is unit-tested where possible (bookmark parser,
budget ledger). Exits non-zero on first failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Make the package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

from wiki.db import Repo, init_db          # noqa: E402
from wiki import (ingest, search as searchmod, queue as queuemod,            # noqa: E402
                  render as rendermod, lint as lintmod, health as healthmod,
                  review, gate as gatemod, gather, fetch as fetchmod,
                  migrate as migratemod, schema as schemamod, drop as dropmod,
                  skills as skillsmod)

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
    # Build an old-shape (v1) sources table lacking the new columns.
    old_db = Path(tempfile.mkdtemp(prefix="wikibrain-mig-")) / "old.db"
    c = _sqlite.connect(str(old_db))
    c.executescript(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL, "
        "path TEXT NOT NULL, title TEXT, url TEXT, origin TEXT NOT NULL, "
        "fetched_at TEXT, ingested_at TEXT, status TEXT NOT NULL DEFAULT 'new');")
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
    check("existing row gets default tags='[]'",
          c.execute("SELECT tags FROM sources WHERE hash='h1'").fetchone()[0] == "[]")
    migratemod.migrate(c)  # idempotent re-run
    check("migrate is idempotent",
          c.execute("PRAGMA user_version").fetchone()[0] == ver)
    c.close()
    # Fresh init_db DBs are already at latest -> migrate is a no-op there.
    with Repo.open(start=root) as r:
        fresh_cols = {row[1] for row in r.conn.execute("PRAGMA table_info(sources)")}
    check("fresh install already has new columns", {"mime_type", "category", "tags"} <= fresh_cols)

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

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
