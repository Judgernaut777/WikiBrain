"""URL fetching + extraction to clean markdown.

Two key-free backends, tried as a fallback chain:
  - jina:        GET https://r.jina.ai/<url> — renders JS, returns LLM-ready
                 markdown. No API key. The default.
  - trafilatura: local readability extraction. No network service, good offline
                 fallback. Imported lazily so file/capture ingest still works in
                 environments without the dependency.

Network fetching is allowed in the CLI (it's data retrieval, not a model call).
No API keys are used by either backend, preserving the project's key-free boundary.
"""
from __future__ import annotations

import re
import urllib.request

UA = "Mozilla/5.0 (wiki-brain)"
DEFAULT_BACKEND = "jina"
DEFAULT_JINA_BASE = "https://r.jina.ai/"


class FetchError(Exception):
    pass


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def fetch_url(url: str, timeout: int = 30, *, backend: str | None = None,
              jina_base: str | None = None) -> tuple[str, str | None]:
    """Return (markdown_text, title). Tries the chosen backend then falls back.

    backend: "jina" | "trafilatura" | None (-> DEFAULT_BACKEND).
    Raises FetchError only if every backend fails.
    """
    backend = (backend or DEFAULT_BACKEND).lower()
    jina_base = jina_base or DEFAULT_JINA_BASE
    chain = {
        "jina": (_jina, _trafilatura),
        "trafilatura": (_trafilatura, _jina),
    }.get(backend, (_jina, _trafilatura))
    errors = []
    for fn in chain:
        try:
            return fn(url, timeout, jina_base=jina_base)
        except FetchError as e:
            errors.append(str(e))
    raise FetchError(f"all fetch backends failed for {url}: " + " | ".join(errors))


def _jina(url: str, timeout: int = 30, *, jina_base: str = DEFAULT_JINA_BASE,
          **_) -> tuple[str, str | None]:
    target = jina_base.rstrip("/") + "/" + url
    req = urllib.request.Request(
        target, headers={"User-Agent": UA, "Accept": "text/markdown"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            md = resp.read().decode("utf-8", "ignore")
    except Exception as e:  # network/HTTP error -> let the chain fall back
        raise FetchError(f"jina reader failed for {url}: {e}") from e
    if not md or not md.strip():
        raise FetchError(f"jina returned no content for {url}")
    # Jina prepends "Title: ...\nURL Source: ...\nMarkdown Content:\n".
    title = None
    m = re.match(r"Title:\s*(.+)", md)
    if m:
        title = m.group(1).strip()
    return md, title


def _trafilatura(url: str, timeout: int = 30, **_) -> tuple[str, str | None]:
    try:
        import trafilatura  # type: ignore
    except ImportError as e:  # pragma: no cover - dep missing
        raise FetchError(
            "trafilatura is not installed (needed to fetch/convert URLs). "
            "Install it: pip install trafilatura"
        ) from e

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise FetchError(f"could not download: {url}")
    md = trafilatura.extract(
        downloaded,
        output_format="markdown",
        include_links=True,
        include_images=True,
        with_metadata=False,
    )
    if not md or not md.strip():
        raise FetchError(f"no extractable content at: {url}")
    title = None
    try:
        meta = trafilatura.extract_metadata(downloaded)
        if meta and getattr(meta, "title", None):
            title = meta.title
    except Exception:
        title = None
    return md, title
