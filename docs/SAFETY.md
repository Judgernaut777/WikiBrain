# SAFETY.md — memory safety in WikiBrain

WikiBrain owns **policy**: when memory is scanned, and what happens when something
is found. It does not own **detection**. Detection is delegated to modular engines,
and the good ones are maintained by other people.

WikiBrain is not becoming an enterprise secret scanner, a PII platform, or a
prompt-injection model project. It has a small built-in ruleset so that a default
install is safe offline, and a clean seam for engines that are better than it.

Nothing here requires an external package, an ecosystem, or a network.

---

## The rule this whole package exists to enforce

> **`trusted` does not mean safe to expose. `safe` does not mean trusted.**

`trusted` is a statement about **authority** — a human promoted this claim, from
this source, in this scope, and nothing contradicts it. It says nothing about
whether the text contains an API key.

A clean scan is a statement about **content**. It says nothing about whether the
claim is true.

They are independent, and the code keeps them that way: **no engine and no policy
may set `trusted`.** Safety can withhold, mask, or block. It cannot vouch. The
acceptance gate asserts this structurally, by parsing every module in
`cli/wiki/safety/` and checking the identifier appears nowhere in its AST.

So a promoted, correct, well-sourced claim that happens to carry a live credential
is *trusted* and *unsafe*, and recall returns it **trusted, with the credential
masked**. That is not a contradiction. It is the point.

---

## What is enforced today

Three surfaces, all live:

| Surface | When | Behaviour |
|---|---|---|
| `memory_candidate` | capture, before anything is written | Secrets are **masked before storage** — the raw value reaches neither the candidate row nor the `inbox/` artifact. PII is masked. High-risk injection or tool-control text is **quarantined**: stored, flagged, and not promotable without an override. |
| `memory_recall` | read, after scope/trust/contradiction filtering, before returning | A secret in a trusted claim is **masked on the way out**; the claim stays trusted. High-risk injection or tool-control content is **withheld** and announced in `warnings`. **The claim text in the ledger is never mutated.** |
| `memory_promotion` | the human gate | Secrets **block**. High-risk injection and tool-control **block**. This is the only surface that runs whole-file scanners, because it happens once, a human is present, and it is the moment content becomes trusted. |

Two surfaces are specified and **not implemented**: `source_ingest` and
`obsidian_projection`. Asking to scan them raises rather than defaulting to
something permissive. See *Future work*.

### Failure is not cleanliness

The five states an engine can end in are never collapsed, because collapsing any
two of them is how a scanner starts reporting unread content as clean:

| Status | Meaning |
|---|---|
| `ok` | it ran. It may have found nothing — *that is a result* |
| `disabled` | switched off in configuration. It never looked, by choice |
| `unavailable` | no import, no binary, no model. It never looked, by absence |
| `skipped` | it has no capability this surface asked for. Correctly idle |
| `failed` | it looked, it raised, and we do not know what it saw |
| `timeout` | an external tool exceeded its budget. A `failed` with a cause |

A **required** engine that does not finish `ok` produces a `scanner_error` finding
at critical severity, and no policy maps that to `allow`. Promotion blocks; recall
withholds the affected item. An **optional** engine that breaks produces a warning,
and the engines that did run still count.

> Only `ok` licenses the sentence "this content was scanned for X."

### The human gate is unchanged

Safety is a **second** gate, behind the human one, and it can only ever subtract.
Agents still cannot promote. A clean scan promotes nothing.

A reviewer who has verified a finding may pass `--safety-override` with an
`--override-reason`. It is available only to the same non-agent reviewers, requires
a non-empty reason, records the actor and timestamp, **retains the original
findings verbatim, and never relabels the result as clean**. Overriding a gate that
is already open is itself an error.

---

## Engines

An engine answers *does this text contain a probable secret*. It never decides what
happens next. Each declares `name`, `version`, `capabilities`, `available()`, and
`scan()`; findings are normalized into a WikiBrain-owned model carrying `engine`,
`engine_version`, `kind`, `rule`, `severity`, `confidence`, `span`, `message`, and
`metadata`.

**No finding, warning, log line, or audit record ever contains the matched value.**
A raw match is used to locate a span and is then dropped.

| Engine | Capabilities | Install | Status |
|---|---|---|---|
| `baseline` | secrets, pii, prompt_injection, tool_control, encoded_content | built in, pure stdlib | **on by default, required** |
| `detect_secrets` | secrets | `pip install wiki-brain[safety-secrets]` | **enabled by default**, inert when absent |
| `trufflehog` | source_or_repository_secrets | separate executable on `PATH` | off by default |
| `gitleaks` | source_or_repository_secrets | separate executable on `PATH` | off by default |
| `presidio` | pii | `pip install wiki-brain[safety-pii]` + a spaCy model | off by default |
| `prompt_guard` | prompt_injection | `pip install wiki-brain[safety-injection]` | off by default |

`gliner` is **deferred and deliberately not registered.** Naming it in the registry
would let someone write `gliner.enabled = true`, get an engine that never runs, and
believe their PII coverage had improved.

### Surface → engine selection

Policy chooses engines by **capability**, never by name. TruffleHog and Gitleaks
declare only `source_or_repository_secrets`, which is how a recall of eight
one-sentence claims avoids becoming eight subprocess spawns.

| Surface | Capabilities requested |
|---|---|
| `memory_candidate` | secrets, pii, prompt_injection, tool_control, encoded_content |
| `memory_recall` | secrets, pii, prompt_injection, tool_control, encoded_content |
| `memory_promotion` | all of the above **+ source_or_repository_secrets** |

### Aggregation

Findings from every engine are **unioned**. One engine finding nothing never cancels
another engine finding risk; the **strongest** decision wins, never the average or
the majority. Identical findings from one engine are deduplicated, engine
attribution is preserved, and overlapping redaction spans are merged before masking
so that two engines reporting the same credential produce one mask and two pieces of
evidence.

### No network, by default and by construction

TruffleHog offers to *verify* a candidate credential by authenticating against the
service it belongs to. That is exfiltration: it takes the secret WikiBrain is trying
to contain and mails it to a third party to ask whether it still works. It is off
unless `allow_network_verification = true`, per engine.

`prompt_guard` reports itself `unavailable` rather than downloading weights, unless
`allow_download = true`. A recall must never become a network call. Pin `revision`:
a classifier that silently changes its weights changes what WikiBrain withholds.

The deterministic core CLI still makes **zero model calls**. `prompt_guard` is the
only engine that runs one, it is local, and it is off by default.

---

## The baseline is a floor, not a product

It is intentionally limited:

- ten well-known credential shapes, plus one entropy-gated `api_key = "…"` heuristic
- five PII regexes and a Luhn check
- six prompt-injection lure patterns
- seven tool-control directive rules
- base64/hex/data-URI/escape-run detection, which **decodes a blob and judges what
  comes out** rather than the fact that it was encoded

Before any rule runs, text is normalized: zero-width and bidi controls stripped,
homoglyphs folded, NFKC applied. An attacker who inserts a zero-width space into
`ignore all previous instructions` defeats a regex without changing what a model
reads.

It will miss things. It does not find a name, an address, a date of birth, a
medical record number, or any novel credential shape. **It is not a substitute for
detect-secrets, TruffleHog, Gitleaks, Presidio, or a maintained injection
classifier**, and no effort will be spent trying to make it one.

A fast deterministic filter is also *provably evadable* by anyone who can query it.
The baseline raises attacker cost. It is not an authority. The structural defences
are the ones that hold: all recalled text is data, agents cannot promote, and a
quarantined claim is withheld rather than trusted.

---

## Configuration

Under `[safety]` in `config.toml`; see `config.example.toml` for the annotated
version.

```toml
[safety]
enabled = true
max_text_chars = 200000

[safety.engines.baseline]
enabled = true
required = true          # cannot be disabled while safety is enabled

[safety.engines.detect_secrets]
enabled = true           # inert, and harmless, when the package is absent
required = false

[safety.engines.trufflehog]
enabled = false
# executable = "trufflehog"
# timeout_seconds = 20.0
# allow_network_verification = false   # leave this off

[safety.engines.presidio]
enabled = false

[safety.engines.prompt_guard]
enabled = false
# model = "protectai/deberta-v3-base-prompt-injection-v2"
# revision = ""          # pin me
# allow_download = false
```

`enabled` and `available` are different questions:

- **enabled + available** — it runs.
- **enabled + unavailable** — it wanted to run and could not. If `required`, the
  content is unscanned, and unscanned is not clean.
- **disabled** — it will not run, by choice.
- **required** — if it does not finish `ok`, nothing here is certified clean.

A missing *optional* tool never crashes a default install. A missing *required* tool
never silently produces a clean result. `required = true` with `enabled = false` is
refused at load, as is an unknown engine name — a typo in `detct_secrets` that
quietly disabled secret scanning would be the worst possible failure, because
everything downstream would keep reporting clean.

`wiki health` reports every engine's `enabled`, `required`, and `available`
separately, and the ledger is **not healthy** when a required engine cannot run.

---

## Limitations

Read these as the contract, not as a disclaimer.

- **The baseline misses things.** See above. Enable a real engine.
- **Injection detection is evadable.** Any fast filter is. The classifier raises
  cost; containment is what holds.
- **Redaction is span-based.** A finding with no span — a whole-text classifier
  score — can be warned about or quarantined, but never masked. There is nothing to
  mask.
- **Redaction normalizes.** When something is masked, the returned text is the
  *normalized* text with spans masked, so offsets stay valid. When nothing is
  masked, your text is returned byte-for-byte.
- **Scanning happens at capture, recall, and promotion — not at rest.** A claim
  written directly to the database by another process is caught on the way out, not
  where it sits.
- **Source ingest and Obsidian projection are unscanned.** Both are named below.
- **A quarantined candidate is stored.** Quarantine flags and blocks promotion; it
  does not delete. Nothing in WikiBrain deletes memory as a safety action.

---

## Future work

Deliberately not built. Recorded so the design is settled before anyone builds it,
and so nobody mistakes the plan for the product.

- **`source_ingest`** — scan raw source documents on the way in, with the whole-file
  engines that promotion already uses.
- **`obsidian_projection`** — redact unsafe content before it is written to markdown.
  Deferred because the projection is regenerable from the database, so it is the one
  surface where a miss is repairable.
- **GLiNER** as a custom Presidio recognizer, for PII recall well above Presidio's
  own field-limited ~0.5 F1.
- **Structural containment** (spotlighting/datamarking) of withheld-but-requested
  content, so a caller can opt into seeing a quarantined claim inside a fence.

---

## The legacy `fascia-guard` seam

`cli/wiki/guard_hook.py` was an optional, dormant integration with an external
`fascia-guard` package. It is **deprecated and inert**: every function is a no-op,
`available()` returns False regardless of what is installed, and setting
`FASCIA_GUARD` or `FASCIA_GUARD_ENFORCE` warns rather than re-enabling it.

Two safety pipelines must never both be authoritative, so rather than define an
ordering between them, that one is switched off. `wiki.safety` is the built-in
authoritative path. The shim is retained only so an out-of-tree import does not
crash, and it will be deleted.

| Old | New |
|---|---|
| `FASCIA_GUARD=1` | `[safety] enabled = true` (the default) |
| `FASCIA_GUARD_ENFORCE=1` | the default: policy enforces at all three surfaces |
| `pip install fascia-guard` | nothing. The baseline is built in |

---

## Integration note

[AgentConnect](https://github.com/Judgernaut777/mcp-agentconnect) is an **optional**
control-plane integration; WikiBrain works independently. The trust boundary is
specified in [LEDGER_SPEC.md §14](LEDGER_SPEC.md). A consumer on the far side
inherits authority guarantees from `trusted`, and exposure guarantees from this
document — and must not confuse the two.
