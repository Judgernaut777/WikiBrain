# ADR 0002 — Backup & restore via the SQLite online backup API

Status: accepted (2026-07-12, production-hardening pass)
Scope: `cli/brainconnect/backup.py`, `brainconnect backup` / `brainconnect restore`

## Context

The ledger is a WAL-mode SQLite database. Operators need a supported backup and
restore path with a rollback story. A file copy is unsafe: committed rows live in
the `-wal` sidecar until checkpointed, so `cp wiki.db backup.db` can miss recent
writes or capture a torn mix of the main file and an out-of-band WAL.

## Decision

Implement `backup`/`restore` with SQLite's **online backup API**
(`sqlite3.Connection.backup`), which walks a transactionally-consistent image of all
committed pages — WAL frames included — into a single self-contained file, without
stopping the writer.

- `backup` snapshots to a `.partial` file, runs `PRAGMA integrity_check`, refuses to
  publish a corrupt snapshot, and atomically renames into place.
- `restore` integrity-checks the source before trusting it, drops the target's stale
  `-wal`/`-shm`, copies pages into a fresh target, and re-verifies integrity. By
  default it first snapshots the current target to `<db>.pre-restore`, so a restore
  is reversible (roll-forward).

Restore requires the operator to stop `serve` first (it rewrites the served file);
this is documented rather than enforced, matching the single-node self-hosted model.

## Consequences

- Backups include WAL-resident data (proven: a naive `.db`-only copy loses 5/5
  WAL rows; the API snapshot keeps all 5).
- Round-trip and rollback are testable and tested end-to-end.
- Rejected alternatives: `.dump` to SQL text (slower, larger, and loses BLOB
  fidelity for embeddings), and filesystem snapshots (host-specific, out of scope for
  a portable CLI).

## Evidence

- `scratchpad/waveA/WikiBrain/backup_restore_upgrade_demo.py` — WAL inclusion,
  round-trip, rollback, and upgrade-from-RC1.
- `tests/acceptance.py` `[hardening]` — backup integrity, round-trip, corrupt-backup
  refusal (in the gate).
