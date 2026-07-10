"""The built-in, dependency-free ruleset.

Deliberately small. Its job is to make a default `pip install wiki-brain` behave
safely offline — not to compete with detect-secrets, TruffleHog, Gitleaks,
Presidio, or a maintained injection classifier. It will miss things those tools
catch. That is a design decision, not a defect, and `docs/SAFETY.md` says so in
public.

What it is for:

  * obvious secrets (well-known credential shapes, high-entropy assignments)
  * obvious injection phrases
  * basic unsafe tool-control directives
  * suspicious encoded blobs
  * a floor that is present when no optional engine is installed

Every rule is pure stdlib `re`, deterministic, and makes no network call. Rules
return `Finding`s stamped `engine="baseline"`; the engine wrapper does not
re-stamp them.
"""
from __future__ import annotations

import math
from collections import Counter

from ..models import Category, Finding, RiskLevel

BASELINE_ENGINE = "baseline"
BASELINE_VERSION = "1"


def hit(rule: str, kind: Category, severity: RiskLevel, message: str,
        start: int = 0, end: int = 0, confidence: float = 1.0,
        metadata: dict | None = None) -> Finding:
    """Build a baseline `Finding`. Never pass matched text into `metadata`."""
    return Finding(engine=BASELINE_ENGINE, engine_version=BASELINE_VERSION,
                   kind=kind, rule=rule, severity=severity, message=message,
                   confidence=confidence, start=start, end=end,
                   metadata=metadata or {})


def shannon_entropy(s: str) -> float:
    """Bits per character. Distinguishes a real token from `changemechangeme`."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())
