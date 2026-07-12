# ADR 0001 — Production concurrency: atomic promotion + service-mode projections

Status: accepted (2026-07-12, production-hardening pass)
Scope: `cli/brainconnect/candidates.py`, `cli/brainconnect/db.py`,
`cli/brainconnect/server.py`

## Context

`brainconnect serve` runs on a `ThreadingHTTPServer` with a fresh `Repo` (SQLite
connection) per request. A concurrent load test (24 workers × 150 requests, plus a
32-worker promotion race) against a real `serve` subprocess surfaced two problems:

1. **Double-promote under a race.** Promotion is a read-check-write. With each
   request on its own connection, two concurrent promotions of the *same* candidate
   both read `status='pending'`, both insert a claim, and both commit — one candidate
   forked into two trusted claims. The load test showed **3 winners** for one
   candidate. This is a correctness defect in the human gate: the ledger must have
   exactly one claim per promoted candidate.

2. **Throughput collapse (24 req/s, p95 ≈ 2.9 s).** Every mutating `Repo.finalize`
   rewrites `db/dump.sql` (a full `iterdump` of the whole database) and appends to
   `log.md`. Under the service these run per request — an O(database) cost that
   serializes writers — and several server threads writing the same working-tree
   files is a corruption hazard SQLite itself does not have.

## Decision

**1. Make promotion atomic.** `candidates.promote` wraps the read-check-write in an
explicit `BEGIN IMMEDIATE` transaction (acquiring the write lock before the status
read, so a racing promoter waits on `busy_timeout` and then reads `promoted`), and
makes the candidate `UPDATE` conditional on `status='pending'` with a `rowcount == 1`
assertion; a lost race rolls the whole promotion back. Belt (conditional UPDATE) and
braces (immediate lock) so the invariant holds even if the locking model changes.

**2. Add a service mode to `Repo`.** `Repo.open(..., write_projections=False)` (used
by `server.py`) commits to the DB but skips the `db/dump.sql` rewrite and the
`log.md` append. The database is the sole source of truth for the service; a human
regenerates the curation projections with `brainconnect dump`. The CLI path is
unchanged (`write_projections=True` by default), so the curation workflow keeps its
git-committed mirror and ops log.

## Consequences

- No double-promote under concurrency: verified, exactly one claim per candidate,
  `integrity_check == ok` after a 32-thread race.
- Throughput 24 → **318 req/s**, p95 2891 → 179 ms, zero lost writes.
- The served DB no longer produces a fresh `db/dump.sql` per request; operators who
  want the textual mirror run `brainconnect dump` (or a periodic job).
- Alternatives rejected: a global application write-lock (redundant with SQLite's
  single-writer + slower), and a WSGI/ASGI framework (breaks the "pure stdlib inside
  the trust boundary, zero model calls" constraint for no concurrency gain on a
  single-writer store).

## Evidence

- `scratchpad/waveA/WikiBrain/concurrency_loadtest.py` — load + race + integrity.
- `tests/acceptance.py` `[hardening]` section — atomic-promotion, WAL/reader/busy,
  and service-mode regressions (all in the gate).
