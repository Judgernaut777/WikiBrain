"""Span masking.

Two engines will find the same secret and disagree about where it starts — the
baseline's `generic_assignment` rule matches `api_key = "ghp_…"` including the
assignment, while detect-secrets matches only the token. Masking each span in turn
would work by accident here (the mask is length-preserving) but not in general, so
overlapping spans are merged into one before anything is masked.

The mask preserves length so that any span computed against the same text remains
valid after masking. That is a property worth keeping: it means redaction is
order-independent.

`merge_spans` is ported from mcp-agentconnect's `safety/redaction.py` (same owner,
MIT). Pure stdlib.
"""
from __future__ import annotations

from .models import Finding

#: U+2588 FULL BLOCK. One char in, one char out.
MASK = "█"


def merge_spans(findings: list[Finding]) -> list[tuple[int, int]]:
    """Non-overlapping `[start, end)` spans covering every span in `findings`.

    Findings without a span contribute nothing: a whole-text classifier score
    tells you the text is risky, not which characters to hide.
    """
    ordered = sorted((f for f in findings if f.has_span),
                     key=lambda f: (f.start, -f.end))
    merged: list[tuple[int, int]] = []
    for f in ordered:
        if merged and f.start < merged[-1][1]:
            start, end = merged[-1]
            merged[-1] = (start, max(end, f.end))
        else:
            merged.append((f.start, f.end))
    return merged


def redact(text: str, findings: list[Finding]) -> str:
    """Mask every span in `findings`. Returns `text` unchanged when there are none."""
    spans = merge_spans(findings)
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        start = max(0, start)
        end = min(len(chars), end)
        for i in range(start, end):
            chars[i] = MASK
    return "".join(chars)
