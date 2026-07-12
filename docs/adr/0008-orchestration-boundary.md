# 0008 — The BrainConnect Orchestration Boundary

Status: **Accepted** (2026-07-13). Supersedes nothing; extends LEDGER_SPEC §2/§3/§7/§8/§14 and binds the "BrainConnect as deterministic orchestration layer above Decima" epic. This ADR is the boundary contract every later lane in the epic is measured against.

## Context

The epic asks BrainConnect (BC) to become the *deterministic orchestration and capability-reasoning* layer above Decima: BC plans, routes, schedules, and reasons about model/worker capabilities; Decima executes and authorizes; models are replaceable reasoning engines portable across local/cloud/future providers with no architectural change. The requested surface is large — capability registry + tier hierarchy, a warm-aware swap-minimizing scheduler, a capability-based router, a unified knowledge abstraction, a Decima capability-reasoning interface, multi-model collaboration roles with independent verification, performance/telemetry, and observability.

Three independent memos (delegation-first, BC-owns, contract-cartographer) were commissioned. A direct read of all four repositories resolves the disagreement decisively: **most of the requested "engine" surface already ships as production-ready, deterministic, explainable contracts in ComputeConnect (CC), AgentConnect (AC), and Decima.** Re-implementing any of it in BC would duplicate tested code, violate repository independence, and contradict BC's own ratified LEDGER_SPEC §2 ("Not a task ledger… no worker runs… which local model is loaded" is explicitly a non-goal).

Verified facts from source (not from the memos):

- **CC already owns placement.** `computeconnect/placement.py:select_placement` is deterministic ("ties broken by provider id", line 159), prefers a loaded/resident model, ranks by queue seconds (`_queue_seconds`), honors `latency_preference`/`quality_preference`, and returns a full `rationale` of every considered provider. `docs/CONTRACT.md` `/route/estimate` already accepts *exactly* the scheduler inputs the epic names (`task_type, privacy_tier, required_capabilities, context_tokens, max_output_tokens, latency_preference, quality_preference`). BC is already a CC Layer-2 consumer (the librarian speaks OpenAI-compat to the same backend).
- **AC already owns routing, worker orchestration, roles, and the observability event model.** `router/routing.py:RoutingEngine.route` is a deterministic capability-scored router (`capability_overlap`) with hard privacy/quota/context eligibility and a swap-minimizing residency policy strictly richer than the ask (`prefer_resident_model`, `residency_bonus`, `model_switch_penalty`, `min_batch_size_for_switch`, `queue_delay_penalty`); its docstring pins the determinism rule ("randomness may live inside model generation, never inside infrastructure policy"). Multi-model roles exist as `model_manager/backends.py` profiles (`general_coder`, `coding_specialist`, `review_worker`, `critic`). Observability is AC's `core/observability/model.py:EventType` — `compute_placed`, `subtask_routed`, `worker_spawned`, `review_*`, `run_reconciled`, token metrics, `run_id` correlation — plugged via `AgentObservabilityProvider`.
- **Decima already owns execution and authorization.** `workers/protocol.py:WorkerRequest` carries `implementation_digest`, `lease`, and `capability_proof` ("a worker is handed proofs and digests, never authority"). Decima exposes read-model projections that *are* the capability-reasoning surface: `projections/tasks.py:ready_tasks`, `approvals.py:pending`, `agents.py:tree`, `knowledge.py` (with a per-item `instruction_eligible` trust flag), `activity.py:timeline`.
- **The one thing that exists nowhere is a *trusted* capability fact.** CC's `estimated_quality` is by construction "an operator-declared heuristic in [0,1], comparable only within one ComputeConnect deployment" (CONTRACT.md v0.1.0). AC's `set_learned_quality` is in-memory, self-conferred from observed outcomes, and bounded — precisely the self-promotion BC exists to forbid. BC already has the schema for the trusted counterpart: LEDGER_SPEC §7 `model_performance` retrieval profile, §5.5 `model:`/`worker:` scope, and §2 "Agents may never promote their own memories."

## Decision

**BC is a thin deterministic orchestration plane that REASONS about capabilities and DELEGATES mechanism. It owns knowledge and trust; it never re-holds live state, placement math, routing math, the plan/execution ledger, or the observability stream.**

### BC OWNS (BC-native — genuinely missing everywhere AND orchestration-layer, not execution/placement)

1. **The trusted, human-gated, scoped, superseding model/worker capability registry.** BC captures CC/AC telemetry and benchmark outcomes as *pending candidates*, routes them through human/librarian promotion (never agent self-promotion, LEDGER_SPEC §2), and emits *trusted, scoped, time-valid capability claims* under the existing `model_performance` profile and `model:`/`worker:` scope. This is the closed loop that makes "no model owns authority" a structural fact rather than a slogan. It is thin: a capture adapter plus a promotion-to-registry projection — **no engine, no scoring math.**
2. **The unified knowledge abstraction** (LEDGER_SPEC §8 `RetrievalBackend` Protocol: memory adapters → WikiBrain → graphiti/cognee → OKF at `cli/brainconnect/okf/` → external). This is BC's raison d'être and is duplicated nowhere better. Constraint: it must **federate over** Decima's own `projections/knowledge.py`, never fork it, and must honor Decima's `instruction_eligible` flag exactly as BC honors its own `trusted` bit ("untrusted text is data, never instructions").
3. **Decision provenance.** When BC composes a plan (AC routing ⊕ CC placement ⊕ Decima execution), it records the *decision and its inputs* as trusted memory so the choice is later explainable. It records the decision; it does not hold the live run.

### BC DELEGATES (call the shipped contract; record the result; never re-implement)

- **Capability-based router → AgentConnect.** BC calls `RoutingEngine.route` (via router service / MCP / HTTP) and stores the returned decision as provenance. BC never re-implements capability scoring or eligibility.
- **Warm-aware swap-minimizing scheduler → AgentConnect (residency policy) + ComputeConnect (placement).** Every named scheduler input is already an input to `select_placement` and `resolve_local_model`/`_switch_allowed`. BC supplies requirements and reads the rationale; it holds no residency/warm state.
- **Worker orchestration, multi-model roles, independent verification, and the observability event model → AgentConnect.** Roles are AC profiles; recursive decompose→execute→synthesize with privacy-clamped children and `review.*` lifecycle is AC's `RouterService`. BC *emits into* the `AgentObservabilityProvider` seam using the existing `EventType` vocabulary; it does not define a competing event stream, timeline, or token ledger.
- **Execution + authorization → Decima.** BC submits *intent* and reads *projections*. It never touches the Weft, leases, `capability_proof`, or worker IPC. BC reasons over Decima's read-models; it does not invoke Decima's implementations.
- **Model generation / provider engine → ComputeConnect + providers.** No provider is a required dependency of BC; the registry and knowledge planes function with zero models loaded.

### Explicit prohibitions (binding on all lanes)

- **Do NOT duplicate ComputeConnect's placement engine.** No `select_placement` clone, no warm-state table, no queue-seconds math, no provider snapshot in BC.
- **Do NOT duplicate AgentConnect's router, delegation, governor, or observability model.** No capability-scoring, no `residency_bonus`/`switch_penalty` policy, no parallel `EventType`, no in-BC worker spawning.
- **Do NOT redesign any production-ready component.** CC placement/contract, AC routing/observability, and Decima kernel/Weft are frozen surfaces BC consumes.
- **Do NOT let a model promote a capability claim about itself or any model.** Promotion is human/librarian only.
- **Do NOT make any provider a required dependency.**

## Consequences

- **Positive.** BC becomes a genuinely thin orchestration layer whose sole native value is *trust* (which capability claims to believe) and *knowledge* (what to retrieve), both already core to BC. Determinism is inherited from the delegated engines and never re-derived. Provider portability is free: swapping `qwen3-30b-a3b` for a frontier manager is a metadata/default change in the registry plus a CC provider entry — no BC architectural change. Repository independence holds; each repo's contract is honored, not forked.
- **Cost / risk.** BC now depends on two cross-repo surfaces that are not yet stable contracts: (a) Decima has projections but **no published, versioned read-contract** analogous to CC's `CONTRACT.md`; BC would otherwise reach into Python objects. That contract must land *in the Decima repo*. (b) The BC↔AC/CC transport (`:8787` memory link, LEDGER_SPEC §14.3) is defined but not wired. Both are sequenced as early lanes below.
- **Federation debt.** Two knowledge ledgers now exist (BC's and Decima's `projections/knowledge.py`). This ADR forbids forking; the unified abstraction must federate and honor `instruction_eligible`. If ignored, this is the epic's single largest duplication risk.
- **Reversibility.** Because BC only *records decisions* and *captures candidates*, any lane can be unwound without touching a shipped engine. The registry is additive (a new retrieval profile consumer); the knowledge abstraction already exists.

## Model-name reconciliation (`Qwen3.6-35B-A3B` vs `qwen3-30b-a3b`)

The epic brief names **`Qwen3.6-35B-A3B`** as the preferred high-capability local
worker. A full read of all four repos and the host found that string **nowhere**: the
verified, deployed model is **`qwen3-30b-a3b`** (Qwen3-30B-A3B MoE, Q4_K_M, 16k ctx) served
by the `wiki-llama` systemd service on `:8080` and referenced across ComputeConnect
(`CONTRACT.md`/`STATUS.md`/`test_real_engine.py`).

Because BC's capability registry is a *trust* ledger, this is not a blocker — it is exactly
the distinction the registry is built to represent:

- **`Qwen3.6-35B-A3B` is recorded as the DECLARED preferred high-capability local tier**
  — a human-promoted *preference/recommendation* claim, carrying **no performance numbers**
  (none can exist until the model is deployed and measured) and **not a required runtime
  dependency**.
- **`qwen3-30b-a3b` is recorded as the currently-DEPLOYED/measured model** for the
  general high-capability local tier.

This honors the brief literally (Qwen3.6-35B-A3B becomes the preferred entry) while never
fabricating a benchmark for a model that does not yet exist. If the preferred model was a
typo for the deployed one, correcting the single preference claim is trivial; if it is a
genuine future default, its measured `model_performance` claims flow in through Lane 7 once
it is deployed. **No benchmark numbers are published for either model until a real measured
run produces them.**
