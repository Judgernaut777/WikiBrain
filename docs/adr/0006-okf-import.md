# ADR 0006 — OKF import: pending-only intake, no gate bypass, no canonical overwrite

Status: accepted (2026-07-13, OKF Stage 3)
Scope: `cli/brainconnect/okf/okfimport.py` (new), `brainconnect import okf` (new
CLI), `OKFAdapter.import_bundle`, `docs/OKF.md`, `scripts/okf_import_demo.py` (new).
No schema change; no new or broadened safety surface.

## Context

Stage 1 exports an OKF bundle; Stage 2 structurally validates one. Stage 3 is the
only stage that lets external content reach the ledger, and so it is the highest-risk
of the four. The failure this stage must not permit is the one BrainConnect exists to
prevent: an agent (or a hostile bundle) landing trusted, recallable knowledge without
a human's explicit promotion — or an external identifier silently editing a canonical
claim. "OKF-valid" is emphatically not "trusted" and not "safe"; a perfectly
well-formed bundle can be entirely hostile, false, or booby-trapped with a secret or
an injection lure.

## Decision

1. **Import's entire authority is: create PENDING candidates.** `import_bundle` calls
   `candidates.create_checked` (which writes `status='pending'` unconditionally) and
   **never** `candidates.promote`. There is no code path, and no argument, by which
   import produces a promoted or trusted claim. Human promotion remains a separate,
   unchanged surface.

2. **An invalid bundle imports nothing (no partial import).** Structural validation
   (Stage 2, reused verbatim) runs first. If it reports any error, `import_bundle`
   returns `valid=False` and creates nothing — not the valid documents, not a
   "best-effort" subset. Validation is a gate, not a repair step.

3. **The human gate cannot be bypassed by actor type.** An `agent` actor may propose
   an import — proposing is exactly what a candidate is for, and `PROPOSER_TYPES`
   already includes agents — but the resulting row is pending like any other.
   `--by-type` is recorded and never consulted for authority. The asymmetry that only
   non-agent reviewers may `promote` (enforced in `candidates.py`) is untouched.

4. **An external id confers no write authority over canonical state.** Import keys
   document identity on the external `brainconnect.id`, stored as the candidate
   `source_ref` `okf:<id>`. Before creating anything, import checks whether that
   external id already traces to a **promoted** claim (a join from `claims` to the
   `memory_candidates` row it was promoted from, filtered on `source_ref`). If it
   does:
   - identical content → an idempotent no-op (`duplicate`);
   - changed content → an explicit `conflict` requiring operator action.
   Either way **no candidate is created and the canonical claim is never touched.**
   Because import only ever writes pending candidates, a promoted claim is already
   unreachable by construction; this check makes the refusal *explicit and visible*
   rather than an accident of layering. Resolving a conflict (adopting the new text)
   is done through the normal claim-supersession governance path, not through import.

5. **Idempotency and updates are explicit, never silent.** A re-import of the same
   external id + same content checksum creates nothing and is reported as a
   `duplicate` — importing a bundle N times causes no duplication. A re-import of the
   same external id with *changed* content (and no promoted claim owning it) creates
   an **explicit new pending candidate**, reported as an `updated` outcome linked to
   the prior candidate(s). The earlier candidate is never overwritten or mutated.

6. **The operator governs scope, not the bundle.** `--scope` sets the scope of every
   created candidate; a document's own `scope:` field is retained only as
   informational metadata. A bundle cannot elevate its own blast radius.

7. **Reuse the `memory_candidate` safety surface; add nothing.** The handoff permits a
   narrow `okf_import` safety surface *only if* the existing capture surface does not
   cover import cleanly. It does cover it: `memory_candidate` already **masks a secret
   before it is written** to an inbox artifact or a candidate row, and **quarantines**
   high-severity injection / tool-control content (accepted-but-quarantined, needing a
   human override at promotion). That is precisely import's requirement, so import
   routes every document body through `create_checked` and introduces **no new or
   broadened safety policy**. A safety *block* (should a future engine map a category
   that way on this surface) stores nothing; the attempt is recorded in the result and
   the audit log carrying finding *kinds* only — never the matched value, never a raw
   span in a log.

8. **Provenance is preserved, and safely.** Each candidate's `metadata.okf_import`
   records bundle path, bundle checksum (the "source checksum"), OKF version, document
   path, external id, per-document content checksum, imported-at timestamp, importing
   actor + type, and relative relationships. "Original frontmatter where safe" is the
   structural, controlled-vocabulary subset of the `brainconnect:` block only — the
   free-text `title` and unknown extension fields are deliberately **not** copied into
   recallable metadata, because a hostile bundle could plant a secret there and only
   the claim **body** is the field that is safety-scanned and masked.

9. **No live mirror.** No directory watching, no bidirectional sync, no auto-merge, no
   auto-supersede, no silent conflict resolution. Import is a one-shot,
   operator-invoked, human-gated intake.

## Consequences

- `OKFAdapter.import_bundle(repo, ImportRequest) -> ImportResult` now returns a result
  instead of raising `NotImplementedError`; the Stage-2 acceptance check that pinned
  the deferral was updated to pin the new behavior (an invalid/missing bundle is
  *reported* invalid with nothing imported, not raised).
- `brainconnect import okf DIR --scope S --by ACTOR [--by-type T] [--dry-run] [--json]`
  exits non-zero on an invalid bundle, so it drops into a shell pipeline as a gate.
  The command opens `Repo.open()` from the CWD (like every mutating command); the
  acceptance CLI check `chdir`s into a scratch repo so it never touches the real tree.
- Identity is external-id-based and content-checksum-verified; no schema migration was
  needed because `source_ref` is an existing, queryable column reserved here with the
  `okf:` prefix (no other candidate producer writes that prefix).
- Import safety is bounded to reuse; `docs/SAFETY.md`'s three live surfaces are
  unchanged. The demo `scripts/okf_import_demo.py` exercises pending-only import, a
  redacted secret, a quarantined injection, idempotent re-import, a refused external-id
  overwrite of a promoted claim (canonical unchanged), and an agent-actor import that
  still lands only pending.
