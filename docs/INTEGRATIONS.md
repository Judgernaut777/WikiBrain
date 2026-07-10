# INTEGRATIONS.md — BrainConnect and the services around it

BrainConnect is a **standalone** trusted memory ledger. It imports nothing from the
services below, runs without any of them, and is not made more correct by their
presence. This document exists so that integrations stay optional and stay honest.

One rule governs every integration on this page, present and future:

> **No external service may write trusted memory.** Every consumer proposes; a human
> promotes. There is no API, no flag, and no actor type that lets a service promote
> its own memory, and none will be added.

Two consequences follow, and they are the reason the rule is worth stating:

- A service may capture as many candidates as it likes. Nothing it captures is
  trusted until a human says so, so a compromised or merely enthusiastic service
  degrades the review queue, never the trusted set.
- A service may read a recall pack, but `trusted` is BrainConnect's verdict. A
  consumer may **downgrade** it — refuse to act on something BrainConnect trusts —
  and may never upgrade it. See [LEDGER_SPEC.md §14.1](LEDGER_SPEC.md).

| Service | Role | Status |
|---|---|---|
| [mcp-agentconnect](https://github.com/Judgernaut777/mcp-agentconnect) | control plane: tasks, artifacts, decisions, handoffs | **integrated**, contract verified |
| [ComputeConnect](https://github.com/Judgernaut777/ComputeConnect) | local inference and compute | **not integrated** — notes only |
| [ToolConnect](https://github.com/Judgernaut777/ToolConnect) | tool registry and governance | **not integrated** — notes only |

---

## AgentConnect — integrated, verified

AgentConnect decides **context injection**: which bounded pack a manager or worker
sees. BrainConnect decides **trust**. Cognee and Graphiti supply breadth and temporal
recall and are authorities over neither.

The adapter binds to four methods, and nothing else:

```python
recall(RecallRequest)              -> RecallPack
capture_candidate(CaptureRequest)  -> CaptureResult
record_feedback(MemoryFeedbackRequest) -> None
health()                           -> dict
```

Plus, for the human review path only: `promote_candidate`, `list_pending`.

### Verified 2026-07-10

Against `mcp-agentconnect@9503661`, after safety landed.
`tests/test_wikibrain_integration.py` passes **32/32**, and a direct probe through the
real adapter, ranker and `ContextBuilder` confirmed each of the following:

| Property | Result |
|---|---|
| **Trust fields** | A promoted, uncontradicted claim arrives `trusted: true` and ranks at `WIKIBRAIN_PROMOTED`. `trusted` is read from the payload, never recomputed from `status`. A contradicted claim arrives `status: "promoted", trusted: false, contradiction_status: "open"` and is dropped from a trusted-only pack. |
| **Scopes** | `global` carries an empty `scope_id`; every other type requires one. Per-profile scopes are honoured; a repo-scoped claim does not reach a global-only recall. |
| **Recall behaviour** | Defaults are conservative (`trusted_only`, no pending, no superseded, 8 items). Withheld material is announced in `warnings`, which the adapter passes through verbatim. |
| **Promotion** | Human-gated. An agent `reviewer_type` is refused at the ledger, not merely hidden by the MCP surface. `confidence` and `scope` are never guessed. |
| **Candidate capture** | `origin_actor_id` / `origin_actor_type` are accepted as aliases for `proposed_by` / `proposed_by_type`. `task_id` and `source_ref` are stored opaquely and never resolved. |
| **Safety interaction** | A raw credential in a trusted claim **never crosses the boundary**: the item arrives `trusted: true` with the credential masked, and the canonical claim text in the ledger is unchanged. An injection payload stored as a promoted claim is **withheld**, with a warning. Promoting a quarantined candidate is **refused across the adapter**. `health()` reports `degraded` when a required safety engine cannot run. |

Safety's effect on this contract is **purely additive** — no field changed meaning and
none was removed. Full detail: [LEDGER_SPEC.md §14.2](LEDGER_SPEC.md).

### Known gaps

Neither is a trust or safety hole. Both are **observability** gaps, both live on the
AgentConnect side of the seam, and neither warrants a change in BrainConnect.

1. **`CaptureResult` drops `safety` and `quarantined`.** BrainConnect returns both;
   AgentConnect's `CaptureResult` has a `metadata` field but the adapter does not
   populate it from them. A quarantined candidate is therefore structurally
   indistinguishable from a clean pending one — the quarantine is announced only in
   the human-readable `message`. The consequence is mild but real: a later
   `promote_candidate` on that candidate raises rather than being pre-filtered, and an
   operator view listing pending candidates cannot flag the dangerous ones.
   *`list_pending` is unaffected: it returns raw candidate dicts, so `metadata.quarantined` and `metadata.safety` are visible there.*

2. **Recall items drop the `safety` block.** The adapter builds `MemoryItem` from an
   enumerated set of keys and `safety` is not among them. A manager therefore sees `█`
   runs in a claim with no per-item explanation of why. The pack-level warning does
   pass through and does say so, but it points the reader at "each item's `safety`
   field" — a field that, on the AgentConnect side, is not there.

Both are fixed by copying two keys into `MemoryItem.metadata` and `CaptureResult.metadata`.
That is AgentConnect's change to make; this handoff explicitly forbids making it here.

### Transport

BrainConnect ships **no HTTP server**. AgentConnect's `WikiBrainMemoryAdapter` expects
REST at `http://localhost:8787`. The integration test injects an in-process transport
into `wiki.api` instead, which exercises the real ledger and the real field shape but
no wire plumbing — no serialisation, status codes, auth, or timeouts.

> **A green integration suite means the semantics agree, not that the network path exists.**

`wiki serve` closes this and is deferred. It is the only item another repository is
waiting on. See [STATUS.md](STATUS.md).

---

## ComputeConnect — possible future interactions

**Nothing is implemented. Nothing should be, without a separate decision.**

ComputeConnect owns compute: which model is loaded, on what hardware, how a run was
executed. BrainConnect owns durable, human-vetted facts *about* that. The line is the
difference between a gauge and a lesson.

> ComputeConnect answers *what is running, and how fast.*
> BrainConnect answers *what we concluded, and who signed off.*

BrainConnect is **not** a metrics store, a time-series database, or a run log. Dumping
telemetry into it is an explicit non-goal ([LEDGER_SPEC.md §2](LEDGER_SPEC.md)):
volatile, high-volume, machine-generated data would drown the review queue that makes
promotion meaningful, and a human cannot vet ten thousand latency samples.

### What would fit

The existing schema already carries these. **No new scope type, tag, profile, or
column is needed**, which is the strongest argument that the boundary is drawn in the
right place.

| Interaction | How it maps today |
|---|---|
| **Model-performance observations** | A candidate scoped `model:<id>` or `worker:<id>`, tagged `model-performance`. The existing `model_performance` recall profile already filters on exactly that tag and admits exactly the `global`, `model`, `worker` scope types. |
| **Compute metadata** | A claim scoped `model:<id>` — context window, quantisation, a known failure mode under load. Durable, low-volume, worth a human's signature. |
| **Execution provenance** | A `source` row. A source is *evidence*, not fact: a run log, a benchmark artifact, an eval report. `source_ref` carries an opaque external pointer (`computeconnect_run_412`) which BrainConnect stores and never resolves. |

The distinction that keeps this honest: *"qwen3-30b served 41 tok/s at 14:02"* is
telemetry and belongs in ComputeConnect. *"qwen3-30b silently truncates past 32k
context on this box; prefer the 14b for long files"* is a lesson, was learned once,
and is worth promoting.

### What it would take

Very little on this side, and that is the point.

- ComputeConnect captures candidates through the same `capture_candidate` contract
  AgentConnect uses. Tags and scopes are *proposals*; the human who promotes chooses
  the final scope and confidence.
- Promotion stays human. An automated benchmark cannot promote its own result, for the
  same reason an agent cannot promote its own memory.
- Recall of model facts uses the existing `model_performance` profile.
- BrainConnect must not learn to reach ComputeConnect. If a recall needs live compute
  state, the *caller* joins the two; the ledger does not.

### Watch for

- **Volume.** A per-run capture rate will bury the review queue. Aggregate first, or
  capture only on a state *change* — a regression, a new failure mode.
- **Staleness.** Model facts expire when the model or the box changes. `valid_until`
  and supersession exist for this; a promoted claim about a model that is no longer
  served should be superseded, not deleted.
- **Scope leakage.** `model:<id>` scoped claims must not be promoted to `global`
  because they happened to be true on one machine.

---

## ToolConnect — possible future interactions

**Nothing is implemented. Nothing should be, without a separate decision.**

ToolConnect owns the tool registry: what tools exist, their schemas, who may call
them. BrainConnect owns vetted notes *about* tools, and the governance decisions that
were made about them.

> ToolConnect answers *what tools exist, and what they accept.*
> BrainConnect answers *what we learned about using them, and what we decided.*

BrainConnect is **not** a tool registry, a schema store, or a permission system. A
tool's schema is live configuration; it changes without a human in the loop, and it
must never be served from a store whose whole premise is that a human vetted it.

### What would fit

Again, the existing scope vocabulary already covers it. `tool` is a scope type today.

| Interaction | How it maps today |
|---|---|
| **Tool documentation** | A `source` — evidence, not fact. Ingested, never automatically trusted. The librarian may *draft* candidate claims from it; a human promotes. |
| **Trusted tool notes** | A claim scoped `tool:<name>`, tagged `gotcha`, `known-failure`, `interface`, or `constraint` — the tags `worker_brief` already filters on, so an executing agent receives them without a new profile. |
| **Governance records** | A claim scoped `tool:<name>` or `global`, tagged `decision`. `reviewer_brief` already surfaces `decision` and `constraint`. A revoked tool is a **superseded** claim, not a deleted one — the decision remains of record with its reason and reviewer. |

### The safety dependency, and it is a real one

Tool documentation is **third-party text that a model will read**. That is precisely
the prompt-injection carrier this ledger's safety layer exists to contain, and it is
the case that turns a deferred item into a blocker.

BrainConnect scans `memory_candidate`, `memory_recall`, and `memory_promotion` today.
It does **not** scan `source_ingest` — the surface where a tool's README enters the
system. See [SAFETY.md](SAFETY.md).

> **`source_ingest` should be implemented before ToolConnect ingests third-party tool
> documentation at scale.** Until then, injected text in a tool's docs is caught at
> promotion (a human sees it flagged) and at recall (high-risk content is withheld),
> but not on the way in — so it sits in the `sources` table and in the `inbox/`
> artifact, unmasked, where any other reader may find it.

Note the ordering that already protects the trusted set: a claim drafted from poisoned
tool docs is a *candidate*. It is quarantined at capture if the payload survives into
the claim text, blocked at promotion, and withheld at recall. What is missing is
containment of the raw source, not of the trusted claim.

### What it would take

- ToolConnect files tool documentation as **sources**, not as claims.
- Tool notes reach agents through the existing `worker_brief` profile; governance
  decisions through `reviewer_brief`. No new profile.
- Revocation is supersession, with a reason and a reviewer. Nothing is deleted.
- BrainConnect must not learn to reach ToolConnect, and must never serve a tool schema.
  A schema that changed since a human vetted it is a lie with a signature on it.

---

## Rules for any future integration

1. **Optional.** BrainConnect must build, test, and run with the service absent.
2. **No inbound trust.** Propose; never promote. There is no exception.
3. **No new schema without a new decision.** If an integration seems to need a column,
   a scope type, or a profile, first check whether it is trying to store live state in
   a ledger of vetted facts. It usually is.
4. **Evidence is a source; a conclusion is a claim.** Logs, reports, docs, and
   artifacts are sources. What a human concluded from them is a claim.
5. **Opaque pointers, never resolution.** `task_id`, `source_ref` and their kin are
   stored verbatim and never dereferenced. The other service owns what they mean.
6. **Third-party text is untrusted data, never instructions** — and, until
   `source_ingest` lands, it is also unscanned on the way in.
