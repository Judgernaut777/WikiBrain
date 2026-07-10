# STATUS.md — where BrainConnect stands

**Stable, and standalone.** BrainConnect is a trusted memory ledger. It runs on its
own, needs nothing else installed, and is not accepting new memory features. Work
from here is stabilisation, documentation, and the deferred items listed below.

Last verified: **2026-07-10**. `main` in sync with origin, working tree clean.

## A note on the name

The GitHub repository is now **`Judgernaut777/BrainConnect`** (the old
`WikiBrain` URL redirects). The **code has not been renamed**: the local checkout is
`WikiBrain/`, the Python package is `wiki`, the CLI is `wiki`, the MCP tools are
`brain_*`, and the isolation variable is `WIKIBRAIN_DB`. Renaming those is deferred
work, tracked below — it would break `mcp-agentconnect`'s integration test, which
imports `wiki.api` by name.

Read "BrainConnect" as the product and "`wiki`" as the module it currently ships as.

## Current checkpoint

| Checkpoint | Commit | What it is |
|---|---|---|
| **Contract tip** | **`221e4f2`** | The consumer contract: fixtures in `tests/contract/`, and the refusal taxonomy in `cli/wiki/errors.py`. Additive only — no behaviour changed. |
| **Behaviour tip** | **`b128e65`** | Memory safety: `cli/wiki/safety/`, enforced at capture, recall and promotion. **The last commit that changed enforced behaviour.** Diff against this when asking "did anything move?" |
| **Trust behaviour** | **`b69e13c`** | `trusted_only` began meaning trusted; disputed claims stopped leaking as trusted. |
| **Tag** | **`v0.1.0-mvp-control-loop`** | Annotated, at `f10569d`. The MVP control-loop checkpoint, taken before safety landed. |

The earlier freeze marker (`c855af9`) and its "docs-only" policy are **superseded**
by this document. That freeze existed to hold the memory contract still during
AgentConnect's dogfood; the contract has since been extended, additively, by safety.

| | |
|---|---|
| Schema version | **9** (`schema.SCHEMA_VERSION == migrate.latest_version()`); unchanged by safety |
| Gate | **552 checks pass, 0 failures** |
| Retrieval backend | `sqlite_fts` (the only one implemented) |
| Transport | in-process Python API + MCP stdio. **No HTTP server** |
| Content safety | enforced at `memory_candidate`, `memory_recall`, `memory_promotion` |
| Safety engines | `baseline` (built in, required) + 5 optional; `gliner` deferred |
| Consumer contract | pinned by fixtures in `tests/contract/` — see [CONTRACT.md](CONTRACT.md) |

Run the gate with:

```bash
PYTHONPATH=/path/to/BrainConnect/cli python3 tests/acceptance.py
```

The gate is **offline**. The only safety engine that runs is the pure-stdlib
baseline; every third-party adapter is exercised through a fake. The count is
identical whether or not `detect-secrets` is installed — verified both ways. A suite
that needs TruffleHog installed is a suite that gets skipped.

---

## What BrainConnect is

A **trusted memory ledger** with a **pluggable retrieval backend**. It owns trust and
provenance; a backend owns search sophistication. Agents propose, humans promote.
Full design: **[LEDGER_SPEC.md](LEDGER_SPEC.md)**.

It owns: claims · candidates · promotion and rejection · provenance · supersession ·
contradictions · scopes · scoped recall · trust decisions · safety policy · the
Obsidian projection.

It does **not** own: task or workflow state, agent routing, model selection, tool
registration, or any live system's runtime. Those belong to the sibling services,
all of which are optional and none of which BrainConnect depends on. See
**[INTEGRATIONS.md](INTEGRATIONS.md)**.

## The trust contract

> **`trusted is True` is the authority signal. `status == "promoted"` is not.**

This is the single rule a consumer must not get wrong. A promoted claim in an open
contradiction is returned `status: "promoted"`, `trusted: false`,
`contradiction_status: "open"` — because a contradiction is a warning, not a deletion,
and the claim remains of record.

- **Absence of `trusted` means untrusted.** Never infer trust from `status`.
- Only BrainConnect — or a consumer's own ledger / locked decisions — may confer trust.
  A retrieval backend reporting `trusted: true` cannot grant itself authority. The
  verdict may only ever **downgrade**.
- **With the defaults (`trusted_only=true`, `include_pending=false`), every item in a
  RecallPack has `trusted: true`.** Disputed, pending and superseded material is
  withheld and announced in `warnings`; opting into any of it is explicit and labeled.
- A backend returns **ids and scores**, never content or status. Recall re-reads every
  authoritative field from the ledger by id. This is what makes the boundary
  structural rather than a matter of discipline.

Stated normatively in [LEDGER_SPEC.md §14.1](LEDGER_SPEC.md).

## Safety

> **`trusted` does not mean safe to expose. `safe` does not mean trusted.**

Promotion establishes *authority*. A scan judges *content*. They are independent, and
**no safety engine and no safety policy may set `trusted`** — the gate asserts this
structurally, by parsing every module in `cli/wiki/safety/` and checking the
identifier appears nowhere in its AST. Safety can withhold, mask, or block. It cannot
vouch.

| Surface | Enforced | Behaviour |
|---|---|---|
| `memory_candidate` | yes | secrets masked **before storage**; injection/tool-control quarantined |
| `memory_recall` | yes | secret in a trusted claim masked on the way out, claim stays trusted; high-risk content withheld and announced; the canonical claim text is never mutated |
| `memory_promotion` | yes | secrets and high-risk payloads block; human override requires a reason and retains the findings |
| `source_ingest` | **no** | specified, deferred |
| `obsidian_projection` | **no** | specified, deferred |

An engine that could not run is never mistaken for one that found nothing: six engine
states (`ok`, `disabled`, `unavailable`, `skipped`, `failed`, `timeout`) are kept
distinct, and a required engine that does not finish `ok` fails closed. Detection is
delegated to modular engines; the built-in baseline is a deliberately limited floor,
not a product. Full contract: **[SAFETY.md](SAFETY.md)**.

Safety is a **second** gate behind the human one, and it can only subtract. Agents
still cannot promote. A clean scan promotes nothing.

## Migration behaviour

**`Repo.open()` runs forward migrations on every open** — including the one
`build_server()` performs at MCP launch. Migrations are forward-only and additive.

**A temporary repo root is not isolation.** `root=` selects which `config.toml` is
read; the database lives at an absolute path *inside* that config. Set **`WIKIBRAIN_DB`**
to a scratch path in tests, scripts and MCP verification. Full detail, the 2026-07-10
incident where a verification script migrated the live database, and the rules for
writing a migration: **[MIGRATIONS.md](MIGRATIONS.md)**.

## Repository boundaries

| Repository | Role | Relationship |
|---|---|---|
| **BrainConnect** (this) | trusted memory ledger | standalone; depends on nothing |
| [mcp-agentconnect](https://github.com/Judgernaut777/mcp-agentconnect) | control plane: tasks, artifacts, decisions, handoffs | **optional** consumer. Contract verified — see below |
| [ComputeConnect](https://github.com/Judgernaut777/ComputeConnect) | local inference / compute | **optional**, not integrated. Notes only |
| [ToolConnect](https://github.com/Judgernaut777/ToolConnect) | tool registry / governance | **optional**, not integrated. Notes only |

BrainConnect never imports any of them, and none of them may write trusted memory:
promotion is human-only, from every direction. Integration notes, including the two
that are not built, live in **[INTEGRATIONS.md](INTEGRATIONS.md)**.

## AgentConnect contract: verified

Re-verified on **2026-07-10** against `mcp-agentconnect@9503661`, after safety landed.
`tests/test_wikibrain_integration.py` passes, 32/32, and a direct probe of the three
seams safety touches confirmed:

- a trusted claim carrying a raw credential crosses the boundary **trusted, with the
  credential masked**; the raw value never reaches AgentConnect, and the canonical
  claim text in the ledger is unchanged;
- an injection payload stored as a promoted claim is **withheld**, and the withholding
  is announced in `warnings`, which AgentConnect passes through;
- promoting a quarantined candidate is **refused across the adapter**;
- `health()` degrades correctly when a required safety engine cannot run.

Trust semantics are unchanged: the ranker still places a promoted, uncontradicted
claim at `WIKIBRAIN_PROMOTED`. Three additive fields BrainConnect emits are dropped by
AgentConnect's mapper — `safety` on a recall item, and `safety` and `quarantined` on a
capture result. That is an observability gap, not a trust or safety hole. All three are
now **pinned by fixtures** so no future consumer can miss them: see
[CONTRACT.md](CONTRACT.md), and [INTEGRATIONS.md](INTEGRATIONS.md#known-gaps) for the
consequences of dropping them.

## Known gap: transport

**BrainConnect ships no HTTP server.** AgentConnect's adapter expects a REST service
at `http://localhost:8787`:

```
POST /recall            POST /candidates/{candidate_id}/promote
POST /capture           GET  /candidates?status=pending&limit=
POST /feedback          GET  /health
```

The integration test closes this with a `transport` that dispatches those routes
straight into `wiki.api` in-process. That is deliberate, and sufficient for the
boundary it tests: real ledger, real promotion, real trust filter, real field shape.
It exercises **no wire plumbing** — no serialisation, status codes, auth, or timeouts.

> **A green integration suite means the semantics agree, not that the network path
> exists.**

`wiki serve` is the deferred follow-up that closes it, tracked separately on purpose
so the semantic boundary and the transport surface cannot be confused for one another.

## Deferred work

Ordered by how much each one blocks. Nothing here is started.

1. **`brainconnect serve`** — the HTTP transport above. The only item another
   repository is waiting on. Its refusal envelope and status codes are already
   specified and tested; see [CONTRACT.md](CONTRACT.md#refusal-semantics).
2. **`source_ingest` safety surface** — scan raw source documents on the way in, with
   the whole-file engines promotion already uses. This becomes load-bearing the moment
   anything ingests third-party text; see the ToolConnect notes.
3. **`obsidian_projection` safety surface** — redact before writing markdown. Lowest
   urgency of the three: the projection is regenerable from the database, so it is the
   one surface where a miss is repairable.
4. **GLiNER** as a custom Presidio recognizer, for PII recall above Presidio's own
   field-limited ~0.5 F1. Deliberately absent from the engine registry until it exists.
5. **Retrieval backends** beyond `sqlite_fts` — `graphiti`, `cognee`, `qdrant`,
   `chroma`, `llamaindex` are named in the registry and fail loudly.
6. **The code rename** — package `wiki` → something matching BrainConnect, CLI entry
   point, `WIKIBRAIN_DB`, the `brain_*` MCP tool names. Coordinate with
   `mcp-agentconnect`, which imports `wiki.api` by name.

## Change policy

No new memory features. Do **not** add: recall profiles, retrieval backends, MCP
tools, promotion paths, recall semantics, ingestion behaviour, schema columns, or
`wiki serve`.

Code changes are in scope only for a concrete:

- **field-shape mismatch** (a consumer needs a field recall does not emit, or emits
  differently),
- **trust, scope, or safety mismatch** (two repositories disagreeing about what is
  visible, trusted, or safe), or
- **migration issue**.

Everything else is documentation.
