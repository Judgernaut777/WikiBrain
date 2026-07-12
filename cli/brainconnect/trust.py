"""The one trusted-authority predicate (LEDGER_SPEC §14.1 — status is not trust).

A claim is **trusted** iff it is *promoted* AND *not party to an open
contradiction*. `status` reflects the ledger's decision of record; `trusted` is
the stronger authority signal a consumer routes on. The two are deliberately
distinct: a promoted claim that is later contradicted stays *of record* (status
``promoted``) but is no longer *trusted*.

This boundary is now **externally exposed** — ``GET /registry`` on the ``:8787``
server serves exactly the claims for which this predicate is true, and
AgentConnect's ``RoutingEngine`` keys on it in place of self-conferred
``learned_quality``. Because an external consumer's routing depends on it, the
predicate lives in exactly ONE place and every producer of a ``trusted`` flag —
recall (``recall.py``), OKF export (``okf/export.py``), and the capability
registry (``registry.py``) — calls it here. Duplicating the boolean invited
skew across the surfaces that publish the same trust claim.

Pure code, zero model calls, deterministic.
"""
from __future__ import annotations

#: The single status value that can be trusted. Kept as a named constant so the
#: predicate and its callers never re-spell the string literal.
TRUSTED_STATUS = "promoted"


def is_trusted(*, status: str, contradicted: bool) -> bool:
    """True iff `status` is promoted AND the claim is not in an open contradiction.

    `contradicted` is the caller's already-resolved answer to "is this claim party
    to an OPEN contradiction?" — a set membership in recall/export, a direct
    ``contradictions`` query in the registry. This function does not read the
    ledger; it only encodes the boundary, so every surface agrees on what the word
    "trusted" means.
    """
    return status == TRUSTED_STATUS and not contradicted
