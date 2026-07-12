# BrainConnect as a deterministic orchestration layer — lane plan

This is the sequenced execution plan for the "BrainConnect as the deterministic
orchestration layer above Decima" epic. It is governed by
[ADR 0008](adr/0008-orchestration-boundary.md), which decides — per capability — what
BrainConnect (BC) **owns** versus **delegates** to ComputeConnect (CC), AgentConnect (AC),
and Decima (D). The one-line rule: **BC reasons about capabilities and records decisions;
it never re-implements routing, placement, scheduling, worker orchestration, or the
observability stream.**

Ownership legend: **A** = BC-native new · **B** = delegate to an existing CC/AC/Decima
contract · **C** = thin BC adapter over an existing contract · **D** = genuinely missing
everywhere.

## Lanes (dependency-ordered)

| Lane | Capability | Ownership | Owner | First deliverable | Depends on |
|---|---|---|---|---|---|
| 1 | Capability registry + tier hierarchy (small → general-doc → high-capability-local → frontier-managers) + preferred-model declaration | **A** (trusted registry) + **B** (runtime tiers to AC/CC) | BC | Capability-claim schema bound to LEDGER_SPEC §7 `model_performance` + §5.5 `model:`/`worker:` scope, seeded with the tier hierarchy as **metadata** claims. `Qwen3.6-35B-A3B` recorded as the *declared preferred* high-capability local tier (no numbers); `qwen3-30b-a3b` as the *deployed* tier. Human/librarian-only promotion. **No benchmark numbers.** | none (foundation) |
| 2 | Published Decima capability-reasoning read-contract (planning/approvals/workspaces/knowledge/agents/artifacts) | **B** (surface lives in Decima) | Decima (BC consumes) | A versioned read-contract *in the Decima repo* stabilizing `projections.{tasks,approvals,agents,knowledge,activity}` with `instruction_eligible` exposed. BC codes against the contract, not Python objects. | none (parallel to L1) |
| 3 | Transport for the registry (BC↔AC/CC memory link, `:8787`) | **A** (claim endpoint) + **B** (consumption) | BC + AC | Wire LEDGER_SPEC §14.3 `:8787` so AC's `RoutingEngine` can pull BC *trusted* capability claims as a routing input (a trusted source instead of self-conferred `learned_quality`). BC serves read-only trusted claims; AC weights them. | L1 |
| 4 | Capability router + warm-aware swap-minimizing scheduler | **B** (fully) | AC (routing/residency) + CC (placement) | A thin BC delegation trigger that assembles a request from trusted claims + knowledge context, calls AC `RoutingEngine.route` and CC `/route/estimate`, and records the returned decision + rationale as trusted provenance. **Zero routing/placement math in BC.** | L3, L2 |
| 5 | Unified knowledge abstraction (adapters → WikiBrain → graph → OKF → external), federating Decima knowledge | **A** (core) | BC | Extend LEDGER_SPEC §8 `RetrievalBackend` federation with a Decima-knowledge backend that reads `projections/knowledge.py` via the L2 contract and honors `instruction_eligible` exactly as BC honors `trusted`. **Federate, do not fork.** | L2 |
| 6 | Multi-model collaboration roles (planning/coding/reviewer/verifier/docs) + independent verification | **B** (fully) | AC (D executes) | BC maps a plan's role requirements to existing AC model-manager profiles (`general_coder`/`coding_specialist`/`review_worker`/`critic`) and triggers AC `RouterService` decompose→execute→synthesize with `review.*` lifecycle; BC records the role-assignment as provenance. **No role engine/verifier in BC.** | L4 |
| 7 | Performance (prompt caching, benchmarking, telemetry, queue analytics, load prediction) feeding the registry | **B** (measurement) + **A** (capture/promote loop) | CC/AC (measure) → BC (trusted capture) | A capture adapter ingesting CC/AC telemetry + benchmark outcomes as **pending** capability candidates (never auto-promoted), closing the loop into L1. First measured run against the live `qwen3-30b-a3b` node produces the first real `model_performance` numbers (as candidates awaiting human promotion). | L1, L4 |
| 8 | Observability (queued work, active agents, utilization, provider health, routing decisions, timelines, token accounting, swap history) | **B** (fully) | AC (event model) | BC emits its orchestration decisions (registry promotion, delegation trigger, role assignment) **into** the existing `AgentObservabilityProvider` using the shipped `EventType` vocabulary. **No parallel event stream/timeline/token ledger in BC.** | L4, L6 |

## Binding prohibitions (from ADR 0008)

- Do **not** re-implement ComputeConnect's placement engine (`select_placement`, warm-state
  table, queue-seconds math, provider snapshot). Call CC `/route/estimate`, read the rationale.
- Do **not** re-implement AgentConnect's capability router (`capability_overlap`, eligibility
  gating, `RoutingEngine.route`). Call AC, record the returned `RoutingDecision`.
- Do **not** re-implement AC's swap-minimizing residency policy (`residency_bonus`,
  `model_switch_penalty`, `queue_delay_penalty`, `min_batch_size_for_switch`).
- Do **not** re-implement AC's delegation/roles/governor (recursive decompose→execute→
  synthesize, `review.*`, reviewer/critic/verifier, worker spawning).
- Do **not** define a parallel observability event model. Emit into AC's
  `AgentObservabilityProvider` with the existing vocabulary.
- Do **not** touch Decima's execution/authorization internals (Weft, leases,
  `capability_proof`, `implementation_digest`, worker IPC). Submit intent; read projections.
- Do **not** fork Decima's knowledge ledger. Federate over `projections/knowledge.py` and
  honor `instruction_eligible`.
- Do **not** let any model/agent promote a capability claim (about itself or any model).
  Promotion is human/librarian-only (LEDGER_SPEC §2). AC's self-conferred `learned_quality`
  is an **input candidate**, never a trusted claim.
- Do **not** hold live orchestration state in BC (active tasks, worker runs, loaded model).
  BC records decisions and captures candidates; it never re-holds runtime state.
- Do **not** make any provider a required dependency. The registry and knowledge planes
  function with zero models loaded.

## Cross-repo dependencies & open questions (do not resolve unilaterally inside BC)

1. **Model name** — `Qwen3.6-35B-A3B` (brief) exists nowhere; deployed model is
   `qwen3-30b-a3b`. Handled by the registry as declared-preferred vs deployed (see ADR 0008
   "Model-name reconciliation"); a one-word user correction updates the preference claim.
2. **`:8787` transport ownership (L3)** — LEDGER_SPEC §14.3 defines the link; the
   trust-boundary note records BC has no HTTP server while the AC adapter expects `:8787`.
   Open: does BC gain a minimal read-only HTTP surface for trusted claims, or does AC pull
   via the existing MCP surface?
3. **Decima read-contract authorship (L2)** — the projections exist and look stable, but the
   versioned external contract belongs *in the Decima repo*; whether this epic authorizes
   BC's lead to open that contract there is a cross-repo governance question.

## Status

- **Lane 0 (boundary ADR):** ✅ complete — ADR 0008 accepted.
- **Lanes 1–8:** planned. Lane 1 is the unblocked foundation and is next.
