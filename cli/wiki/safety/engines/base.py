"""The engine contract. Detection only, never policy.

An engine answers *does this text contain a probable secret* and nothing else. It
does not know what surface it is scanning, what WikiBrain will do about a finding,
or whether the claim is trusted. Keeping that line sharp is what lets a maintained
third-party tool be dropped in without giving it a vote on trust.

Two rules for every implementation:

  * `available()` must not scan, must not raise, and must not download anything.
    It answers "could I run right now", cheaply.
  * `scan()` raising means **failure**, not absence. The pipeline maps a raising
    engine to `EngineStatus.failed` and the policy fails closed. An engine that
    swallows its own exception and returns `[]` is lying about having looked.

Ported from mcp-agentconnect's `safety/engines/base.py` (same owner, MIT).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from ..models import (CATEGORY_OF_CAPABILITY, Capability, Finding, RiskLevel)

#: An unbounded subprocess is an unbounded recall.
DEFAULT_TIMEOUT_SECONDS = 20.0


@dataclass
class EngineScanRequest:
    text: str
    surface: str
    #: What the surface asked for. An engine with several rulesets may narrow to
    #: these; one with a single ruleset can ignore it, since the pipeline has
    #: already decided the engine is worth running.
    capabilities: frozenset = frozenset()
    #: Set only when the caller has a real file to scan. Whole-file engines are
    #: given a temporary file when this is None.
    path: Optional[Path] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SafetyEngine(Protocol):
    """Detection only. Never policy."""

    name: str
    version: str
    capabilities: frozenset

    def available(self) -> bool:
        """Can this engine run right now? Must not scan, and must not raise."""

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        """Normalized findings. Raising means *failure*, not absence."""


class BaseEngine:
    """Shared plumbing. Subclasses set `name`, `version`, `capabilities`."""

    name: str = "engine"
    version: str = "0"
    capabilities: frozenset = frozenset()

    def available(self) -> bool:  # pragma: no cover - overridden
        return True

    def scan(self, request: EngineScanRequest) -> list[Finding]:  # pragma: no cover
        raise NotImplementedError

    def finding(self, rule: str, capability: Capability, severity: RiskLevel,
                message: str, start: int = 0, end: int = 0,
                confidence: float = 1.0,
                metadata: Optional[dict] = None) -> Finding:
        return Finding(
            engine=self.name, engine_version=self.version,
            kind=CATEGORY_OF_CAPABILITY[capability], rule=rule, severity=severity,
            message=message, confidence=confidence, start=start, end=end,
            metadata=metadata or {})


class ExternalToolEngine(BaseEngine):
    """Base for engines backed by an installed executable.

    Three rules, each load-bearing:

    1. **No network.** These tools offer to *verify* a candidate credential by
       calling the service it belongs to. Verification is exfiltration: it takes
       the secret WikiBrain is trying to contain and mails it to a third party to
       ask whether it still works. It stays off unless an operator explicitly opts
       in, per invocation flags — never by default, and never silently.
    2. **A timeout.** A subprocess with no budget is a recall that never returns.
    3. **Structured output.** We parse JSON, never prose. A tool that changes its
       human-readable format must not silently start reporting everything clean.

    Raw matched values are used only to locate a span, and are then dropped. They
    never reach a `Finding`, a log line, or candidate metadata.
    """

    executable: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    allow_network_verification: bool = False

    def __init__(self, executable: Optional[str] = None,
                 timeout_seconds: Optional[float] = None,
                 allow_network_verification: bool = False, **_: Any) -> None:
        self.executable = executable or self.executable
        self.timeout_seconds = (self.timeout_seconds if timeout_seconds is None
                                else float(timeout_seconds))
        self.allow_network_verification = bool(allow_network_verification)

    def available(self) -> bool:
        return bool(self.executable) and shutil.which(self.executable) is not None

    def argv(self, target: Path) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def parse(self, stdout: str, text: str) -> list[Finding]:  # pragma: no cover
        raise NotImplementedError

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        target = request.path
        with tempfile.TemporaryDirectory(prefix="wikibrain-safety-") as tmp:
            if target is None:
                target = Path(tmp) / "content.txt"
                target.write_text(request.text, encoding="utf-8")
            try:
                proc = subprocess.run(self.argv(target), capture_output=True,
                                      text=True, timeout=self.timeout_seconds,
                                      check=False)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f"{self.name} exceeded {self.timeout_seconds}s") from exc
            except OSError as exc:  # vanished between available() and here
                raise RuntimeError(f"{self.name} could not run: {exc}") from exc
        return self.parse(proc.stdout, request.text)

    @staticmethod
    def json_lines(stdout: str) -> list[dict]:
        records: list[dict] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    @staticmethod
    def locate(text: str, raw: str) -> tuple[int, int]:
        """Find `raw` in `text` so it can be masked — then forget `raw`."""
        if not raw:
            return (0, 0)
        index = text.find(raw)
        return (index, index + len(raw)) if index >= 0 else (0, 0)
