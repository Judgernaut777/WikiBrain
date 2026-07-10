"""Baseline prompt-injection heuristics.

Six lure patterns. A fast deterministic filter is *provably* evadable — any
attacker who can query it can construct text that passes it — so this is a layer
that raises cost, never an authority. The structural defences are the ones that
hold: all recalled text is data, agents cannot promote, and a quarantined claim is
withheld rather than trusted.

Read that sentence again before enabling anything downstream that treats a clean
injection scan as permission.

Lures ported from fascia-guard's `scanners/injection.py` heuristic backend (same
owner). Enable `prompt_guard` for a maintained classifier on top.
"""
from __future__ import annotations

import re

from ..models import Category, Finding, RiskLevel
from . import hit

_LURES: list[tuple[str, RiskLevel, re.Pattern]] = [
    ("ignore_instructions", RiskLevel.high, re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
        r"\b(?:previous|prior|above|earlier|all|any)\b[^.\n]{0,20}"
        r"\b(?:instruction|prompt|rule|direction|context)s?\b")),
    ("system_prompt_probe", RiskLevel.high, re.compile(
        r"(?i)\b(?:reveal|print|show|repeat|output|leak)\b[^.\n]{0,30}"
        r"\b(?:system\s+prompt|initial\s+instructions|your\s+instructions)\b")),
    ("role_override", RiskLevel.high, re.compile(
        r"(?i)\byou\s+are\s+now\b|\bact\s+as\b[^.\n]{0,30}"
        r"\b(?:dan|jailbroken|unfiltered|no\s+restrictions)\b")),
    ("dev_mode", RiskLevel.medium, re.compile(
        r"(?i)\b(?:developer|debug|god)\s+mode\b|\benable\s+dev\b")),
    ("instruction_markers", RiskLevel.medium, re.compile(
        r"(?i)(?:^|\n)\s*(?:system|assistant)\s*:\s|\[/?INST\]|<\|im_start\|>")),
    ("memory_poisoning", RiskLevel.high, re.compile(
        r"(?i)\b(?:remember|store|save|memorize)\b[^.\n]{0,30}"
        r"\b(?:always|never|from\s+now\s+on)\b[^.\n]{0,40}"
        r"\b(?:approve|trust|promote|allow|grant)\b")),
]


def find(text: str) -> list[Finding]:
    out: list[Finding] = []
    for rule, severity, pattern in _LURES:
        for m in pattern.finditer(text):
            out.append(hit(rule, Category.prompt_injection, severity,
                           f"text resembles a prompt-injection lure ({rule})",
                           start=m.start(), end=m.end(), confidence=0.6))
    return out
