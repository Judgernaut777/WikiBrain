"""TruffleHog — whole-file and repository secret scanning.

Declares only `source_or_repository_secrets`, never plain `secrets`. That is
deliberate: it keeps a process spawn off the recall path, where claims are a
sentence long and a 20-second subprocess budget per item would be absurd. Policy
asks for this capability on promotion and on source ingest, and nowhere else.

Two invocation flags carry the safety property:

  * `--no-verification` — without it, TruffleHog *verifies* a candidate credential
    by authenticating against the service it belongs to. That mails the secret to
    a third party. It is the exact thing this module exists to prevent, and it is
    the tool's default. Turning it back on requires
    `allow_network_verification = true`, per engine, in config.
  * `--results=verified,unknown,unverified` — under `--no-verification` every hit
    is "unverified", and the default result filter hides those. Omitting this
    silently reports every file clean.

AGPL-3.0, invoked as a separate executable. Never imported, never linked, so
WikiBrain's Apache-2.0 licensing is unaffected.
"""
from __future__ import annotations

from pathlib import Path

from ..models import Capability, Finding, RiskLevel
from .base import ExternalToolEngine


class TruffleHogEngine(ExternalToolEngine):
    name = "trufflehog"
    version = "cli"
    capabilities = frozenset({Capability.source_or_repository_secrets})
    executable = "trufflehog"

    def argv(self, target: Path) -> list[str]:
        argv = [self.executable, "filesystem", str(target),
                "--json", "--no-update",
                "--results=verified,unknown,unverified"]
        if not self.allow_network_verification:
            argv.append("--no-verification")
        return argv

    def parse(self, stdout: str, text: str) -> list[Finding]:
        out: list[Finding] = []
        for record in self.json_lines(stdout):
            detector = record.get("DetectorName") or "unknown"
            verified = bool(record.get("Verified"))
            raw = record.get("Raw") or ""
            start, end = self.locate(text, raw)
            out.append(self.finding(
                rule=str(detector).lower(),
                capability=Capability.source_or_repository_secrets,
                severity=RiskLevel.critical if verified else RiskLevel.high,
                message=f"trufflehog matched {detector}",
                start=start, end=end,
                confidence=1.0 if verified else 0.85,
                metadata={"verified": verified}))
        return out
