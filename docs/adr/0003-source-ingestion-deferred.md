# ADR 0003 — Source ingestion & projection safety: DEFERRED, fail-closed

Status: accepted (2026-07-12, production-hardening pass)
Scope: `cli/brainconnect/safety/` (policy surfaces), `docs/SAFETY.md`

## Context

Three safety surfaces are enforced today: `memory_candidate` (capture),
`memory_recall` (read), and `memory_promotion` (the human gate). Two more are
conceivable — scanning **source ingestion** (fetched web/document content) and the
**Obsidian projection** (`wiki/` render). The production-hardening ask (#8) is to
either implement these safely or defer them with safe defaults documented.

## Decision

**Defer both, and keep the deferral fail-closed.** There is no policy for
`source_ingest` or any projection surface, and asking to scan an unknown surface
**raises `PolicyError`** rather than silently returning "allowed". A future engine
cannot be wired to a surface that does not exist, and no code path treats an
unscanned new surface as clean.

The load-bearing safety boundary that already protects ingestion is unchanged and
is **not** deferred: *all fetched/captured content is untrusted data, never
instructions.* Ingested source text only ever enters the ledger as a `memory_candidate`,
which **is** scanned (`memory_candidate` surface) and requires a human promotion
before it can reach trusted recall. So untrusted source content is already gated by
the capture scan + the human gate before it can influence anything.

## Consequences

- Safe default: a new surface is refused, not allowed. Adding source-ingest scanning
  later is additive (define the policy, wire an engine) and cannot silently regress
  the current guarantees.
- No false sense of coverage: there is no disabled-but-present "ingest scanner" that
  an operator could believe is protecting them.

## Evidence

- `safety.policy("source_ingest")` / `"projection"` raise `PolicyError` (verified).
- `docs/SAFETY.md` records the deferral and the untrusted-data boundary.
