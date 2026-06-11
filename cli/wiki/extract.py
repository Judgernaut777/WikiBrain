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


def to_markdown(path: Path, *, kind: str | None = None,
                tesseract_cmd: str | None = None) -> str:
    """Return markdown text for a file. Raises ExtractError if the needed
    backend/extra isn't available (caller leaves the file for a later run)."""
    path = Path(path)
    kind = kind or kind_for(path)
    if kind == "text":
        return path.read_text(encoding="utf-8", errors="replace")
    if kind == "doc":
        return _doc(path)
    if kind == "image":
        return _image(path, tesseract_cmd=tesseract_cmd)
    if kind in ("video", "audio"):
        return _media(path, kind)
    raise ExtractError(f"unsupported kind: {kind}")


# --- heavy backends (optional extras; import-guarded) -----------------------
def _doc(path: Path) -> str:
    """PDF/DOCX/PPTX/HTML -> markdown via Docling (has built-in OCR for scans)."""
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except ImportError as e:
        raise ExtractError(
            f"document extraction needs the [docs] extra: "
            f"pip install '.[docs]' ({path.name})") from e
    try:
        result = DocumentConverter().convert(str(path))
        md = result.document.export_to_markdown()
    except Exception as e:
        raise ExtractError(f"docling failed on {path.name}: {e}") from e
    if not md or not md.strip():
        raise ExtractError(
            f"docling produced no text for {path.name} (scanned without OCR?)")
    return md


def _image(path: Path, tesseract_cmd: str | None = None) -> str:
    """Flat image -> OCR text via Tesseract. The result is a stub; the Claude
    session also *views* the image (see gather.md) to describe + label it."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as e:
        raise ExtractError(
            f"image OCR needs the [docs] extra: pip install '.[docs]' ({path.name})") from e
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        text = (pytesseract.image_to_string(Image.open(path)) or "").strip()
    except Exception as e:
        raise ExtractError(
            f"tesseract OCR failed on {path.name}: {e} "
            f"(is the tesseract binary installed / on PATH?)") from e
    body = f"OCR text:\n\n{text}\n" if text else \
        "_No machine-readable text; view the image directly._\n"
    return f"# image: {path.name}\n\n{body}"


def _media(path: Path, kind: str) -> str:
    # Local audio/video files go through Whisper (the [whisper] extra). Drop-folder
    # video files route here; YouTube URLs go through `transcribe()` below.
    return _whisper(path)


# --- transcripts (item 4) ---------------------------------------------------
def transcribe(target: str, *, whisper_model: str = "base") -> tuple[str, str]:
    """Return (markdown_transcript, title) for a YouTube URL or a local
    audio/video file. Key-free: captions via youtube-transcript-api, or local
    Whisper ASR. No frame analysis."""
    if target.startswith("http://") or target.startswith("https://"):
        return _youtube(target)
    return _whisper(Path(target), whisper_model=whisper_model)


def _yt_id(url: str) -> str | None:
    import urllib.parse as up
    u = up.urlparse(url)
    if u.hostname and "youtu.be" in u.hostname:
        return u.path.lstrip("/").split("/")[0] or None
    if u.hostname and "youtube.com" in u.hostname:
        qs = up.parse_qs(u.query)
        if "v" in qs:
            return qs["v"][0]
        parts = [p for p in u.path.split("/") if p]
        if parts and parts[0] in ("embed", "shorts", "live"):
            return parts[1] if len(parts) > 1 else None
    return None


def _youtube(url: str) -> tuple[str, str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError as e:
        raise ExtractError(
            "video transcripts need the [media] extra: pip install '.[media]'") from e
    vid = _yt_id(url)
    if not vid:
        raise ExtractError(f"could not parse a YouTube video id from {url}")
    try:
        chunks = YouTubeTranscriptApi.get_transcript(vid)
    except Exception as e:
        raise ExtractError(f"no transcript available for {url}: {e}") from e
    text = " ".join(c["text"] for c in chunks).strip()
    if not text:
        raise ExtractError(f"empty transcript for {url}")
    return f"# transcript: {url}\n\n{text}\n", f"transcript {vid}"


def _whisper(path: Path, whisper_model: str = "base") -> tuple[str, str]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise ExtractError(
            f"local audio/video transcription needs the [whisper] extra (and ffmpeg "
            f"on PATH): pip install '.[whisper]' ({Path(path).name})") from e
    try:
        model = WhisperModel(whisper_model)
        segments, _info = model.transcribe(str(path))
        text = " ".join(seg.text for seg in segments).strip()
    except Exception as e:
        raise ExtractError(f"whisper failed on {Path(path).name}: {e}") from e
    if not text:
        raise ExtractError(f"empty transcription for {Path(path).name}")
    return f"# transcript: {Path(path).name}\n\n{text}\n", f"transcript {Path(path).stem}"
