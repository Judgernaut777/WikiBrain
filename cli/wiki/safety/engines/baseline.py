"""The built-in engine. Always available, never enough.

Wraps the four deterministic rulesets in `safety/baseline/`. It is the floor that
makes a default install safe offline, and it is the only engine whose absence is a
configuration error rather than a missing optional dependency.

It does not catch exceptions. A ruleset that raises is a failure, and the pipeline
must hear about it.
"""
from __future__ import annotations

from ..baseline import BASELINE_VERSION, encoding, pii, prompt_injection, secrets
from ..baseline import tool_instructions
from ..models import Capability, Finding
from .base import BaseEngine, EngineScanRequest

_RULESETS = {
    Capability.secrets: secrets,
    Capability.pii: pii,
    Capability.prompt_injection: prompt_injection,
    Capability.tool_control: tool_instructions,
    Capability.encoded_content: encoding,
}


class BaselineEngine(BaseEngine):
    name = "baseline"
    version = BASELINE_VERSION
    capabilities = frozenset(_RULESETS)

    def __init__(self, **_) -> None:
        pass

    def available(self) -> bool:
        return True

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        #: Run only the rulesets the surface asked for: a recall should not pay for
        #: encoded-blob decoding it would merely warn about.
        wanted = request.capabilities or self.capabilities
        out: list[Finding] = []
        for capability, ruleset in _RULESETS.items():
            if capability in wanted:
                out.extend(ruleset.find(request.text))
        return out
