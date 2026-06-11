"""Content -> markdown extraction for ingest (pure code, ZERO model calls).

Turns a dropped file into the markdown text that becomes its `raw/` artifact, so
a Claude session can later read it and emit claims. Dispatch by `kind`:

  text  -> read UTF-8 text/markdown as-is                (always available)
  doc   -> Docling: PDF/DOCX/PPTX/HTML -> markdown        ([docs] extra)
  image -> Tesseract OCR text + an image reference        ([docs] extra)
  video -> youtube-transcript-api / local Whisper         ([media]/[whisper] extras)

Every backend is LOCAL and key-free (no billable LLM call, no API key), so the
"zero model calls in the CLI" boundary holds. Visual/semantic *judgment* (what an
image means) still happens in the Claude session — here we only do mechanical
text extraction. Heavy backends are imported lazily and raise ExtractError with
an install hint when their extra is missing, so the core CLI runs without them.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path


class ExtractError(Exception):
    pass


# file suffix -> handler kind
KIND_BY_SUFFIX = {
    ".md": "text", ".markdown": "text", ".txt": "text", ".text": "text",
    ".pdf": "doc", ".docx": "doc", ".pptx": "doc", ".html": "doc", ".htm": "doc",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".gif": "image", ".bmp": "image", ".tif": "image", ".tiff": "image",
    ".mp4": "video", ".mkv": "video", ".webm": "video", ".mov": "video",
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio",
}


def kind_for(path: Path) -> str:
    """Route a file to a handler by suffix (unknown -> 'text')."""
    return KIND_BY_SUFFIX.get(Path(path).suffix.lower(), "text")


def mime_for(path: Path) -> str | None:
    return mimetypes.guess_type(str(path))[0]


def to_markdown(path: Path, *, kind: str | None = None) -> str:
    """Return markdown text for a file. Raises ExtractError if the needed
    backend/extra isn't available (caller leaves the file for a later run)."""
    path = Path(path)
    kind = kind or kind_for(path)
    if kind == "text":
        return path.read_text(encoding="utf-8", errors="replace")
    if kind == "doc":
        return _doc(path)
    if kind == "image":
        return _image(path)
    if kind in ("video", "audio"):
        return _media(path, kind)
    raise ExtractError(f"unsupported kind: {kind}")


# --- heavy backends (filled in by Tier 2: items 2 & 4) ----------------------
def _doc(path: Path) -> str:
    raise ExtractError(
        f"document extraction needs the [docs] extra: pip install '.[docs]' ({path.name})")


def _image(path: Path) -> str:
    raise ExtractError(
        f"image OCR needs the [docs] extra: pip install '.[docs]' ({path.name})")


def _media(path: Path, kind: str) -> str:
    raise ExtractError(
        f"{kind} transcription needs the [media]/[whisper] extra ({path.name})")
