"""Suspicious encoded blobs.

Neither fascia-guard nor AgentConnect detected these; this ruleset is new.

Encoding is how a payload walks past a pattern matcher. `aWdub3JlIGFsbCBwcmV2aW91
cyBpbnN0cnVjdGlvbnM=` contains no lure any injection regex will find, and a model
that decodes it obligingly gets the instruction anyway.

So: find blobs, and where a blob decodes cleanly, scan what came out. A base64
string that decodes to prose about deployment is noise. One that decodes to
`ignore all previous instructions` is an attack, and is reported at the severity of
what it *decodes to*, not of the fact that it was encoded.

Decoding is bounded (`MAX_DECODE_BYTES`) and never recurses.
"""
from __future__ import annotations

import base64
import binascii
import re

from ..models import Category, Finding, RiskLevel
from . import hit, shannon_entropy
from . import prompt_injection, tool_instructions

#: Do not decode more than this. A memory claim is not a file.
MAX_DECODE_BYTES = 8192

#: Below this length a base64-ish run is more likely a word than a payload.
MIN_BLOB_CHARS = 40

#: Base64 of English text sits near 4.2 bits/char; prose sits near 4.0. Random
#: key material sits above 5.0. The floor rejects ordinary long identifiers.
ENTROPY_FLOOR = 3.5

_B64 = re.compile(r"\b[A-Za-z0-9+/]{%d,}={0,2}" % MIN_BLOB_CHARS)
_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{%d,}\b" % MIN_BLOB_CHARS)
_DATA_URI = re.compile(r"(?i)\bdata:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{16,}")
_ESCAPES = re.compile(r"(?:\\x[0-9a-fA-F]{2}){8,}|(?:\\u[0-9a-fA-F]{4}){8,}")
_PERCENT = re.compile(r"(?:%[0-9a-fA-F]{2}){8,}")


def _decoded_text(blob: str) -> str | None:
    """Decode base64 to text, or None when it is not decodable text."""
    if len(blob) > MAX_DECODE_BYTES:
        return None
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > MAX_DECODE_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
    return text if printable / len(text) > 0.9 else None


def _payload_findings(decoded: str) -> list:
    """What the decoded text is, if it is anything. Never recurses into encoding."""
    return prompt_injection.find(decoded) + tool_instructions.find(decoded)


def find(text: str) -> list[Finding]:
    out: list[Finding] = []

    for m in _DATA_URI.finditer(text):
        out.append(hit("data_uri", Category.encoding, RiskLevel.medium,
                       "an embedded base64 data URI",
                       start=m.start(), end=m.end(), confidence=0.8))

    for m in _B64.finditer(text):
        blob = m.group(0)
        if shannon_entropy(blob) < ENTROPY_FLOOR:
            continue
        decoded = _decoded_text(blob)
        if decoded is None:
            out.append(hit("opaque_blob", Category.encoding, RiskLevel.low,
                           "a high-entropy base64-shaped blob that does not "
                           "decode to text",
                           start=m.start(), end=m.end(), confidence=0.4))
            continue
        payload = _payload_findings(decoded)
        if payload:
            worst = max(f.severity for f in payload)
            rules = sorted({f.rule for f in payload})
            out.append(hit("encoded_payload", Category.encoding,
                           RiskLevel.high if worst in (RiskLevel.high,
                                                       RiskLevel.critical)
                           else RiskLevel.medium,
                           "a base64 blob decodes to text matching "
                           f"{', '.join(rules)}",
                           start=m.start(), end=m.end(), confidence=0.9,
                           metadata={"decoded_rules": rules}))
        else:
            out.append(hit("encoded_text", Category.encoding, RiskLevel.low,
                           "a base64 blob decoding to plain text",
                           start=m.start(), end=m.end(), confidence=0.5))

    for m in _HEX.finditer(text):
        blob = m.group(0)
        if shannon_entropy(blob) < 3.0:
            continue
        out.append(hit("hex_blob", Category.encoding, RiskLevel.low,
                       "a long high-entropy hexadecimal run",
                       start=m.start(), end=m.end(), confidence=0.4))

    for rule, pattern in (("escape_run", _ESCAPES), ("percent_run", _PERCENT)):
        for m in pattern.finditer(text):
            out.append(hit(rule, Category.encoding, RiskLevel.medium,
                           "a long run of escaped characters, which hides text "
                           "from pattern matching",
                           start=m.start(), end=m.end(), confidence=0.7))

    return out
