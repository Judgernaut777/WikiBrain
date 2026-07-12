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
ledger. Two kinds of fact live there, and they bind differently:

- **MODEL claims** (preferred / deployed) are `model:`-scoped and carry the
  `model-performance` tag — the `model_performance` profile (§7) is exactly where a
  measured capability fact about a model belongs.
- **Tier-STRUCTURE facts** (ordinal, required capabilities, provider binding) are
  `global`-scoped and are *registry-structural*, **not** model-performance: they
  describe the registry's own hierarchy, not a measured property of any model. They
  carry the `registry-structural` tag and never the `model-performance` tag, because
  §7 confines `model_performance` to `model`/`worker` scope. **No claim binds
  `model_performance` at global scope.**

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

> **Capability vocabulary is AgentConnect's, not ours.** The `required_capabilities`
> strings here are a *requirements list*, not a taxonomy. At Lane 4 they must be
> **mapped onto AgentConnect's existing capability vocabulary** (the terms
> `RoutingEngine.route`/`capability_overlap` already score on) — the registry must
> not grow a second, competing capability ontology. If a required capability has no
> AgentConnect term, that is a mapping/reconciliation task at the boundary, not a
> new BrainConnect taxonomy.

## 3. Preferred vs deployed (the model-name reconciliation)

The `high-capability-local` tier names two distinct models, and the distinction
is the whole point of a *trust* ledger (see ADR 0008 "Model-name reconciliation"):

- **Declared PREFERRED:** `Qwen3.6-35B-A3B`. A **preference/recommendation only**.
  It carries **no benchmark numbers** (none can exist until it is deployed and
  measured) and is **not a required runtime dependency**.
- **Currently DEPLOYED:** `qwen3-30b-a3b` — the real model on the `wiki-llama`
  `:8080` node. BrainConnect records that it is deployed; it does not connect to
  it. The `deployed` role is a **declared STATIC fact** (editable only in
  `SEED_TIERS`); it says which model is *recorded* as serving the tier, **not**
  that it is loaded or reachable right now. It must never be wired to ComputeConnect
  residency/warm-state or host liveness — that would re-hold live run state, which
  ADR 0008 / LEDGER_SPEC §2 forbid. Liveness is ComputeConnect's to answer.

**No benchmark numbers are published for either model.** None have been measured.
Fabricating any is forbidden. When the preferred model is deployed and a real
measured run exists, its `model_performance` numbers flow in through Lane 7 as
their own candidates — awaiting human promotion, like everything else.

If `Qwen3.6-35B-A3B` turns out to be a typo for the deployed model, correcting it
is a one-field edit in `SEED_TIERS` plus re-promotion — no code change.

## 4. How a fact becomes trusted

Seeding files each tier/model fact as a **pending** candidate (LEDGER_SPEC §5.2)
under the right scope and tag:

- tier metadata → `global` scope, tag `registry-structural` (registry-structural,
  **never** `model-performance` — §7 confines that profile to `model`/`worker`
  scope);
- preferred/deployed model → `model:<name>` scope, tag `model-performance`.

`registry.seed()` **never promotes.** Promotion is `candidates.promote`, whose
`REVIEWER_TYPES` is `("human", "librarian")` — every agent/worker/model reviewer
type is refused with `ReviewerNotPermitted`. There is no auto-promote path, and no
argument to `seed()` can create one. A model cannot launder a capability claim
about itself into trusted recall.

Seeding is **idempotent**: a fact already present (as the registry's own candidate
or its promoted claim) is not re-filed.

### 4.1 Canonical facts are identified by an unforgeable marker, not a tag

The registry locates its own facts by a **registry-controlled, unforgeable marker**
(`candidates.REGISTRY_CANONICAL_KEY`, written into candidate metadata at seed time
and matched exactly with `json_extract`) — **never** by the public `reg:*` tag. The
capture API forwards arbitrary caller tags, so a `reg:*` tag is *squattable*: an
agent could file a candidate tagged `reg:preferred:high-capability-local` with a
fabricated number. That squatter **cannot** set the marker (the public metadata path
strips the reserved key), so it can neither be surfaced by `snapshot()`/`registry
list` nor suppress the canonical seed. On a detected collision `seed()` still files
the canonical fact and **warns**; it never silently skips. Any residual `reg:*`
tag lookups (collision detection) use **exact** `json_each` array membership, not a
`LIKE '%…%'` substring, so a name containing `_` or `%` cannot over-match.

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

## 6. Serving trusted claims over `:8787` (ADR 0008 Lane 3)

The registry's trusted claims are served to AgentConnect over the existing
`brainconnect serve` HTTP surface (`cli/brainconnect/server.py`, default
`127.0.0.1:8787`) so AC's `RoutingEngine` can **pull** a human-promoted capability
source and weight it *instead of* its self-conferred `learned_quality` (the
self-promotion LEDGER_SPEC §2 forbids). This is the **BC side only**: BC serves
trusted claims; **how AC weights them lives in the AgentConnect repo**, out of scope
here.

```
GET /registry                 # trusted-only capability claims (bearer-authed)
GET /registry/capabilities    # identical alias
```

Properties (all enforced in `registry.trusted_view` + `tests/acceptance.py`):

- **Read-only.** No mutation path exists: the route is `GET`-only, a `POST` is a
  route miss (`404 not_found`) and any other verb wears the enveloped
  `invalid_request`. The handler resolves and serializes; it writes nothing.
- **Bearer-authed exactly like every other route.** When a token is configured, a
  missing/wrong credential is refused `forbidden` (`403`); only `GET /health` stays
  open. It reuses the server's one auth path — there is no route-specific auth.
  **A tokenless `brainconnect serve` is UNAUTHENTICATED:** with no `--token` /
  `BRAINCONNECT_TOKEN` set, auth is off and every non-health route — now including
  `GET /registry`, which then serves the trusted capability claims to any caller
  that can reach the socket — is open. This is why the server binds `127.0.0.1` by
  default: a tokenless serve MUST stay on loopback and must never be exposed beyond
  `127.0.0.1`. Set a token before binding anywhere else. (The auth default is
  intentionally unchanged — consistent with every other route — and is a deployment
  obligation, not a code toggle.)
- **Trusted-only.** The payload carries the tier hierarchy STRUCTURE (ordinal,
  required capabilities, provider — data-derived, never squattable) and, per tier,
  the preferred/deployed **model claim only when it is `trusted`** (promoted AND
  uncontradicted, resolved by the unforgeable registry marker — §4.1). A **pending**
  candidate and a **squatted** `reg:*` fact — even one a human is tricked into
  promoting — are **omitted** (the tier slot goes `null`), never relabelled trusted.
  A flat `trusted_capability_claims` list gives AC the consumable set directly.
- **No fabricated numbers, no live state.** Each claim carries identity
  (tier/role/model/scope), trust status, and the promoted-claim ref AC keys on —
  and nothing else. No benchmark metric is attached (measured numbers arrive as
  candidates through Lane 7), and `deployed` remains a static declared fact, never a
  liveness signal. The endpoint needs **zero models loaded** — it reads the ledger,
  never `:8080`.
- **Deterministic.** Two reads against an unchanged ledger are byte-identical
  (`trusted_view` walks the deterministic `snapshot()`).

The consumption side — AC pulling this endpoint and folding it into
`RoutingEngine.route` weighting — is **delegated to the AgentConnect repo** (ADR
0008: BC serves trusted claims, AC decides how to weight them).

## 7. Boundary reminders (ADR 0008, binding)

- Do **not** add routing/placement/scheduling/residency math here. Call
  AgentConnect and ComputeConnect; record the returned decision as provenance
  (Lane 4).
- Do **not** add an auto-promote path. Promotion is human/librarian only.
- Do **not** attach a fabricated benchmark to any model. Measured numbers arrive
  as candidates through Lane 7.
- Do **not** make any provider a required dependency. The registry functions with
  zero models loaded — everything above runs against a ledger and never touches
  `:8080`.
