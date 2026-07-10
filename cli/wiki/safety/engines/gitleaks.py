"""Gitleaks — an independently selectable secret scanner.

An alternative to TruffleHog, not a companion. Enabling both doubles the subprocess
cost on every promotion to catch a thin margin of extra credential shapes; pick
one. Like TruffleHog it declares only `source_or_repository_secrets`, so it stays
off the recall path.

Gitleaks does no network verification at all, which is one fewer footgun.

MIT, invoked as a separate executable.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import Capability, Finding, RiskLevel
from .base import ExternalToolEngine


class GitleaksEngine(ExternalToolEngine):
    name = "gitleaks"
    version = "cli"
    capabilities = frozenset({Capability.source_or_repository_secrets})
    executable = "gitleaks"

    def argv(self, target: Path) -> list[str]:
        return [self.executable, "detect", "--no-git", "--redact",
                "--report-format", "json", "--report-path", "/dev/stdout",
                "--source", str(target.parent if target.is_file() else target),
                "--exit-code", "0"]

    def parse(self, stdout: str, text: str) -> list[Finding]:
        stdout = stdout.strip()
        if not stdout:
            return []
        try:
            records = json.loads(stdout)
        except json.JSONDecodeError:
            records = self.json_lines(stdout)
        if not isinstance(records, list):
            return []

        out: list[Finding] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            rule = record.get("RuleID") or record.get("Description") or "unknown"
            # `--redact` means Secret comes back masked, so it cannot be used to
            # locate a span. Fall back to the match, and to no span at all.
            raw = record.get("Match") or ""
            start, end = self.locate(text, raw)
            out.append(self.finding(
                rule=str(rule).lower().replace(" ", "_"),
                capability=Capability.source_or_repository_secrets,
                severity=RiskLevel.high,
                message=f"gitleaks matched {rule}",
                start=start, end=end, confidence=0.85,
                metadata={"entropy": record["Entropy"]}
                if isinstance(record.get("Entropy"), (int, float)) else {}))
        return out
