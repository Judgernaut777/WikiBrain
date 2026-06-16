"""`wiki drop`: ingest files a user drops into a watch folder.

Globs config `[paths].drop_folder`, converts each file to a markdown `raw/`
artifact via `extract.to_markdown`, and registers it as a pending source
(origin "drop", with `mime_type`). On success the original is archived to
`<drop>/.processed/` so re-runs are idempotent. Files whose extractor/extra
isn't installed are left in place (with a warning) for a later run. Pure code,
ZERO model calls — the dropped content is read and turned into claims later, by
a Claude session.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .db import Repo
from . import ingest, extract, util

PROCESSED = ".processed"


def scan(repo: Repo, *, move: bool = True) -> list[dict]:
    """Process every file in the drop folder. Returns one result dict per file:
    {file, kind, mime_type, source_id|None, warning|None}."""
    folder = repo.cfg.drop_folder
    results: list[dict] = []
    if not folder or not folder.exists():
        return results
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

    for path in sorted(folder.iterdir()):
        if path.is_dir():
            continue  # skip subdirs, including .processed
        kind = extract.kind_for(path)
        mime = extract.mime_for(path)
        entry = {"file": path.name, "kind": kind, "mime_type": mime,
                 "source_id": None, "warning": None}
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
                url=None, origin="drop", fetched_at=None, mime_type=mime)
        except ingest.IngestError as e:
            entry["warning"] = str(e)  # exact duplicate -> already known
            _archive(path, h8)         # still get it out of the inbox
            results.append(entry)
            continue
        entry["source_id"] = sid
        _archive(path, h8)
        results.append(entry)

    ingested = [e for e in results if e["source_id"]]
    if ingested:
        repo.finalize("drop", f"ingested {len(ingested)} file(s) from drop folder")
    return results
