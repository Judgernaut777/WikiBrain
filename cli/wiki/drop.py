"""`wiki drop`: ingest files from the configured ingestion folders.

Scans every `[[paths.sources]]` folder (plus the legacy single `drop_folder`),
converts each file to a markdown `raw/` artifact via `extract.to_markdown`, and
registers it as a pending source (per-folder `origin`, with `mime_type`). By
default originals are LEFT IN PLACE — global content-hash dedup (`sources.hash`)
makes re-scanning harmless; a source with `move=true` (and the legacy
`drop_folder`) archives the original to `<folder>/.processed/` instead. Files
whose extractor/extra isn't installed are left in place (with a warning) for a
later run. Pure code, ZERO model calls — the dropped content is read and turned
into claims later, by the librarian or a Claude session.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from .db import Repo
from .config import IngestSource
from . import ingest, extract, util

PROCESSED = ".processed"


def _included(name: str, include: tuple[str, ...]) -> bool:
    """True if `name` passes the source's include globs (empty = all files)."""
    return not include or any(fnmatch.fnmatch(name, g) for g in include)


def iter_source_files(src: IngestSource) -> Iterator[Path]:
    """Yield the files to ingest from one source, honoring `recursive` and the
    `include` globs. Skips `.processed/` and hidden dirs, and never follows
    symlinks (no infinite loops). Shared with the watcher so what it fingerprints
    and what `scan` ingests can't drift."""
    folder = src.path
    if not folder.exists():
        return
    if src.recursive:
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if d != PROCESSED and not d.startswith("."))
            for name in sorted(filenames):
                if _included(name, src.include):
                    yield Path(dirpath) / name
    else:
        for p in sorted(folder.iterdir()):
            if p.is_dir():  # skip subdirs, including .processed/
                continue
            if _included(p.name, src.include):
                yield p


def _scan_folder(repo: Repo, src: IngestSource, *,
                 move_override: bool | None = None) -> list[dict]:
    """Ingest one source folder. Returns one result dict per file:
    {file, source, origin, kind, mime_type, source_id|None, warning|None}."""
    folder = src.path
    results: list[dict] = []
    if not folder.exists():
        return results
    move = src.move if move_override is None else move_override
    processed_dir = folder / PROCESSED
    tess = repo.cfg.extract_cfg("tesseract_cmd") or None
    assets = repo.root / "raw" / "assets"

    def _archive(path: Path, h8: str | None) -> None:
        if not move:
            return
        processed_dir.mkdir(parents=True, exist_ok=True)
        target = processed_dir / path.name
        if target.exists():
            suffix = f"-{h8}" if h8 else "-dup"
            target = processed_dir / f"{path.stem}{suffix}{path.suffix}"
        shutil.move(str(path), str(target))

    for path in iter_source_files(src):
        kind = extract.kind_for(path)
        mime = extract.mime_for(path)
        entry = {"file": path.name, "source": str(src.path), "origin": src.origin,
                 "kind": kind, "mime_type": mime, "source_id": None, "warning": None}
        try:
            md = extract.to_markdown(path, kind=kind, tesseract_cmd=tess)
        except extract.ExtractError as e:
            entry["warning"] = str(e)  # extractor/extra missing -> leave in place
            results.append(entry)
            continue
        # For images, keep the binary under raw/assets/ so a session can view it,
        # and link it from the raw artifact.
        if kind == "image":
            assets.mkdir(parents=True, exist_ok=True)
            ah8 = util.sha256_bytes(path.read_bytes())[:8]
            asset = assets / f"{util.slug(path.stem)}-{ah8}{path.suffix.lower()}"
            shutil.copyfile(path, asset)
            md = (f"_image file: `{repo.rel(asset)}` — view it to describe and "
                  f"label. Binary evidence stays under `raw/assets/` even if this "
                  f"wrapper is filed into `raw/images/<year>/`._\n\n" + md)
        content = md.encode("utf-8")
        h8 = util.sha256_bytes(content)[:8]
        dest = repo.root / "raw" / f"{util.slug(path.stem)}-{h8}.md"
        # write_bytes, not write_text: keep on-disk bytes == the hashed content
        # (Windows write_text emits CRLF and would break sources.hash).
        dest.write_bytes(content)
        try:
            sid = ingest._register_source(
                repo, content=content, rel_path=repo.rel(dest), title=path.stem,
                url=None, origin=src.origin, fetched_at=None, mime_type=mime)
        except ingest.IngestError as e:
            entry["warning"] = str(e)  # exact duplicate -> already known
            _archive(path, h8)         # still get it out of the inbox (if move)
            results.append(entry)
            continue
        entry["source_id"] = sid
        _archive(path, h8)
        results.append(entry)
    return results


def scan(repo: Repo, *, move: bool | None = None) -> list[dict]:
    """Process every configured ingestion folder (`[[paths.sources]]` + the
    legacy `drop_folder`). `move=None` (default) lets each source use its own
    `move` setting; `move=False`/`True` overrides all of them (the `--no-move`
    flag passes False, so it means "don't touch my files this run"). Returns one
    result dict per file across all folders."""
    results: list[dict] = []
    for src in repo.cfg.ingest_sources:
        results += _scan_folder(repo, src, move_override=move)
    ingested = [e for e in results if e["source_id"]]
    if ingested:
        n_src = len({e["source"] for e in ingested})
        repo.finalize("drop", f"ingested {len(ingested)} file(s) from {n_src} source(s)")
    return results
