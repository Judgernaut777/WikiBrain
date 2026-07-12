# REGISTRY.md — the trusted model/worker capability registry

Status: **active** (ADR 0008 Lane 1, 2026-07-13). Governed by
[ADR 0008](adr/0008-orchestration-boundary.md) and
[ORCHESTRATION.md](ORCHESTRATION.md). Binds to
[LEDGER_SPEC.md](LEDGER_SPEC.md) §2 (promotion is human-only), §5.5
(`model:`/`worker:` scope), and §7 (the `model_performance` retrieval profile).

Code: `cli/brainconnect/registry.py`. CLI: `brainconnect registry`.

---

## 1. What this is (and is not)

BrainConnect's capability registry is a **trust/knowledge artifact, not an
engine.** It is the one genuinely-missing orchestration-layer thing ADR 0008
found nowhere else: a *trusted* capability fact about a model or worker.

- ComputeConnect's `estimated_quality` is an operator-declared heuristic,
  comparable only within one deployment.
- AgentConnect's `learned_quality` is self-conferred from observed outcomes —
  exactly the self-promotion LEDGER_SPEC §2 forbids.

The registry is the closed loop that makes *"no model owns authority over a
capability claim about itself"* a structural fact rather than a slogan: capability
facts enter as ordinary **pending** BrainConnect candidates and can only become
trusted through the existing **human/librarian** promotion gate.

It contains, by ADR-0008 mandate, **no routing math, no placement math, no
scheduler, and no residency/warm-state.** Those ship in AgentConnect
(`RoutingEngine.route`) and ComputeConnect (`select_placement`) and are delegated,
never duplicated here. The registry adds *structure* (the tier hierarchy) and a
*query*; it invents no new persistence — capability facts are claims in the one
ledger, under the `model_performance` profile and `model:`/`worker:`/`global`
scope.

## 2. The tier hierarchy (data-driven)

The hierarchy is **data**, in `SEED_TIERS` (`cli/brainconnect/registry.py`). No
code branches on a tier name; every read surface walks the seed structure. The
preferred model for a tier is therefore swappable by editing one data field —
the ADR-0008 provider-portability promise: **no BrainConnect architectural change
to swap models.**

| ordinal | tier | required capabilities | provider binding |
|---|---|---|---|
| 1 | `small` | classification, routing-hints, short-extraction | ComputeConnect |
| 2 | `general-doc` | summarization, doc-qa, tagging | ComputeConnect |
| 3 | `high-capability-local` | code, reasoning, tool-use, long-context | ComputeConnect |
| 4 | `frontier-managers` | planning, delegation, adjudication, verification | AgentConnect |

The *provider binding* is a delegation target, not an engine: BrainConnect never
loads a model or routes to a provider. The *required capabilities* are
requirements, not a score — the eligibility/ranking math lives in AgentConnect.

## 3. Preferred vs deployed (the model-name reconciliation)

The `high-capability-local` tier names two distinct models, and the distinction
is the whole point of a *trust* ledger (see ADR 0008 "Model-name reconciliation"):

- **Declared PREFERRED:** `Qwen3.6-35B-A3B`. A **preference/recommendation only**.
  It carries **no benchmark numbers** (none can exist until it is deployed and
  measured) and is **not a required runtime dependency**.
- **Currently DEPLOYED:** `qwen3-30b-a3b` — the real model on the `wiki-llama`
  `:8080` node. BrainConnect records that it is deployed; it does not connect to
  it.

**No benchmark numbers are published for either model.** None have been measured.
Fabricating any is forbidden. When the preferred model is deployed and a real
measured run exists, its `model_performance` numbers flow in through Lane 7 as
their own candidates — awaiting human promotion, like everything else.

If `Qwen3.6-35B-A3B` turns out to be a typo for the deployed model, correcting it
is a one-field edit in `SEED_TIERS` plus re-promotion — no code change.

## 4. How a fact becomes trusted

Seeding files each tier/model fact as a **pending** candidate (LEDGER_SPEC §5.2)
under the `model_performance` profile and the right scope:

- tier metadata → `global` scope, tag `model-performance`;
- preferred/deployed model → `model:<name>` scope, tag `model-performance`.

`registry.seed()` **never promotes.** Promotion is `candidates.promote`, whose
`REVIEWER_TYPES` is `("human", "librarian")` — every agent/worker/model reviewer
type is refused with `ReviewerNotPermitted`. There is no auto-promote path, and no
argument to `seed()` can create one. A model cannot launder a capability claim
about itself into trusted recall.

Seeding is **idempotent**: a fact already present (as a candidate or a promoted
claim) is not re-filed.

## 5. The read surface

`brainconnect registry list` (add `--json` for machine output) prints every tier
in canonical order (by ordinal, ties broken by name) with:

- the tier's ordinal, required capabilities, and provider binding (from data);
- the tier-metadata claim's status (from the ledger);
- the preferred and deployed model, each with its claim status and whether it is
  **trusted** (promoted AND not in an open contradiction — LEDGER_SPEC §14.1:
  status is not trust).

The query is **deterministic**: two reads against an unchanged ledger are
byte-identical. `registry.preferred_model("high-capability-local")` returns the
declared preferred model from data; it is surfaced in `brainconnect ledger-health`
by **reading the registry**, never a hard-coded constant.

```
brainconnect registry list                 # tiers + preferred/deployed + status
brainconnect registry list --json          # deterministic machine output
brainconnect registry seed                 # file the hierarchy as PENDING candidates
brainconnect promote candidate_N \          # human-gated promotion (never an agent)
  --scope model:Qwen3.6-35B-A3B --confidence high
```

## 6. Boundary reminders (ADR 0008, binding)

- Do **not** add routing/placement/scheduling/residency math here. Call
  AgentConnect and ComputeConnect; record the returned decision as provenance
  (Lane 4).
- Do **not** add an auto-promote path. Promotion is human/librarian only.
- Do **not** attach a fabricated benchmark to any model. Measured numbers arrive
  as candidates through Lane 7.
- Do **not** make any provider a required dependency. The registry functions with
  zero models loaded — everything above runs against a ledger and never touches
  `:8080`.
