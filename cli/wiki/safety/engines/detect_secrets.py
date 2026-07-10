"""detect-secrets — the first real optional engine.

Yelp's detect-secrets is a Python library, so unlike TruffleHog and Gitleaks it
needs no executable and no subprocess: `pip install wiki-brain[safety-secrets]`
and it is there. That makes it the right default-on optional engine, and the one
worth reaching for before either CLI scanner. It brings entropy detection and a
maintained plugin set — novel credential shapes the baseline's ten regexes will
never see.

Scanning happens line by line and in memory. `scan_file` would apply the library's
own filters for us, but it wants a path, and writing a claim that may contain a
credential to a temp file in order to discover that it contains a credential is
exactly backwards.

**The entropy gate below is not optional.** `scan_line` yields *candidates*: it
runs each plugin's matcher and does not apply the plugin's entropy limit, which
lives further down the `scan_file` path. Left alone, it reports `The`, `is`, and
`seconds` as Base64 high-entropy strings, and a sentence about cache expiry comes
back fully masked. So the limits documented by the library — 4.5 bits/char for
base64, 3.0 for hex — are enforced here, along with a minimum length, and only for
the two entropy plugins. The 25 named detectors are precise and pass through.

Apache-2.0. Imported, not vendored.
"""
from __future__ import annotations

from ..baseline import shannon_entropy
from ..models import Capability, Finding, RiskLevel
from .base import BaseEngine, EngineScanRequest

#: detect-secrets' own documented defaults for its two entropy plugins, keyed by
#: the `type` string it reports. Applied here because `scan_line` does not.
ENTROPY_LIMITS = {
    "Base64 High Entropy String": 4.5,
    "Hex High Entropy String": 3.0,
}

#: A high-entropy run shorter than this is a word, not a credential.
MIN_ENTROPY_LENGTH = 20

#: Detectors whose hits are near-certainly live credentials. Everything else the
#: library reports is suggestive rather than conclusive, so it lands at `high`.
_CRITICAL_TYPES = frozenset({
    "AWS Access Key", "Private Key", "Stripe Access Key", "GitHub Token",
    "Azure Storage Account access key", "Slack Token", "Twilio API Key",
    "SendGrid API Key", "Square OAuth Secret", "NPM tokens",
    "OpenAI Token", "Anthropic API Key", "GitLab Token",
})


def keep(secret_type: str, value: str) -> bool:
    """Should this candidate become a finding?

    Pure, and importable without detect-secrets, so the offline gate can test the
    part of this adapter that actually decides anything.
    """
    limit = ENTROPY_LIMITS.get(secret_type)
    if limit is None:
        return True  # a named detector; it matched a shape, not a distribution
    if not value or len(value) < MIN_ENTROPY_LENGTH:
        return False
    return shannon_entropy(value) >= limit


def severity_for(secret_type: str) -> RiskLevel:
    if secret_type in ENTROPY_LIMITS:
        return RiskLevel.medium   # a heuristic, and one that fires on UUIDs
    return (RiskLevel.critical if secret_type in _CRITICAL_TYPES
            else RiskLevel.high)


def _slug(kind: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in kind.strip().lower()).strip("_")


class DetectSecretsEngine(BaseEngine):
    name = "detect_secrets"
    version = "unknown"
    capabilities = frozenset({Capability.secrets})

    def __init__(self, **_) -> None:
        self._ready = False
        try:
            from detect_secrets.core import scan as _scan  # type: ignore
            from detect_secrets.core.plugins.util import (  # type: ignore
                get_mapping_from_secret_type_to_class)
            from detect_secrets.settings import transient_settings  # type: ignore

            self._scan_line = _scan.scan_line
            self._transient_settings = transient_settings
            self._plugins = [{"name": cls.__name__} for cls in
                             sorted(set(get_mapping_from_secret_type_to_class()
                                        .values()), key=lambda c: c.__name__)]
            self.version = _version()
            self._ready = True
        except Exception:
            self._ready = False

    def available(self) -> bool:
        return self._ready

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        if not self._ready:
            raise RuntimeError("detect_secrets is not importable")

        out: list[Finding] = []
        with self._transient_settings({"plugins_used": self._plugins}):
            offset = 0
            for line in request.text.splitlines(keepends=True):
                for secret in self._scan_line(line):
                    kind = secret.type or "secret"
                    # `secret_value` is the credential. It is used to find a span
                    # and to measure entropy, then dropped: it never reaches the
                    # Finding, which is what gets persisted and returned.
                    raw = getattr(secret, "secret_value", None) or ""
                    if not keep(kind, raw):
                        continue
                    at = line.find(raw) if raw else -1
                    span = ((offset + at, offset + at + len(raw)) if at >= 0
                            else (0, 0))
                    out.append(self.finding(
                        rule=_slug(kind), capability=Capability.secrets,
                        severity=severity_for(kind),
                        message=f"detect-secrets matched {kind}",
                        start=span[0], end=span[1],
                        confidence=0.7 if kind in ENTROPY_LIMITS else 0.95))
                offset += len(line)
        return out


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("detect-secrets")
    except Exception:
        return "unknown"
