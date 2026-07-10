# SAFETY.md — what the trust gate protects, and what it does not

WikiBrain is a **trusted memory ledger**. Its guarantees are about *authority* —
who decided a claim is true, on what evidence, in what scope, and whether anything
has since contradicted or superseded it.

Authority is not the same thing as safety. The distinction below is the whole point
of this document:

> **A trusted claim can still be unsafe to expose.** A human can promote a claim
> that is entirely correct and still contains an API key, a customer's address, or
> a sentence written to hijack whatever model reads it next. Promotion answers
> *"is this true?"* — it has never answered *"is this safe to hand to a model, write
> to disk, or show in a rendered page?"*

Nothing in WikiBrain today closes that gap. Read the two sections below as a pair:
what exists, then what does not.

---

## What protects you today

These are implemented, tested, and load-bearing. They are content-blind: they reason
about provenance and lifecycle, never about what the text says.

| Protection | What it actually gives you |
|---|---|
| **Human-gated promotion** | Nothing an agent captures becomes trusted memory without a human (or the librarian, under review) promoting it. This is the primary defence against memory poisoning. |
| **Agents cannot promote** | `promote()` refuses an agent `reviewer_type` outright. There is no MCP tool that promotes; `brain_promote` / `brain_reject` exist only under `wiki mcp serve --review`, which is not an agent-facing surface. |
| **Candidate lifecycle** | Everything captured lands `pending`. Rejected and archived material is never recalled. |
| **`trusted_only` semantics** | With the defaults, every item in a recall pack is `trusted: true`. Disputed, pending and superseded material is withheld and announced in `warnings`. |
| **Scope filtering** | A claim reaches a consumer only if its scope matches what was requested. |
| **Contradiction and supersession** | A contradicted claim stops being trusted without being deleted. A superseded claim is announced rather than silently dropped. |
| **Provenance tracking** | Every claim links to the sources it came from and records who proposed and who promoted it. |
| **Migration safeguards** | Forward-only, additive migrations; the live-DB hazard is documented in [MIGRATIONS.md](MIGRATIONS.md). |

**None of these inspect content.** WikiBrain does not scan for secrets, PII,
prompt-injection text, or unsafe tool-control instructions on any of its surfaces.
Do not read "trusted" as "scanned", "redacted", or "sanitised" — it means promoted.

### The one exception, and why not to rely on it

`cli/wiki/guard_hook.py` holds an old integration with an external `fascia-guard`
package. It is a **soft dependency and dormant by default**: the package is not a
requirement, is not installed by default, and even when present the hook does nothing
unless `FASCIA_GUARD` or `FASCIA_GUARD_ENFORCE` is set in the environment.

You do not need it, and the documentation no longer directs you to it. It is a legacy
seam that the built-in module described below is intended to replace. Treat WikiBrain's
current content-safety posture as **none**, and size your own controls accordingly.

---

## Future direction: safety scanning inside WikiBrain

**This is future work. None of it exists yet.** It is recorded here so the design is
settled before anyone builds it, and so nobody mistakes the plan for the product.

WikiBrain should include baseline local safety scanning **directly in WikiBrain**,
scoped to WikiBrain-owned surfaces: memory candidates, recall output, promotion,
rejection notes, source text, and Obsidian projection. It should detect and handle
secrets, PII, prompt-injection text, unsafe tool-control instructions, and suspicious
encoded blobs before content is stored, promoted, recalled, or projected.

This must be a **WikiBrain-local module, not a required third-party runtime
dependency**. WikiBrain has to keep working as a standalone product, offline, with
zero model calls in the deterministic core CLI. A safety layer that arrives as an
external install requirement would violate the reason this document exists.

### Shape

```
wikibrain.safety
  models.py          # verdicts, findings, severities
  scanner.py         # policy dispatch over the rule set
  redaction.py       # span masking
  policies.py        # the five surfaces below
  rules/
    secrets.py
    pii.py
    prompt_injection.py
    tool_instructions.py
    encoding.py
```

### Surfaces it owns

`memory_candidate` · `memory_recall` · `memory_promotion` · `obsidian_projection` ·
`source_ingest`

### Intended behaviour

| Situation | Expected handling |
|---|---|
| Candidate carries a secret | Block, redact, or quarantine at the write door |
| Trusted claim carries a secret | Redact on recall / output — a credential must never leave memory, even a promoted one |
| Prompt-injection candidate | Warn or quarantine; must not be promoted by accident |
| Promotion of a high-risk candidate | Require an explicit human override |
| Obsidian projection | Redact unsafe sensitive content before writing markdown |

That third row is why scanning cannot live only at capture. Content promoted before
the scanner existed, or written by another client directly against the database, has
to be caught on the way *out* as well as on the way in.

---

## Integration note

[AgentConnect](https://github.com/Judgernaut777/mcp-agentconnect) is an **optional**
control-plane integration; WikiBrain works independently. The trust boundary between
the two is specified in [LEDGER_SPEC.md §14](LEDGER_SPEC.md). A consumer on the far
side of that boundary inherits exactly the guarantees in *What protects you today* —
authority, not content safety — and should not assume a recall pack has been scanned.
