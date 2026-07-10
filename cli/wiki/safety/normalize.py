"""Text normalization, run before any rule sees the text.

An attacker who can insert a zero-width space into `ignore all previous
instructions` defeats a regex without changing what a model reads. Homoglyphs do
the same across scripts: Cyrillic `о` renders as Latin `o`. Normalizing first is
what makes the deterministic rules worth having at all.

Ported from fascia-guard's `normalize.py` (same owner, no third-party code). Pure
stdlib.

**This changes offsets.** Spans reported by an engine index into the *normalized*
text, which is why `pipeline` redacts the normalized text and hands that back
rather than trying to map masks onto the original.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

_ZERO_WIDTH = frozenset(
    "​"    # zero-width space
    "‌"    # zero-width non-joiner
    "‍"    # zero-width joiner
    "⁠"    # word joiner
    "﻿"    # zero-width no-break space / BOM
    "­"    # soft hyphen
    "᠎"    # mongolian vowel separator
)

_BIDI_CONTROLS = frozenset(chr(c) for c in (
    0x202a, 0x202b, 0x202c, 0x202d, 0x202e,   # LRE RLE PDF LRO RLO
    0x2066, 0x2067, 0x2068, 0x2069,           # LRI RLI FSI PDI
))

#: Confusables that render as ASCII. Not exhaustive — the full Unicode confusables
#: table is thousands of entries — but it covers the Cyrillic and Greek lookalikes
#: that appear in practice.
_CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "х": "x", "у": "y", "і": "i", "ԁ": "d", "ԛ": "q",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H",
    "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "Ѕ": "S", "А": "A", "В": "B", "Е": "E", "К": "K",
    "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "Х": "X",
}


@dataclass
class Normalized:
    original: str
    text: str
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.text != self.original


def normalize(text: str) -> Normalized:
    """Strip invisible controls, fold homoglyphs, then NFKC."""
    notes: list[str] = []

    stripped = 0
    buf = []
    for ch in text:
        if ch in _ZERO_WIDTH or ch in _BIDI_CONTROLS:
            stripped += 1
            continue
        buf.append(ch)
    s = "".join(buf)
    if stripped:
        notes.append(f"stripped {stripped} zero-width/bidi control character(s)")

    folded = 0
    out = []
    for ch in s:
        repl = _CONFUSABLES.get(ch)
        if repl is None:
            out.append(ch)
        else:
            out.append(repl)
            folded += 1
    s = "".join(out)
    if folded:
        notes.append(f"folded {folded} homoglyph(s) to ASCII")

    nfkc = unicodedata.normalize("NFKC", s)
    if nfkc != s:
        notes.append("applied NFKC compatibility normalization")

    return Normalized(original=text, text=nfkc, notes=notes)
