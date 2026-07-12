# OPERATIONS.md — running BrainConnect as a self-hosted service

BrainConnect serves the trusted memory ledger over HTTP (`brainconnect serve`,
default `127.0.0.1:8787`) to AgentConnect's `WikiBrainMemoryAdapter`. This doc is
the operator's manual: the concurrency model, backup/restore/rollback, and the
degradation behaviour you should expect under load and under failure.

The live database is a single SQLite file at an absolute path from config
(`[paths] db`, default `~/.wiki-brain/wiki.db`). Everything here is about that file.

---

## 1. Concurrency model

`brainconnect serve` is a stdlib `ThreadingHTTPServer`: one thread per connection,
each request opening its own short-lived `Repo` (a fresh SQLite connection) and
closing it when the request ends — the same pattern as the MCP server. There is no
shared connection and no connection pool to corrupt.

SQLite is the concurrency authority, configured on every open (`db.Repo.open`):

- **WAL journal mode** (`PRAGMA journal_mode=WAL`). Readers never block the writer
  and the writer never blocks readers; a recall during a capture proceeds and sees
  the last committed state, never a half-written row.
- **Single-writer serialization.** SQLite permits exactly one writer at a time.
  Concurrent captures/promotions/feedback are serialized by the database, not by
  application locking.
- **`busy_timeout=10000`.** A writer that finds the write lock held waits up to
  10 s for it rather than failing fast with `SQLITE_BUSY` ("database is locked").
  Under self-hosted load this converts contention into latency, not errors.

### Atomic promotion (why a race cannot double-promote)

Promotion is a read-check-write: *is this candidate still pending? then insert a
claim and mark it promoted.* Across a thread pool that is not atomic by default —
two requests can both read `pending`, both insert a claim, and both commit,
forking one candidate into two trusted claims. `candidates.promote` therefore opens
a `BEGIN IMMEDIATE` transaction (taking the write lock up front, so the loser waits
on `busy_timeout`) and the candidate `UPDATE` is conditional on `status='pending'`
with a row-count assertion. The second promoter reads `promoted` and refuses; if it
somehow raced past the read, the conditional UPDATE matches zero rows and the whole
promotion rolls back. Proven under a 32-thread race:
`scratchpad/waveA/WikiBrain/concurrency_loadtest.py`.

### Service mode: the DB is the whole record

The CLI refreshes two **curation-workflow projections** on every mutation: a
git-committed textual mirror (`db/dump.sql`, a full `iterdump` of the database) and
an ops log (`log.md`). Those are right for a human running one command at a time and
**wrong for the service**: rewriting `db/dump.sql` on every request is an O(database)
cost that collapses throughput, and several server threads writing the same
working-tree files is a corruption hazard the database itself does not have.

`brainconnect serve` therefore opens repos with `write_projections=False`: writes go
to the DB only. Regenerate the projections on demand with `brainconnect dump`.

### Observed throughput (this host, aarch64, Python 3.11)

24 workers × 150 requests = 3600 mixed capture/recall/feedback/health requests
against a real `serve` subprocess:

| build | throughput | p50 | p95 | p99 |
|-------|-----------:|----:|----:|----:|
| before service-mode | 24 req/s | 258 ms | 2891 ms | 3054 ms |
| after service-mode  | **318 req/s** | **42 ms** | 179 ms | 692 ms |

Zero transport/5xx errors; `integrity_check == ok`; every HTTP-200 capture is a
durable row (no lost or torn writes). This is ample for a single-node self-hosted
deployment. It is a single-writer store: if you need many thousands of sustained
writes per second, that is a different backend, not a tuning knob.

---

## 2. Backup

```
brainconnect backup --out /path/to/snapshot.db
```

Uses SQLite's **online backup API**, which copies a transactionally-consistent image
of every committed page — *including committed WAL frames* — into a single
self-contained file. This is why a naive `cp wiki.db backup.db` is wrong: recently
committed rows live in the `-wal` sidecar, and copying the main file alone loses them
(demonstrated deterministically in `backup_restore_upgrade_demo.py`). The command
runs `PRAGMA integrity_check` on the snapshot and refuses to write a corrupt one. It
does not require stopping `serve`.

Schedule it from cron/systemd-timer; the snapshot is a normal file — copy it
off-host, keep as many as you keep.

---

## 3. Restore & rollback

```
# 1. stop the service (a restore rewrites the file it serves)
systemctl --user stop brainconnect          # or however you run it

# 2. restore a known-good snapshot
brainconnect restore --from /path/to/snapshot.db

# 3. restart
systemctl --user start brainconnect
```

`restore` integrity-checks the backup **before** trusting it, drops the target's
stale `-wal`/`-shm` sidecars so they cannot shadow the restored file, and copies the
snapshot's pages into place via the backup API. It verifies the result's integrity
and reports whether row counts match the source.

**Rollback safety net.** Before overwriting, `restore` snapshots the *current* target
to `<db>.pre-restore` (disable with `--no-pre-restore`, relocate with
`--pre-restore-out`). So a restore is itself reversible: if you restored the wrong
snapshot, `brainconnect restore --from <db>.pre-restore` rolls forward to where you
were. Every state is a file; nothing is destroyed in place.

---

## 4. Upgrade

Schema migrations are forward-only and run automatically on the first `Repo.open`
after an upgrade — including the open that `serve` performs at launch
(`docs/MIGRATIONS.md`). To upgrade safely:

1. `brainconnect backup --out pre-upgrade.db`  (roll-back point)
2. install the new version
3. start it once (`brainconnect health` is enough) — migrations apply
4. if anything looks wrong, stop and `brainconnect restore --from pre-upgrade.db`

Verified: a ledger authored at the RC1 tag opens intact under current code, and a
genuine pre-ledger schema-v8 database migrates to v9 with every pre-existing claim
preserved and backfilled (`backup_restore_upgrade_demo.py`, PART 3).

---

## 5. Degradation you should expect (fail-closed)

- **A required safety engine is unavailable** → `GET /health` returns `ok:false` and
  names the engine (`safety.required_engines_unavailable`), recall **withholds** any
  item it cannot re-scan (with a warning), and promotion is **refused**
  (`safety_refused`). Unscanned is never treated as clean. Fix the engine (install
  its dependency) or drop `required` — do not expect a retry to succeed.
  (`safety_degrade_demo.py`.)
- **The server is down / a request is dropped** → the adapter surfaces
  `MemoryUnavailable` (retryable), not a crash, and recovers when the server returns;
  the ledger file is unharmed. (`agentconnect_integration_demo.py`.)
- **A bad credential** → `403 forbidden` on every route except `GET /health`, which
  stays open so a probe can still ask "are you degraded?".

## 6. Auth

`brainconnect serve --token <t>` (or `BRAINCONNECT_TOKEN`) requires
`Authorization: Bearer <t>` (or the bare token) on every route except `GET /health`,
compared in constant time. A missing/wrong token is `forbidden`, which the adapter
maps to its "never retry with the same credential" class. Bind to `127.0.0.1` and/or
put a TLS-terminating reverse proxy in front for anything beyond localhost.
