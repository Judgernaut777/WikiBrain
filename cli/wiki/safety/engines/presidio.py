"""Microsoft Presidio — the preferred PII engine.

WikiBrain will not build a PII platform. The baseline finds an email and a
Luhn-valid card; Presidio finds a person, a location, a date of birth, a medical
licence number, and does it across languages, with a maintained recognizer set.

Loaded lazily: constructing an `AnalyzerEngine` pulls in spaCy and a language
model, which is slow and must not happen at import time just because someone ran
`wiki search`.

Presidio's own recall on free text is roughly 0.5 F1 on field-limited benchmarks.
That is better than the baseline by a wide margin and still not a guarantee.

MIT. Imported, not vendored. GLiNER, which would raise recall further as a custom
Presidio recognizer, is deferred — see docs/SAFETY.md.
"""
from __future__ import annotations

from ..models import Capability, Finding, RiskLevel
from .base import BaseEngine, EngineScanRequest

#: Presidio entity types that identify a person outright.
_HIGH = frozenset({"US_SSN", "CREDIT_CARD", "IBAN_CODE", "US_PASSPORT",
                   "US_DRIVER_LICENSE", "MEDICAL_LICENSE", "US_BANK_NUMBER",
                   "CRYPTO"})
_MEDIUM = frozenset({"EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON", "US_ITIN"})

#: Presidio reports these constantly and they are rarely personal data in a
#: memory claim about software.
_IGNORED = frozenset({"URL", "DATE_TIME", "NRP"})


class PresidioEngine(BaseEngine):
    name = "presidio"
    version = "unknown"
    capabilities = frozenset({Capability.pii})

    def __init__(self, score_threshold: float = 0.5, **_) -> None:
        self.score_threshold = float(score_threshold)
        self._analyzer = None
        self._importable = False
        try:
            import presidio_analyzer  # type: ignore

            self.version = getattr(presidio_analyzer, "__version__", "unknown")
            self._importable = True
        except Exception:
            self._importable = False

    def available(self) -> bool:
        return self._importable

    def _engine(self):
        if self._analyzer is None:
            from presidio_analyzer import AnalyzerEngine  # type: ignore

            self._analyzer = AnalyzerEngine()
        return self._analyzer

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        if not self._importable:
            raise RuntimeError("presidio_analyzer is not importable")
        results = self._engine().analyze(text=request.text, language="en",
                                         score_threshold=self.score_threshold)
        out: list[Finding] = []
        for r in results:
            entity = r.entity_type
            if entity in _IGNORED:
                continue
            severity = (RiskLevel.high if entity in _HIGH
                        else RiskLevel.medium if entity in _MEDIUM
                        else RiskLevel.low)
            out.append(self.finding(
                rule=entity.lower(), capability=Capability.pii, severity=severity,
                message=f"presidio matched {entity}",
                start=r.start, end=r.end, confidence=float(r.score)))
        return out
