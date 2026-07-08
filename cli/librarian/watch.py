"""`wiki-librarian watch`: an event-driven loop over the two inputs that don't
pass through any `wiki` command — the drop folder and the browser bookmark
files. On a detected change it runs the matching pure-code ingest
(`wiki.drop.scan` / `wiki.gather.bookmarks_sync`) and then the librarian
extraction pass (`librarian.extract.catch_up`) over whatever landed, so a
dropped PDF or a freshly-bookmarked URL flows all the way to gated + rendered
without a human running commands by hand.

No model call happens in this module's own code — it only delegates to
`extract.catch_up`, the model half that already exists. Uses the `watchdog`
library (the `[watch]` extra) IF installed, for responsive filesystem events;
falls back to a dependency-free stdlib mtime/size poll otherwise (mirrors the
import-guard pattern in wiki/embed.py and wiki/mcp_server.py).
"""
from __future__ import annotations

import time
from pathlib import Path

from wiki.db import Repo
from wiki.config import Config
from wiki import drop as dropmod
from wiki import gather as gathermod

from . import extract as extractmod
from .config import LibrarianConfig


def _watchdog():
    """Import-guarded like embed.py / mcp_server.py: returns (Observer,
    FileSystemEventHandler) or (None, None) if the [watch] extra isn't
    installed."""
    try:
        from watchdog.observers import Observer  # type: ignore
        from watchdog.events import FileSystemEventHandler  # type: ignore
        return Observer, FileSystemEventHandler
    except ImportError:
        return None, None


def scan_once(repo: Repo, cfg: LibrarianConfig) -> dict:
    """One detect-and-react pass: ingest the drop folder + sync bookmarks, then
    (only if something landed) run the librarian extraction catch-up over the
    new backlog. Idempotent — safe to call on every poll tick even when
    nothing changed, since drop.scan/bookmarks_sync/catch_up are each
    idempotent on their own."""
    dropped_results = dropmod.scan(repo)
    dropped = len([e for e in dropped_results if e["source_id"]])
    bm = gathermod.bookmarks_sync(repo)
    bookmarks_added = len(bm["added"])
    extracted = 0
    if dropped or bookmarks_added:
        cu = extractmod.catch_up(repo, cfg)
        extracted = len(cu["processed"])
    return {"dropped": dropped, "bookmarks_added": bookmarks_added, "extracted": extracted}


def _signature(wiki_cfg: Config) -> dict:
    """Cheap stdlib change fingerprint over the watched inputs: a map of
    "src:<path>" / "bm:<path>" -> (mtime_ns, size). Used by the poll fallback to
    decide whether a scan is worth doing this tick. Enumerates each configured
    ingestion source exactly as `drop.scan` does (`iter_source_files` honors
    recursive + include and skips .processed/hidden), so the watcher fingerprints
    precisely the files that would be ingested."""
    sig: dict[str, tuple[int, int]] = {}
    for src in wiki_cfg.ingest_sources:
        for p in dropmod.iter_source_files(src):
            try:
                st = p.stat()
            except OSError:
                continue
            sig[f"src:{p}"] = (st.st_mtime_ns, st.st_size)
    for bm in wiki_cfg.bookmark_files:
        try:
            st = bm.stat()
        except OSError:
            continue
        sig[f"bm:{bm}"] = (st.st_mtime_ns, st.st_size)
    return sig


def _watched_dirs(wiki_cfg: Config) -> list[tuple[Path, bool]]:
    """The directories to observe, each with a recursive flag: every configured
    ingestion source (recursive per its setting) plus each bookmark file's parent
    (non-recursive). Deduped preferring recursive=True."""
    dirs: dict[Path, bool] = {}
    for src in wiki_cfg.ingest_sources:
        if src.path.exists():
            dirs[src.path] = dirs.get(src.path, False) or src.recursive
    for bm in wiki_cfg.bookmark_files:
        if bm.parent.exists():
            dirs.setdefault(bm.parent, False)
    return list(dirs.items())


def run(*, interval: int = 5, once: bool = False, start: Path | None = None) -> dict:
    """The watch loop. `once=True` does a single scan_once pass and returns —
    essential for tests and scripted runs, since it never blocks. Otherwise
    runs until KeyboardInterrupt (Ctrl-C), which exits cleanly.

    Opens a fresh Repo for each pass rather than holding one open for the
    process lifetime, so a long-running watcher never sits on a stale WAL
    snapshot across ticks. The loop itself only handles timing + change
    detection; the reaction is scan_once (above)."""
    lcfg = LibrarianConfig.load(start=start)

    if once:
        with Repo.open(start=start) as repo:
            return scan_once(repo, lcfg)

    Observer, Handler = _watchdog()

    if Observer is not None:
        changed = {"flag": True}  # force an initial pass

        class _Handler(Handler):
            def on_any_event(self, event):
                changed["flag"] = True

        with Repo.open(start=start) as repo:
            watch_dirs = _watched_dirs(repo.cfg)
        observer = Observer()
        for d, rec in watch_dirs:
            observer.schedule(_Handler(), str(d), recursive=rec)
        observer.start()
        try:
            while True:
                if changed["flag"]:
                    changed["flag"] = False
                    with Repo.open(start=start) as repo:
                        scan_once(repo, lcfg)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            observer.join()
        return {"stopped": True}

    # Dependency-free stdlib fallback: mtime/size polling.
    last_sig: dict | None = None
    try:
        while True:
            with Repo.open(start=start) as repo:
                sig = _signature(repo.cfg)
                if sig != last_sig:
                    last_sig = sig
                    scan_once(repo, lcfg)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    return {"stopped": True}
