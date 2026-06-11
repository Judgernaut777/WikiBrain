"""Phase 4 gather tooling: bookmarks sync, fetch, websearch, budgets.

All network access lives here (inside `wiki` commands), never in a model's own
tools. Budgets are enforced in code via the gather_events ledger.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

from .db import Repo
from . import fetch as fetchmod
from . import ingest, util

UA = "wiki-brain/1.0 (+https://localhost)"


# --- budgets ----------------------------------------------------------------
def _count(repo: Repo, kind: str, qid: int | None = None) -> int:
    day = util.today_local()
    if qid is None:
        row = repo.one("SELECT COUNT(*) n FROM gather_events WHERE day=? AND kind=?",
                       (day, kind))
    else:
        row = repo.one(
            "SELECT COUNT(*) n FROM gather_events WHERE day=? AND kind=? AND qid=?",
            (day, kind, qid))
    return row["n"]


def _record(repo: Repo, kind: str, qid: int | None):
    repo.ex("INSERT INTO gather_events(day, kind, qid, created_at) VALUES (?,?,?,?)",
            (util.today_local(), kind, qid, util.now_iso()))


def budget_status(repo: Repo) -> dict:
    return {
        "queries_today": _count(repo, "query"),
        "fetches_today": _count(repo, "fetch"),
        "questions_per_night": repo.cfg.budget("questions_per_night"),
        "fetches_per_night": repo.cfg.budget("fetches_per_night"),
        "queries_per_question": repo.cfg.budget("queries_per_question"),
        "fetches_per_question": repo.cfg.budget("fetches_per_question"),
    }


class BudgetError(Exception):
    pass


def _check_query_budget(repo: Repo, qid: int | None):
    if qid is not None and _count(repo, "query", qid) >= repo.cfg.budget("queries_per_question"):
        raise BudgetError(f"per-question query budget reached for q#{qid}")


def _check_fetch_budget(repo: Repo, qid: int | None):
    if _count(repo, "fetch") >= repo.cfg.budget("fetches_per_night"):
        raise BudgetError("per-night fetch budget reached")
    if qid is not None and _count(repo, "fetch", qid) >= repo.cfg.budget("fetches_per_question"):
        raise BudgetError(f"per-question fetch budget reached for q#{qid}")


# --- websearch --------------------------------------------------------------
def websearch(repo: Repo, query: str, qid: int | None = None) -> list[dict]:
    _check_query_budget(repo, qid)
    engine = (repo.cfg.search_cfg("engine") or "ddg").lower()
    if engine == "searxng":
        results = _searxng(repo.cfg.search_cfg("searxng_url"), query)
    else:
        results = _ddg(query)
    _record(repo, "query", qid)
    repo.finalize("websearch", f"{engine}: {query[:50]} ({len(results)} hits)")
    return results


def _searxng(base_url: str, query: str) -> list[dict]:
    url = base_url.rstrip("/") + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for r in data.get("results", []):
        out.append({"title": r.get("title"), "url": r.get("url"),
                    "snippet": r.get("content")})
    return out


def _ddg(query: str) -> list[dict]:
    """DuckDuckGo via the maintained `ddgs` library (no key); on any failure
    fall back to the dependency-free HTML scrape so search still degrades."""
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        return _ddg_scrape(query)
    try:
        with DDGS() as d:
            rows = list(d.text(query, max_results=10))
        if rows:
            return [{"title": r.get("title"), "url": r.get("href"),
                     "snippet": r.get("body", "")} for r in rows]
        return _ddg_scrape(query)
    except Exception:
        return _ddg_scrape(query)


def _ddg_scrape(query: str) -> list[dict]:
    import re
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "ignore")
    out = []
    for m in re.finditer(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, title = m.group(1), re.sub("<[^>]+>", "", m.group(2)).strip()
        if href.startswith("//duckduckgo.com/l/"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            href = qs.get("uddg", [href])[0]
        out.append({"title": title, "url": href, "snippet": ""})
    return out


# --- fetch for a queue question ---------------------------------------------
def fetch_for(repo: Repo, url: str, qid: int) -> int:
    if not repo.one("SELECT 1 FROM research_queue WHERE id=?", (qid,)):
        raise SystemExit(f"error: no queue item #{qid}")
    _check_fetch_budget(repo, qid)
    md, title = fetchmod.fetch_url(
        url, backend=repo.cfg.search_cfg("fetch_backend"),
        jina_base=repo.cfg.search_cfg("jina_base"))
    content = md.encode("utf-8")
    h8 = util.sha256_bytes(content)[:8]
    dest = repo.root / "raw" / f"{util.slug(title or url)}-{h8}.md"
    dest.write_text(md, encoding="utf-8")
    rel = repo.rel(dest)
    sid = ingest._register_source(
        repo, content=content, rel_path=rel, title=title, url=url,
        origin="autoresearch", fetched_at=util.now_iso())
    _record(repo, "fetch", qid)
    repo.finalize("fetch", f"source #{sid} autoresearch for q#{qid}: {url}")
    return sid


# --- bookmarks sync ---------------------------------------------------------
def _chrome_urls(path: Path, folder: str) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []

    def walk(node, inside):
        if node.get("type") == "folder":
            inside = inside or (node.get("name") == folder)
            for ch in node.get("children", []):
                walk(ch, inside)
        elif node.get("type") == "url" and inside:
            found.append((node.get("url"), node.get("name") or ""))

    for root in data.get("roots", {}).values():
        if isinstance(root, dict):
            walk(root, False)
    return found


def _firefox_urls(path: Path, folder: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    uri = f"file:{path}?immutable=1&mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        fid = con.execute(
            "SELECT id FROM moz_bookmarks WHERE title=? AND type=2", (folder,)).fetchone()
        if not fid:
            return out
        rows = con.execute(
            """SELECT p.url, b.title FROM moz_bookmarks b
               JOIN moz_places p ON p.id=b.fk WHERE b.parent=?""", (fid[0],)).fetchall()
        out = [(r[0], r[1] or "") for r in rows]
    finally:
        con.close()
    return out


def _register_failed(repo: Repo, url: str, origin: str):
    content = ("FETCH_FAILED:" + url).encode("utf-8")
    h = util.sha256_bytes(content)
    if repo.one("SELECT 1 FROM sources WHERE hash=?", (h,)):
        return
    repo.ex(
        """INSERT INTO sources(hash, path, title, url, origin, fetched_at,
                               ingested_at, status)
           VALUES (?,?,?,?,?,?,?, 'failed')""",
        (h, "", url, url, origin, util.now_iso(), util.now_iso()))


def bookmarks_sync(repo: Repo) -> dict:
    folder = repo.cfg.bookmark_folder
    known = {r["url"] for r in repo.q("SELECT url FROM sources WHERE url IS NOT NULL")}
    candidates: list[tuple[str, str]] = []
    for bm in repo.cfg.bookmark_files:
        if not bm.exists():
            continue
        try:
            if bm.name.lower() == "bookmarks":  # Chrome JSON
                candidates += _chrome_urls(bm, folder)
            elif bm.suffix == ".sqlite":         # Firefox places.sqlite
                candidates += _firefox_urls(bm, folder)
        except Exception as e:  # noqa
            repo.log("bookmarks-sync", f"warning reading {bm.name}: {e}")

    added, failed, skipped = [], [], 0
    for url, _name in candidates:
        if not url or url in known:
            skipped += 1
            continue
        known.add(url)
        try:
            sid, _ = ingest.add(repo, url, origin="bookmark")
            added.append((sid, url))
        except fetchmod.FetchError:
            _register_failed(repo, url, "bookmark")
            failed.append(url)
        except ingest.IngestError:
            skipped += 1
    repo.finalize("bookmarks-sync",
                  f"+{len(added)} added, {len(failed)} failed, {skipped} skipped")
    return {"added": added, "failed": failed, "skipped": skipped}
