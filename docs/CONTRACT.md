# CONTRACT.md — the response shapes a consumer may rely on

> **Product: BrainConnect. Python module and CLI: `brainconnect`** (renamed from
> `wiki` on 2026-07-12; see [STATUS.md](STATUS.md)). The MCP tools are `brain_*`
> and the isolation variable is `BRAINCONNECT_DB` (legacy `WIKIBRAIN_DB` is
> honored with a `DeprecationWarning` while the new name is unset). `health()`
> reports `"service": "brainconnect"`.

This document, and the fixtures beside it, exist because BrainConnect emits fields
that a consumer can miss. AgentConnect misses three of them today: `safety` on a
recall item, and `safety` and `quarantined` on a capture result. Missing them costs
observability, never trust or safety. But nobody should have to read our source to
learn that they are there.

Everything here is **pinned by a test**, not by prose.

## Where the contract lives

| | |
|---|---|
| Fixtures | `tests/contract/*.json` — seven canonical responses |
| Builders | `tests/contract_cases.py` — each one produced by real code against a real ledger |
| Regenerate | `python3 tests/gen_contract_fixtures.py` |
| Enforced by | `tests/acceptance.py`, which rebuilds each response and asserts equality |

A fixture is never written by hand. A hand-written fixture pins what somebody
*believed* the API returned; these pin what it returns. If a response shape changes,
the gate fails and names the field.

The fixtures are **install-independent**. Every case pins a baseline-only engine set,
so they do not change when someone runs `pip install detect-secrets`. What is under
contract is the field shape, not how many engines happened to be present.

---

## The additive safety fields

Safety changed no existing field and removed none. A consumer written before it
existed is still correct — it simply cannot explain itself to a human. These are the
fields it is missing.

### Recall item

```jsonc
{
  "id": "claim_1", "text": "…", "status": "promoted", "trusted": true,
  "confidence": "high", "validity": "current",
  "scope": {"scope_type": "global", "scope_id": ""},
  "source_id": "source_1", "sources": [ … ],

  // present ONLY when the returned representation is not clean
  "safety": {
    "surface": "memory_recall",
    "decision": "redact",            // allow | warn | redact | quarantine | block
    "kinds": ["secret"],             // secret | pii | prompt_injection |
                                     // tool_instruction | encoding | scanner_error
    "redacted": true,
    "findings": [
      {
        "engine": "baseline", "engine_version": "1",
        "kind": "secret", "rule": "aws_access_key",
        "severity": "critical",      // low | medium | high | critical
        "confidence": 1.0,
        "span": [18, 38],            // half-open, into `text`. Absent when the
                                     // finding has no span (a classifier score).
        "message": "…"
      }
    ],
    "engines": [ {"engine": "…", "version": "…", "status": "…",
                  "required": bool, "findings": int} ]
  }
}
```

`text` is the **representation being handed over**. The canonical claim text in the
ledger is never rewritten by recall.

> **A masked item is still trusted.** `trusted: true`, `status: "promoted"`, and `█`
> runs where a credential was. Masking is exposure control, not distrust. A consumer
> that downgrades trust on seeing `safety` has misread this.

Pinned by `recall_item_clean.json` (no `safety` key at all) and
`recall_item_masked_trusted.json`.

### Recall pack, when something is withheld

```jsonc
{
  "backend": "sqlite_fts", "profile": "manager_brief", "query": "…",
  "retrieval_mode": "fts", "items": [], "note": "…",
  "warnings": [
    "1 claim(s) matching this query were WITHHELD by safety policy (…). They remain in the ledger; nothing was deleted."
  ]
}
```

> **An empty `items` with a warning is a complete answer, not an absence of memory.**
> High-risk injection or tool-control content is withheld, and so is content a
> *required* engine could not scan. Nothing is deleted. A consumer that reports "no
> memory found" here has lost the only information that mattered.

Pinned by `recall_pack_withheld.json`.

### Capture result

```jsonc
{
  "accepted": true,
  "candidate_id": "candidate_1",
  "status": "pending",
  "quarantined": false,        // ALWAYS present
  "message": "…",
  "safety": { … }              // present ONLY when the capture was not clean
}
```

> **`accepted` does not mean safe.** A quarantined candidate is `accepted: true`,
> `status: "pending"`, `quarantined: true`. It is stored and of record, and it cannot
> be promoted without an explicit human override. A consumer keying on `accepted`
> alone cannot tell it from a clean capture — which is exactly why `quarantined`
> exists, and why dropping it means a later `promote` raises instead of being
> pre-filtered.

Secrets are masked **before** storage, so a redacted capture's original text was never
written to the candidate row or to the `inbox/` artifact.

For the same reason, a redacted candidate (decision `redact`, not `quarantine`/`block`)
can be **promoted without an override** — only the mask was ever stored, so promotion
publishes nothing sensitive; the human-override gate protects quarantined content,
whose original text *is* stored.

Pinned by `capture_result_clean.json` and `capture_result_quarantined.json`.

### Health

```jsonc
{
  "ok": false,                 // false when a required safety engine cannot run
  "service": "brainconnect", "role": "trusted memory ledger",
  "schema_version": 9, "backend": { … }, "ledger": { … }, "profiles": [ … ],
  "safety": {
    "enabled": true, "ok": false,
    "surfaces": ["memory_candidate", "memory_recall", "memory_promotion"],
    "engines": [ {"engine": "gitleaks", "enabled": true, "required": true,
                  "available": false, "version": "cli"} ],
    "required_engines_unavailable": ["gitleaks"]
  }
}
```

`enabled` and `available` are reported separately, on purpose. An engine that is
enabled and unavailable is the most misleading state a scanner can be in, and a health
check that collapses the two hides it. `available` is `null` for a disabled engine: it
was never asked.

> `ok: false` means **degraded, not unreachable.** Such a ledger will fail closed on
> every promotion and withhold on every recall. That is correct behaviour, and a
> consumer should surface it rather than infer it from a stream of refusals.

Pinned by `health_degraded_required_engine.json`.

---

## Refusal semantics

BrainConnect's in-process API refuses by **raising**. A transport cannot: it must
answer with a code. `cli/brainconnect/errors.py` is that mapping — five codes, an HTTP status,
and whether a retry could ever help.

**`brainconnect.errors` is the vocabulary of every refusal** — asserted by the
contract tests, and answered on the wire by `brainconnect serve`.

| Code | HTTP | Retryable | Raised by | Means |
|---|---|---|---|---|
| `safety_refused` | 409 | no | `candidates.SafetyRefused` | the request was fine; the **content** is not |
| `not_found` | 404 | no | `candidates.CandidateNotFound` | no such candidate |
| `forbidden` | 403 | no | `candidates.ReviewerNotPermitted` | this actor may **never** do this |
| `invalid_request` | 400 | no | `ApiError`, `ScopeError`, `ConfidenceError`, `RefError`, `ProfileError`, `FeedbackError`, `IngestError`, other `CandidateError` | the request was malformed |
| `backend_error` | 503 | **yes** | `BackendError`, `SafetyConfigError`, `PolicyError`, anything unrecognised | BrainConnect is degraded; not the caller's fault |

Two distinctions are worth the trouble of keeping:

**`forbidden` versus `invalid_request`.** An agent told *invalid* fixes its payload and
tries again. An agent told *forbidden* learns that promotion is not available to it,
which is the entire point of the human gate. Collapse them and you teach a fleet to
keep knocking.

**`safety_refused` versus `invalid_request`.** A safety refusal is not a bug in the
request. The request was well-formed and the content was dangerous. A consumer that
retries with a tidier payload has misunderstood; one that escalates to a human has
understood exactly.

An exception the table has never heard of is `backend_error`, never `invalid_request`.
An unrecognised failure is BrainConnect's problem to explain, and blaming the caller's
request would be a guess dressed as an answer.

### The refusal envelope

Served by `brainconnect serve` and produced by `errors.envelope(exc)`; pinned
by `promotion_safety_refusal.json`:

```jsonc
// HTTP 409
{
  "error": {
    "code": "safety_refused",
    "message": "safety policy blocks promoting candidate_1: prompt_injection (high, via baseline). Promoting it anyway requires an explicit override with a reason.",
    "retryable": false,
    "safety": { "surface": "memory_promotion", "decision": "block",
                "kinds": ["prompt_injection"], "findings": [ … ], "engines": [ … ] }
  }
}
```

`safety` appears only on a safety refusal. It is the audit-safe summary: rule names,
severities, spans, engine attribution.

> **It never contains the matched text.** A refusal that quotes the credential it
> refused has published it. The gate asserts this.

The **override is deliberately absent from this envelope.** It is human-only, at the
CLI (`brainconnect promote --safety-override --override-reason …`), requires a non-empty
reason, records the actor, and retains the original findings. A control plane must
surface a refusal to a human. It must not retry around it, and there is no field here
that would let it.

## The served contract: `brainconnect serve`

Built 2026-07-12. A pure-stdlib HTTP server (`http.server`; a fresh `Repo` per
request), default **`127.0.0.1:8787`**, run with:

```bash
brainconnect serve [--host 127.0.0.1] [--port 8787] [--token TOKEN]
```

It serves **exactly six routes** — the ones AgentConnect's
`WikiBrainMemoryAdapter` calls — and refuses everything else with a `not_found`
envelope. Every response body below is the corresponding in-process shape from
this document, unchanged: the gate asserts wire-vs-in-process equality for both
a recall pack and a refusal envelope.

| Route | Body (request → response) |
|---|---|
| `POST /recall` | `{query, profile?, scopes?, max_items?, trusted_only?, include_pending?, include_superseded?, task_id?}` → the recall pack above |
| `POST /capture` | `{text, origin_actor_id, origin_actor_type?, source_ref?, tags?, proposed_scopes?, task_id?}` → the capture result above (`origin_actor_id` is required; the ledger records who proposed a claim and never guesses) |
| `POST /candidates/{id}/promote` | `{promoted_by, confidence, scope?, note?, reviewer_type?}` → the promoted claim, with `claim_id` echoed beside `id`. **`safety_override` / `override_reason` are refused with 403 `forbidden`** — the override is human-only, at the CLI, and no HTTP field reaches it |
| `GET /candidates?status=pending&limit=N` | → `{"count": N, "candidates": [...]}` (limit capped at 500) |
| `POST /feedback` | `{feedback, actor_id, memory_item_id?/claim_id?, source_id?, note?, task_id?}` → `{"recorded": true}`. `memory_item_id` is AgentConnect's name for `claim_id`; both are accepted, a contradiction between them is `invalid_request` |
| `GET /health` | → the health shape above |

Rules the transport adds — and the only ones it adds:

- **Envelope, always.** Every refusal is `errors.envelope(exc)` with
  `errors.http_status(exc)`; the taxonomy above is never re-derived from message
  strings. A malformed body, an unknown field, a missing route — all wear the
  same nested `{"error": {code, message, retryable, safety?}}`.
- **Optional bearer token.** With `--token` (or `BRAINCONNECT_TOKEN`) set, every
  route except `GET /health` requires `Authorization: Bearer <token>` (the bare
  token is also accepted; comparison is constant-time). A missing or wrong
  credential is 403 `forbidden` — AgentConnect maps that to
  `MemoryAuthorizationError`, the never-retry class. `GET /health` stays open:
  a liveness probe that cannot ask "are you degraded?" invents the answer from
  refusals instead.
- **Nulls are absences.** Adapter clients send explicit `null` for unset
  optionals; the server treats them as absent so dataclass defaults apply.
- **The DB it serves is the one the resolved config (or `BRAINCONNECT_DB`)
  points at, and `Repo.open` migrates on every request.** Point a test server at
  a scratch DB, never at the live one.

---

## Stability

- **Additive by default.** New fields may appear. A consumer must ignore unknown keys.
- **`safety` and `quarantined` are optional to consume, mandatory to tolerate.**
  Dropping them costs observability. Nothing about trust changes.
- **`trusted` is the authority signal; `status` is not.** That rule is older than this
  document and outranks it. See [LEDGER_SPEC.md §14.1](LEDGER_SPEC.md).
- **A changed fixture is a changed contract.** Regenerate deliberately, and say here
  what moved.

Related: [LEDGER_SPEC.md §14.2](LEDGER_SPEC.md) for safety at the boundary,
[SAFETY.md](SAFETY.md) for what the surfaces do, [INTEGRATIONS.md](INTEGRATIONS.md) for
who consumes this and what remains deferred.
