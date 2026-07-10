"""Baseline credential detection.

Well-known credential shapes, plus one entropy-gated heuristic for the
`api_key = "…"` form that no fixed prefix can catch. The entropy gate is what
keeps `password = "changemechangeme"` out of the findings: it is a placeholder,
and blocking on it would train people to disable the scanner.

Patterns ported from fascia-guard's `scanners/secrets.py` (same owner).

Enable `detect_secrets` for real coverage; enable `gitleaks` or `trufflehog` for
whole-file and repository scanning.
"""
from __future__ import annotations

import re

from ..models import Category, Finding, RiskLevel
from . import hit, shannon_entropy

#: Minimum bits/char before a generic assignment counts as a credential.
ENTROPY_FLOOR = 3.0

_PATTERNS: list[tuple[str, RiskLevel, re.Pattern]] = [
    ("aws_access_key", RiskLevel.critical,
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_pat", RiskLevel.critical,
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_fine_pat", RiskLevel.critical,
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack_token", RiskLevel.high,
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", RiskLevel.high,
     re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("openai_key", RiskLevel.high,
     re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", RiskLevel.critical,
     re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("stripe_secret", RiskLevel.critical,
     re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{16,}\b")),
    ("private_key_block", RiskLevel.critical,
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE" + r" KEY-----")),
    ("jwt", RiskLevel.medium,
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

_GENERIC = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\b"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9/+=_\-]{16,})['\"]?")


def find(text: str) -> list[Finding]:
    out: list[Finding] = []
    for rule, severity, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            out.append(hit(rule, Category.secret, severity,
                           f"content matches the {rule} credential shape",
                           start=m.start(), end=m.end()))
    for m in _GENERIC.finditer(text):
        value = m.group(1)
        entropy = shannon_entropy(value)
        if entropy < ENTROPY_FLOOR:
            continue  # a placeholder, not a credential
        out.append(hit("generic_assignment", Category.secret, RiskLevel.high,
                       "a credential-shaped assignment with high-entropy value",
                       start=m.start(1), end=m.end(1),
                       confidence=min(1.0, entropy / 5.0),
                       metadata={"entropy_bits_per_char": round(entropy, 2)}))
    return out
