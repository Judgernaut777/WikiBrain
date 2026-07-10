"""DEPRECATED. The legacy optional `fascia-guard` seam. Inert.

Superseded by `wiki.safety`, which is WikiBrain-local, enabled by default, and
requires no third-party package. Nothing in WikiBrain calls this module any more:
capture, recall and promotion all go through `wiki.safety.scan_for`.

**Two safety pipelines must never both be authoritative.** Rather than define an
ordering between them, this one is switched off. Every function below is a no-op,
`available()` and `active()` return False regardless of `FASCIA_GUARD` or
`FASCIA_GUARD_ENFORCE`, and setting either variable warns instead of silently
re-enabling a second, weaker gate.

Retained only so an out-of-tree import does not crash. It will be deleted; do not
add behaviour to it. Configure `[safety]` in config.toml instead — see
docs/SAFETY.md, which maps each old flag onto its replacement.
"""
from __future__ import annotations

import os
import warnings

#: The environment variables the old seam honoured. They no longer do anything.
LEGACY_FLAGS = ("FASCIA_GUARD", "FASCIA_GUARD_ENFORCE")

_REPLACEMENT = (
    "wiki.safety is now the built-in safety pipeline; it is enabled by default "
    "and needs no external package. Configure it under [safety] in config.toml. "
    "See docs/SAFETY.md."
)


def _warn_if_set() -> None:
    live = [f for f in LEGACY_FLAGS if os.environ.get(f, "").strip()]
    if live:
        warnings.warn(
            f"{'/'.join(live)} is set, but the fascia-guard hook is deprecated and "
            f"disabled. It is doing nothing. {_REPLACEMENT}",
            DeprecationWarning, stacklevel=3)


def available() -> bool:
    """False, always. The seam is retired regardless of what is installed."""
    _warn_if_set()
    return False


def active() -> bool:
    _warn_if_set()
    return False


def enforcing() -> bool:
    _warn_if_set()
    return False


def check_capture(text: str):
    return None


def check_recall(text: str):
    return None


def carries_secret(verdict) -> bool:
    return False


def redact_secrets(text: str) -> str:
    return text


def categories(verdict) -> list[str]:
    return []
