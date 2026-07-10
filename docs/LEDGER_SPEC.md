# LEDGER_SPEC.md — BrainConnect as a trusted memory ledger

Status: **active** (2026-07-10 re-scope). This document is the design contract for
the ledger rework. `BUILD_SPEC.md` remains the origin design; `SCHEMA.md` remains
the living conventions file. Where this document and `BUILD_SPEC.md` disagree about
*scope*, this document wins.

---

## 1. Identity

The product is **BrainConnect**. The Python package, the CLI, and the MCP tool names
still say `wiki` / `brain_*`; that rename is deferred work, tracked in
[STATUS.md](STATUS.md). This document uses both names as they appear in code.

BrainConnect is a **trusted memory ledger and human-readable projection layer** for
agent systems. It specialises in:

source-backed claims · candidate memory capture · pending/promoted/rejected
lifecycle · human/librarian review · provenance · supersession · contradiction
visibility · scoped recall · Obsidian projection · MCP/API access for trusted
context packs.

The **retrieval engine is pluggable**. WikiBrain owns trust and provenance;
retrieval backends own search and indexing sophistication.

> WikiBrain answers: *what do we trust, where did it come from, who promoted it,
> what scope does it apply to, is it current, what superseded it, should this be
> shown to an agent?*
>
> It does **not** answer: *what task is running, which manager owns it, which
> worker should run this subtask, what is the Temporal workflow state, which
> Linear issue is assigned, which local model is loaded?*

## 2. Non-goals

- Not a task ledger. No active tasks, manager claims, Temporal workflow state,
  Linear issue state, worker runs, or live subtask status.
- Agents may **never** promote their own memories.
- **Zero LLM calls in the deterministic core CLI** (unchanged billing/determinism
  boundary). The model-bearing librarian stays a separate process, advisory only.
- Do not dump all agent logs into trusted memory.
- Captured items are not permanent.
- Retrieval results are **not** automatically trusted.
- **Trust is not content safety.** Promotion establishes authority, not that the
  text is free of secrets, PII, or injection payloads. WikiBrain scans capture,
  recall and promotion and can withhold, mask, or block — but no safety engine and
  no safety policy may ever set `trusted`. Safety subtracts; it never vouches.
  Not a secret-scanning, PII, or classifier-training project: detection is
  delegated to modular engines. See [SAFETY.md](SAFETY.md).

## 3. Source-of-truth boundary

| WikiBrain owns | AgentConnect owns | Retrieval backend owns |
|---|---|---|
| sources | active tasks | semantic search |
| claims | subtasks | vector search |
| candidate memories | manager claims | graph traversal |
| claim↔source links | review tickets | temporal graph indexing |
| promotion/rejection decisions | decisions | hybrid retrieval |
| supersession links | attempts | ranking |
| contradiction records | artifacts | |
| recall feedback | Temporal workflow state | |
| Obsidian projection | Linear sync | |
| trusted recall packs | worker runs, route history | |

## 4. Architecture

```
Agent / AgentConnect / CLI / MCP
        ↓
WikiBrain API              (cli/wiki/api.py — the stable facade)
        ↓
trusted memory ledger      (sources, candidates, claims, scopes, supersessions)
        ↓
retrieval backend adapter  (cli/wiki/backends/base.py)
        ↓
SQLite FTS / Graphiti / Cognee / vector DB
        ↓
RecallPack                 (trust + scope filtered, bounded)
```

Agents may **propose** memory. Humans or an approved librarian workflow
**promote/reject**. Promoted memory is trusted by default; pending memory is never
injected unless explicitly requested, and is labeled when it is.

**The load-bearing invariant:** a backend returns *candidates*. WikiBrain applies
trust/status/scope filtering **after** the backend returns and **before** anything
reaches an agent. A backend can never widen trust.

## 5. Core concepts

Integer primary keys are retained throughout (they are load-bearing for FTS5
`content_rowid` and every existing foreign key). The API layer emits **prefixed
string refs** — `claim_4`, `candidate_12`, `source_7` — which are what agents and
the CLI accept and echo. `wiki.refs` converts both ways and accepts a bare integer.

> **Migrations run on every `Repo.open()`**, including the one `build_server()`
> performs. A temp `root=` does not isolate the database — set `WIKIBRAIN_DB`.
> See [MIGRATIONS.md](MIGRATIONS.md).

### 5.1 Source — evidence, not fact

`sources(id, hash, path, title, url, origin, fetched_at, ingested_at, status,
mime_type, category, tags)`

Unchanged. A source is evidence: a document, repo file, Linear issue, AgentConnect
decision, review artifact, user note, test log, web page, manual note. Sources are
never automatically trusted facts.

### 5.2 MemoryCandidate — proposed, not trusted

`memory_candidates(id, text, proposed_by, proposed_by_type, source_id, source_ref,
task_id, proposed_scopes, tags, created_at, reviewed_at, status, promoted_claim_id,
review_reason, metadata)`

`status: pending → (promoted | rejected | archived)`.

`source_id` points at an internal `sources` row (capture always files one, so
provenance is never dangling). `source_ref` carries an **external** pointer such as
`agentconnect_attempt_123` — WikiBrain stores it opaquely and never resolves it.
That is the boundary: AgentConnect owns what an attempt *is*.

**Agents can create pending candidates only.** `capture_candidate` never
auto-promotes.

### 5.3 Claim — promoted or reviewed fact

Existing `claims` gains: `scope_type`, `scope_id`, `tags`, `confidence_label`,
`valid_from`, `valid_until`, `last_verified_at`, `promoted_by`, `candidate_id`.

`status: pending → (promoted | rejected | superseded | contradicted | archived)`.

**Confidence is dual-representation.** The numeric `confidence REAL` is retained
because the auto-gate (`gate.py`) and the contradiction pre-adjudicator compare
against it numerically. The spec's ordinal label lives alongside it in
`confidence_label ∈ {low, medium, high, verified}`. `wiki.confidence` maps between
them (`low=0.3, medium=0.6, high=0.85, verified=0.95`); a label is derived from the
number when absent, so pre-rework claims answer both questions.

Claims are what normal recall returns by default.

### 5.4 ClaimSource — the provenance join

`claim_sources(id, claim_id, source_id, evidence_type, quote_or_pointer, created_at)`

`claims.source_id` (single, NOT NULL) is retained — it is load-bearing for the
renderer, the gate's corroboration count, and every existing query. The join table
adds many-to-many provenance with `evidence_type` and a quote/pointer. On migration
each existing claim gets exactly one join row (`evidence_type='extracted'`).

### 5.5 Scope

Every durable memory is scoped. `scope_type ∈ global | user | project | repo | task
| manager | worker | model | tool`, with a free-form `scope_id`.

Rendered as `repo:mcp-agentconnect`, `model:qwen2.5-coder-14b`, `manager:claude-code`.
`global` carries an empty `scope_id`.

**Recall scope rule.** Given a requested scope set `S`, a claim matches iff
`scope_type = 'global'` **or** `(scope_type, scope_id) ∈ S`. A repo-specific claim
therefore never leaks into another repo's or a global-only recall, while global
facts remain visible everywhere. Pre-rework claims backfill to `global`, preserving
today's behaviour exactly.

### 5.6 Supersession

`supersessions(id, old_claim_id, new_claim_id, reason, created_at, created_by)`

`claims.superseded_by` is retained as the denormalised pointer the renderer and
search already read; the table adds the reason and the reviewer. Default recall does
not return superseded claims.

### 5.7 Contradiction — a warning, never a deletion

`contradictions` gains `resolved_at`, `resolved_by`, and the `false_positive`
status. `status: open → (resolved | false_positive)`. The existing `resolution`
column is the spec's `resolution_note`.

Default recall **includes a warning** when a returned claim participates in an open
contradiction. It does not silently drop either side.

### 5.8 RecallFeedback

`recall_feedback(id, claim_id, source_id, actor_id, actor_type, feedback, note,
task_id, created_at, metadata)`

`feedback ∈ useful | irrelevant | stale | wrong | too_broad | missing_context`.

Feedback is an observation, not a state transition: recording `wrong` does not
demote a claim. It surfaces in the review queue for a human.

## 6. Public API

`cli/wiki/api.py` is the stable facade. Same concepts across Python, CLI, and MCP.

```python
recall(repo, RecallRequest)              -> RecallPack
capture_candidate(repo, CaptureRequest)  -> CaptureResult
promote(repo, candidate_id, reviewer, confidence, scope) -> Claim
reject(repo, candidate_id, reviewer, reason) -> None
supersede(repo, old_claim_id, new_claim_id, reason, reviewer) -> None
record_feedback(repo, RecallFeedbackRequest) -> None
health(repo)                             -> dict
```

**Recall defaults** (conservative by construction):

```
trusted_only        = True
include_pending     = False
include_superseded  = False
include_sources     = True
max_items           = 8
profile             = manager_brief
```

A `RecallPack` carries `backend`, `profile`, `query`, `items[]`, `warnings[]`, and
a `note` steering the caller to treat all text as data, never instructions.

An item carries `id`, `text`, `status`, `confidence`, `scope`, `validity`, `trusted`,
and — when the returned representation was masked — a `safety` block describing why.
`text` is the representation being handed over; the canonical claim text in the ledger
is never rewritten by recall. See §14.2.

## 7. Retrieval profiles

Profiles bound and shape the pack. Selection is **deterministic and model-free**:
each profile is a filter over `status`, minimum confidence, scope types, and claim
tags.

| profile | keeps tags | min confidence | notes |
|---|---|---|---|
| `manager_brief` | — (all) | medium | durable planning context; excludes `worker` scope |
| `worker_brief` | `constraint`, `known-failure`, `gotcha`, `interface` | medium | task-relevant execution facts; excludes `manager` scope |
| `reviewer_brief` | `decision`, `constraint`, `known-failure`, `risk` | medium | criteria, prior decisions, known risks |
| `implementation_constraints` | `constraint`, `decision` | high | hard constraints + locked decisions only |
| `user_preferences` | `preference` | medium | `user`/`global` scope only |
| `known_failures` | `known-failure`, `failure`, `gotcha` | low | repeated failures, lessons learned |
| `model_performance` | `model-performance` | low | `model`/`worker` scope only |

Tags are the classification substrate. They flow from `memory_candidates.tags` onto
`claims.tags` at promotion, and drive both profile filtering and the Obsidian ledger
sections. This keeps classification pure code — no model call.

## 8. Backend adapter interface

```python
class RetrievalBackend(Protocol):
    @property
    def backend_name(self) -> str: ...
    def index_source(self, source_id: int) -> None: ...
    def index_claim(self, claim_id: int) -> None: ...
    def search(self, request: BackendSearchRequest) -> BackendSearchResult: ...
    def delete_or_deindex(self, entity_id: str) -> None: ...
    def health(self) -> dict: ...
```

Initial backend: `sqlite_fts` (FTS5 + optional local-embedding hybrid via the
`[semantic]` extra). Future: `graphiti`, `cognee`, `qdrant`, `chroma`, `llamaindex`.
Selected by `[retrieval] backend` in `config.toml`; unknown names fail loudly at
resolve time, never silently.

`BackendSearchRequest` carries the query, a `limit`, and **hints** (scopes,
statuses). Hints are an optimisation a backend may honour or ignore. WikiBrain
re-applies every trust and scope predicate afterwards regardless — a backend that
ignores or misreads a hint can degrade recall quality but can never widen trust.

**Note.** FTS is also load-bearing for the *write* path — `gate._corroborating_sources`,
`gate._conflicts_with_promoted`, and `ingest._detect_contradictions` use the same
FTS primitives for similarity. Those uses are **not** routed through the backend
seam: gating must stay deterministic and local even when recall is served by a
remote vector store.

## 9. Graphiti integration path

```
WikiBrain promoted claims → Graphiti temporal graph index → retrieval candidates
                          → WikiBrain trust/scope filter → RecallPack
```

Index only: promoted claims, selected sources, supersession relationships,
contradiction relationships, time-scoped project facts.

Never index first: raw chat logs, all worker outputs, pending candidates. Letting
arbitrary agent text into trusted Graphiti recall would launder untrusted content
into trusted memory.

## 10. Cognee integration path

Cognee handles ingestion/retrieval/graph/RAG; WikiBrain handles
trust/promotion/provenance.

- **Mode A — Cognee as backend.** WikiBrain sends promoted claims/sources to Cognee
  for indexing; recall goes WikiBrain → Cognee search → candidates → WikiBrain
  trust/scope filter → RecallPack.
- **Mode B — Cognee as peer.** AgentConnect chooses `WikiBrainMemoryAdapter` or
  `CogneeMemoryAdapter`.

WikiBrain never assumes Cognee is present.

## 11. MCP surface

Small and safe. Agent-facing default: **`brain_recall`, `brain_capture`,
`brain_feedback`** (plus the raw query tools `brain_search`, `brain_hybrid`,
`brain_graph`).

Human-gated, exposed **only** under `wiki mcp serve --review`: `brain_pending`,
`brain_promote`, `brain_reject`.

| mode | tools |
|---|---|
| default | search, hybrid, graph, recall, capture, feedback |
| `--read-only` | search, hybrid, graph, recall |
| `--contribute-only` | capture |
| `--review` | default + pending, promote, reject |

`--review` is mutually exclusive with `--read-only` and `--contribute-only`. The
guard rejects the combination before the FastMCP import, so it holds without the
`[mcp]` extra installed.

## 12. CLI surface

```
wiki recall --query ... --scope repo:my-app --profile manager_brief
wiki capture --text ... --source ... --scope repo:my-app
wiki pending list
wiki pending show candidate_12
wiki promote candidate_12 --scope repo:my-app --confidence high
wiki reject candidate_12 --reason ...
wiki claims show claim_4
wiki claims supersede claim_4 claim_9 --reason ...
wiki feedback claim_4 --feedback stale --note ...
wiki project obsidian
```

`wiki promote` / `wiki reject` are polymorphic on the ref: a `candidate_N` ref (or
`--candidate`) takes the candidate path and requires a scope + confidence; a bare
integer keeps the pre-rework claim path used by the morning gate. This is why the
API emits prefixed refs.

The CLI stays deterministic. Model-bearing work stays in `wiki-librarian`.

## 13. Obsidian projection

First-class. `wiki project obsidian` renders the vault plus `wiki/ledger.md`.

Claim lines carry status, confidence label, scope, validity, supersession link, and
a contradiction warning. Sections:

```
# Project facts        # Known failures                  # Pending candidates
# Decisions            # Model/worker performance        # Sources
# Constraints          # Superseded facts
```

Pending candidates render in their own clearly-labeled review queue — never as
trusted knowledge.

## 14. AgentConnect integration contract

AgentConnect is an **optional** control-plane integration. WikiBrain works
independently as a trusted memory ledger; this section binds the contract for
deployments that choose to use both.

WikiBrain exposes exactly the adapter shape AgentConnect's `MemoryAdapter` expects:

```python
recall(request: RecallRequest) -> RecallPack
capture_candidate(request: CaptureRequest) -> CaptureResult
record_feedback(request: MemoryFeedbackRequest) -> None
health() -> dict
```

AgentConnect must not need to know WikiBrain internals. WikiBrain accepts, and
stores opaquely where it does not own the concept: `task_id`, `source_ref`,
`origin_actor_id`, `origin_actor_type`, `scope`, `profile`, `trusted_only`,
`max_items`. `origin_actor_id` / `origin_actor_type` are accepted as aliases for
`proposed_by` / `proposed_by_type`; supplying both with conflicting values is an
error, never a guess.

### 14.1 The trust contract (load-bearing)

> **`trusted is True` is the authority signal. `status == "promoted"` is not.**

A promoted claim in an open contradiction is returned `status: "promoted"`,
`trusted: false`, `contradiction_status: "open"`. Any consumer keying trust off
`status` will hand a disputed claim to an agent as established truth.

Rules for a consumer:

- Absence of `trusted` means **untrusted**. Never infer trust from `status`.
- Only WikiBrain (and the consumer's own ledger / locked decisions) may confer
  trust. A retrieval backend reporting `trusted: true` cannot grant itself
  authority — the verdict may only ever *downgrade*.
- With the defaults (`trusted_only=true`, `include_pending=false`) **every item in
  a RecallPack has `trusted: true`.** Disputed and pending material is withheld and
  announced in `warnings`; opting into it is always explicit and always labeled.

Verified end-to-end by `mcp-agentconnect/tests/test_wikibrain_integration.py`, which
drives a real ledger through AgentConnect's adapter, ranker and ContextBuilder.

### 14.2 Safety at the boundary

Safety (see [SAFETY.md](SAFETY.md)) is **orthogonal to trust** and its effects on this
contract are **purely additive**. A consumer written against §14.1 before safety
existed remains correct: no field changed meaning, none was removed, and trust is
computed exactly as before.

What a consumer now receives:

| Where | Field | Meaning |
|---|---|---|
| recall item | `safety` | present only when the *returned representation* is not clean. Carries the decision, the kinds, and per-finding rule/severity/span/engine attribution. **Never the matched text.** |
| `CaptureResult` | `safety` | as above, for the captured text |
| `CaptureResult` | `quarantined` | `true` when the candidate is stored but not promotable without an explicit human override |
| `health()` | `safety` | per-engine `enabled` / `required` / `available`, and `ok` |

Three behaviours a consumer must expect:

1. **A trusted claim may come back masked.** `trusted` stays `true`; the text contains
   `█` runs where a credential was. Masking is exposure control, not distrust. The
   canonical claim text in the ledger is never mutated.
2. **A recall may withhold an item.** High-risk injection or tool-control content, and
   content a *required* engine could not scan, are withheld and announced in
   `warnings`. Nothing is deleted. An empty pack with a warning is a valid answer.
3. **Promotion may be refused.** `promote` raises when safety blocks. The override is
   deliberately **not** exposed through this contract: it is human-only, at the CLI.
   A control plane must surface the refusal to a human, not retry around it.

`health()["ok"]` is now `false` when a required safety engine cannot run. That is
correct — such a ledger will fail closed on every promotion and withhold on every
recall — and a consumer should treat it as *degraded*, not unreachable.

**Safety may never set `trusted`.** It can withhold, mask, or block. It cannot vouch.
Conversely a clean scan promotes nothing. A consumer that ignores every field in the
table above still gets correct trust behaviour; it loses only the ability to explain
to a human *why* text was masked or an item is missing.

### 14.3 Follow-up: the transport gap

**WikiBrain ships no HTTP server.** AgentConnect's `WikiBrainMemoryAdapter` expects a
REST service at `http://localhost:8787`:

```
POST /recall            POST /candidates/{candidate_id}/promote
POST /capture           GET  /candidates?status=pending&limit=
POST /feedback          GET  /health
```

The cross-repo integration test closes this today by injecting a `transport` that
dispatches those routes straight into `wiki.api` in-process. That is deliberate and
sufficient for the boundary it tests: it exercises the real ledger, real promotion,
the real trust filter, and the real API field shape. What it does not exercise is
wire plumbing — serialisation, status codes, auth, timeouts.

**`wiki serve` is a separate, later change.** It is tracked here rather than folded
into the semantic work so the trust boundary and the transport surface cannot be
confused for one another: a green integration suite means the *semantics* agree, not
that the network path exists.

## 15. Acceptance criteria

1. Runs with the SQLite FTS backend only.
2. Agents can capture pending memory candidates.
3. Pending candidates are not returned in trusted recall by default.
4. A human can promote a candidate into a scoped claim.
5. Trusted recall returns promoted claims with provenance and scope.
6. Superseded claims are hidden by default.
7. Recall profiles produce different bounded context packs.
8. Feedback can mark a claim useful/stale/wrong.
9. Obsidian projection shows promoted claims, sources, scopes, and the pending queue.
10. AgentConnect can call WikiBrain through a stable recall/capture/feedback API.
