"""Pure helpers: hashing, slugs, timestamps, FTS query building."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def now_iso() -> str:
    """UTC timestamp, second precision, ISO-8601 with Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_local_compact() -> str:
    """Local 'YYYY-MM-DD HH:MM' for the operations log header."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_slug_strip = re.compile(r"[^a-z0-9]+")


def slug(text: str, maxlen: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _slug_strip.sub("-", text).strip("-")
    if len(text) > maxlen:
        text = text[:maxlen].rstrip("-")
    return text or "untitled"


_word = re.compile(r"[A-Za-z0-9]+")

# Negation tokens used by the contradiction polarity heuristic.
NEGATION_TOKENS = {
    "not", "no", "never", "cannot", "cant", "wont", "without", "fails",
    "failed", "unsupported", "incompatible", "false", "lacks", "missing",
    "doesnt", "isnt", "arent", "dont", "none", "neither", "nor",
}


def tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _word.finditer(text)]


STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "for", "and", "or", "with", "as", "at", "by",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def fts_query(terms: str) -> str:
    """Build a safe FTS5 MATCH expression (AND) from free user input.

    Each alphanumeric token becomes a quoted term ANDed together. This avoids
    accidental FTS operator injection. Used for `wiki search` (high precision).
    """
    toks = _word.findall(terms)
    if not toks:
        # Match nothing rather than erroring on punctuation-only input.
        return '"' + terms.replace('"', "") + '"' if terms.strip() else '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def significant_tokens(text: str) -> list[str]:
    """Content tokens with stopwords AND negation tokens dropped."""
    return [t for t in tokens(text)
            if len(t) > 1 and t not in STOPWORDS and t not in NEGATION_TOKENS]


def fts_or_query(text: str) -> str:
    """OR of significant tokens — high recall, for similarity/contradiction
    candidate retrieval. Negation tokens are dropped so 'X is not Y' still
    retrieves 'X is Y' as a candidate (polarity is then checked separately).
    Returns '""' (matches nothing) when there are no significant tokens.
    """
    toks = significant_tokens(text)
    if not toks:
        return '""'
    return " OR ".join('"' + t + '"' for t in toks)


def has_negation(text: str) -> bool:
    return any(t in NEGATION_TOKENS or t.endswith("nt") for t in tokens(text))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
