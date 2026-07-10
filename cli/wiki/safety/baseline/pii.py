"""Baseline personal-data detection. Known-incomplete, on purpose.

Five regexes and a Luhn check. It finds an email address and a credit card. It
does not find a name, an address, a date of birth, a medical record number, or any
of the hundred other things a PII platform detects, and it will not learn to.
Enable `presidio` if PII coverage matters to you.

Patterns ported from fascia-guard's `scanners/pii.py` (same owner).
"""
from __future__ import annotations

import re

from ..models import Category, Finding, RiskLevel
from . import hit

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}[\s\-](?!00)\d{2}[\s\-](?!0000)\d{4}\b")
_IPV4 = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?"
                    r"\d{3}[\s.\-]?\d{3,4}[\s.\-]?\d{0,4}(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < e and end > s for s, e in spans)


def find(text: str) -> list[Finding]:
    out: list[Finding] = []
    claimed: list[tuple[int, int]] = []

    for m in _EMAIL.finditer(text):
        claimed.append(m.span())
        out.append(hit("email", Category.pii, RiskLevel.medium,
                       "an email address", start=m.start(), end=m.end()))

    for m in _SSN.finditer(text):
        claimed.append(m.span())
        out.append(hit("ssn", Category.pii, RiskLevel.high,
                       "a US social-security-number shape",
                       start=m.start(), end=m.end()))

    for m in _CARD.finditer(text):
        digits = re.sub(r"[ \-]", "", m.group(0))
        if not (13 <= len(digits) <= 19) or not _luhn_ok(digits):
            continue  # a long number, not a card
        claimed.append(m.span())
        out.append(hit("credit_card", Category.pii, RiskLevel.high,
                       "a Luhn-valid payment card number",
                       start=m.start(), end=m.end()))

    for m in _IPV4.finditer(text):
        if _overlaps(claimed, *m.span()):
            continue
        claimed.append(m.span())
        out.append(hit("ipv4", Category.pii, RiskLevel.low,
                       "an IPv4 address", start=m.start(), end=m.end()))

    # Phones last: the pattern is loose and would otherwise swallow the leading
    # digits of a card or an SSN that a stronger rule already claimed.
    for m in _PHONE.finditer(text):
        if _overlaps(claimed, *m.span()):
            continue
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 10:
            continue
        claimed.append(m.span())
        out.append(hit("phone", Category.pii, RiskLevel.low,
                       "a telephone-number shape", confidence=0.6,
                       start=m.start(), end=m.end()))

    return out
