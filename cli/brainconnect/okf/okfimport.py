"""OKF import (Stage 3) — the HIGHEST-RISK stage, and the most conservative.

Import turns an external OKF bundle into **pending memory candidates**. That is the
entire ceiling of its authority. It is written so that no combination of inputs —
hostile bundle, agent actor, colliding external id, changed source, a secret or an
injection lure planted in a document — can do anything a human did not separately
and explicitly approve through the normal promotion gate.

The flow, in order, and never out of order:

    1. structural VALIDATION      reuse Stage 2; an invalid bundle is refused whole.
                                  There is no partial import — nothing is written.
    2. provenance registration    each document's identity, checksum, bundle path,
                                  OKF version and relationships are recorded.
    3. import SAFETY scan         every document runs the existing `memory_candidate`
                                  surface BEFORE it is stored: secrets are masked,
                                  injection / tool-control content is quarantined.
                                  This covers BOTH the claim body AND every retained
                                  free-text frontmatter value (notably `provenance`):
                                  retained metadata is untrusted bundle content too,
                                  so it is masked / quarantined / dropped-fail-closed
                                  on the same policy, never stored verbatim.
    4. candidate creation         a PENDING candidate, via `candidates.create_checked`.
                                  There is no argument that makes it anything else.
    5. stop.                      Human promotion is a separate, unchanged surface.

The invariants this module is responsible for (each one a critical bug if broken):

  * **No auto-promotion, ever.** Every created row is `status='pending'`. Import
    calls `candidates.create_checked` and never `candidates.promote`.
  * **No bypass of the human gate.** An `agent` actor may *propose* an import (that
    is what a candidate is for), but the resulting row is pending like any other;
    nothing here can make an agent's import trusted.
  * **An external id confers no write authority over canonical state.** If an
    imported document's external id already traces to a PROMOTED claim, import
    REFUSES to touch that claim and returns an explicit `conflict` requiring
    operator action. It never edits, supersedes, or overwrites a canonical claim.
  * **OKF-valid is not trusted and is not safe.** A structurally valid bundle is
    still untrusted, unsafe-until-scanned input. All bundle content is DATA.

Import reuses the existing `memory_candidate` safety surface rather than inventing a
new one: that surface already masks secrets before storage and quarantines
injection / tool-control content, which is exactly import's requirement. No new or
broadened safety policy is introduced (see docs/OKF.md and docs/adr/0006-okf-import.md).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..db import Repo
from ..scopes import Scope
from .. import candidates as candmod, ingest, refs, safety, util
from .export import BODY_END_MARKER
from .validate import (ValidationLimits, _Frontmatter, _split_frontmatter,
                       validate_bundle)
from .yamlfmt import split_frontmatter

# The source_ref prefix that marks a candidate as OKF-imported. It is queryable
# (source_ref is a real column) and unique to this subsystem: no other producer
# of candidates ever writes an `okf:` ref, so a lookup on it never collides with
# an AgentConnect attempt pointer or a plain capture.
_REF_PREFIX = "okf:"

# Section headers the exporter appends after a claim body. Used ONLY as a tolerant
# fallback for a foreign bundle that lacks the explicit `BODY_END_MARKER` boundary;
# an exporter-produced document is split on the marker instead, so a claim body that
# itself contains one of these headings survives intact rather than being truncated.
_SCAFFOLD_HEADERS = ("## Sources", "## Superseded by", "## Contradicts")

# Structural frontmatter fields we retain on the candidate as "original frontmatter
# where safe". Deliberately excludes the free-text `title`: a hand-authored hostile
# bundle could plant a secret there, and this metadata is recallable. These are the
# short, controlled-vocabulary structural values (ids, scope, status, timestamps)
# plus `provenance`, which is free text. Retaining them is NOT the same as trusting
# them: every string value here is run through the SAME `memory_candidate` safety
# surface the claim body gets (see `_scan_retained` / `import_bundle`), so a secret,
# PII, or injection lure planted in `provenance` (or any other retained field) is
# masked, dropped fail-closed, or quarantined before it can reach recallable
# candidate metadata. The body is not the only scanned field.
_SAFE_BC_KEYS = (
    "status", "scope", "confidence", "trusted", "superseded_by",
    "contradictions", "provenance", "valid_from", "valid_until",
    "learned_at", "last_verified_at",
)


@dataclass
class ImportRequest:
    #: Directory containing the OKF bundle to import.
    bundle_dir: str
    #: The scope the operator assigns to every candidate this import creates. The
    #: bundle's own `scope` field is retained only as informational metadata — the
    #: OPERATOR governs blast radius, never the bundle.
    scope: Scope
    #: Who is importing. Recorded on every candidate. May be any proposer type,
    #: including `agent`; that never changes the pending outcome.
    imported_by: str
    imported_by_type: str = "human"
    #: Extra tags applied to every created candidate (in addition to bundle tags).
    tags: list[str] = field(default_factory=list)
    limits: "ValidationLimits | None" = None
    #: Validate + plan only. Creates nothing; reports what an import WOULD do.
    dry_run: bool = False


@dataclass
class DocResult:
    """The outcome for one claim document. Carries no matched/unsafe text."""
    document_path: str
    external_id: str
    #: created | duplicate | updated | conflict | rejected
    outcome: str
    candidate_ref: str = ""
    content_checksum: str = ""
    quarantined: bool = False
    redacted: bool = False
    #: Safety finding KINDS only (e.g. ["secret"], ["prompt_injection"]). Never a value.
    safety_kinds: list[str] = field(default_factory=list)
    #: For `updated`: the prior candidate(s) for the same external document.
    prior_candidates: list[str] = field(default_factory=list)
    #: For `conflict`: the canonical claim an import may NOT overwrite.
    conflicting_claim: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImportResult:
    bundle_dir: str
    scope: str
    imported_by: str
    imported_by_type: str
    okf_version: str = ""
    dry_run: bool = False
    #: True iff the bundle passed structural validation. When False, NOTHING is
    #: imported (no partial import) and every other list below is empty.
    valid: bool = False
    validation_errors: list[dict] = field(default_factory=list)
    validation_warnings: list[dict] = field(default_factory=list)
    #: candidate refs newly created (pending).
    created: list[str] = field(default_factory=list)
    #: candidate refs created for a CHANGED external document (also pending).
    updated: list[str] = field(default_factory=list)
    #: document paths that were an exact re-import (idempotent no-op).
    duplicates: list[str] = field(default_factory=list)
    #: document paths whose external id already traces to a PROMOTED claim; refused.
    conflicts: list[str] = field(default_factory=list)
    #: candidate refs created but QUARANTINED (need a human override to promote).
    quarantined: list[str] = field(default_factory=list)
    #: candidate refs whose stored body was MASKED by safety policy.
    redacted: list[str] = field(default_factory=list)
    #: document paths refused by a safety BLOCK (no candidate created).
    rejected: list[str] = field(default_factory=list)
    documents: list[DocResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


# --- document parsing --------------------------------------------------------
def _parse_front(text: str, limits: ValidationLimits) -> dict:
    """Parse a claim document's frontmatter with the validator's bounded parser.

    Never a full YAML loader: the same non-constructing subset parser the validator
    uses, so a hostile bundle cannot instantiate an object here. Returns `{}` on
    anything it cannot parse — the bundle already passed validation, so this is
    belt-and-braces, not the primary structural check.
    """
    try:
        split = _split_frontmatter(text)
    except Exception:
        return {}
    if not split:
        return {}
    yaml_text, _body = split
    try:
        front = _Frontmatter(yaml_text, limits.max_yaml_depth).parse()
    except Exception:
        return {}
    return front if isinstance(front, dict) else {}


def _body_start(lines: list[str]) -> int:
    """Index of the first body line, past leading blanks and the `# title` heading."""
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i < len(lines) and lines[i].startswith("# "):
        i += 1
    return i


def _extract_claim_text(text: str) -> str:
    """Recover the claim text from a claim document body.

    Drops the leading `# title` heading the exporter writes. The claim/scaffold
    boundary is taken from the explicit `BODY_END_MARKER` the exporter writes after
    every body: everything from the marker onward is bundle scaffolding, never claim
    text. This is unambiguous even when the claim body itself contains a `## Sources`
    heading. A foreign bundle that predates / omits the marker falls back to the
    tolerant heuristic of cutting at the first scaffolding heading. Either way the
    result is untrusted data a human reviews before promotion.
    """
    try:
        _front, body = split_frontmatter(text)
    except ValueError:
        body = text
    if BODY_END_MARKER in body:
        body = body.split(BODY_END_MARKER, 1)[0]
        lines = body.split("\n")
        return "\n".join(lines[_body_start(lines):]).strip()
    lines = body.split("\n")
    out: list[str] = []
    for ln in lines[_body_start(lines):]:
        if ln.strip() in _SCAFFOLD_HEADERS:
            break
        out.append(ln)
    return "\n".join(out).strip()


def _safe_frontmatter(front: dict) -> dict:
    """The structural, non-sensitive subset of a document's frontmatter.

    Only controlled-vocabulary mapping fields; never the free-text title or an
    unknown extension field whose value could carry a secret into recallable
    metadata. Values are truncated defensively.
    """
    out: dict = {}
    bc = front.get("brainconnect")
    if isinstance(bc, dict):
        keep: dict = {}
        for k in _SAFE_BC_KEYS:
            if k in bc:
                keep[k] = _bounded(bc[k])
        if keep:
            out = keep
    return out


def _bounded(v, _limit: int = 512):
    if isinstance(v, str):
        return v[:_limit]
    if isinstance(v, list):
        return [_bounded(x, _limit) for x in v[:64]]
    if isinstance(v, dict):
        return {str(k)[:64]: _bounded(x, _limit) for k, x in list(v.items())[:32]}
    return v


def _relationships(front: dict) -> dict:
    bc = front.get("brainconnect")
    rel: dict = {}
    if isinstance(bc, dict):
        sup = bc.get("superseded_by")
        if isinstance(sup, str) and sup:
            rel["superseded_by"] = sup
        con = bc.get("contradictions")
        if isinstance(con, list):
            rel["contradictions"] = [c for c in con if isinstance(c, str) and c]
    return rel


def _doc_tags(front: dict) -> list[str]:
    tags = front.get("tags")
    if isinstance(tags, list):
        return [t for t in tags if isinstance(t, str) and t]
    return []


# --- retained-metadata safety ------------------------------------------------
# Retained frontmatter (`_safe_frontmatter`, `_relationships`) is untrusted bundle
# content exactly like the claim body. A secret, PII, or injection lure planted in
# the free-text `provenance` (or any string nested inside a retained structure) must
# never land RAW in recallable candidate metadata (the `metadata` column,
# candidates.get(), candidates.listing(), db/dump.sql, log.md). So before any
# retained value is stored we route every string through the SAME `memory_candidate`
# surface the body gets, with the SAME fail-closed posture.

#: Sentinel: a retained value that could not be cleared and must NOT be stored.
_DROP = object()


def _new_agg() -> dict:
    """Accumulator for the metadata scan across all retained values in one doc."""
    return {"decision": safety.Decision.allow, "kinds": set(),
            "summaries": [], "dropped": 0, "redacted": False}


def _scan_one(repo: Repo, s: str, agg: dict):
    """Scan one retained free-text string on the `memory_candidate` surface.

    Returns the value to store: the masked text when policy redacts a secret / PII,
    the original text when it is clean, and `_DROP` when a required engine could not
    look (fail closed — unscanned free-text is never stored in recallable metadata,
    matching the body path's refusal to store what it could not clear).
    """
    if not s:
        return s
    verdict = safety.scan_for(repo, s, safety.MEMORY_CANDIDATE)
    if safety.at_least(verdict.decision, agg["decision"]):
        agg["decision"] = verdict.decision
    if not verdict.clean:
        agg["kinds"].update(verdict.kinds())
        agg["summaries"].append(verdict.summary())  # audit-safe: no matched text
    if verdict.has(safety.Category.scanner_error):
        # A required engine did not scan: this free-text is UNSCANNED, not clean.
        agg["dropped"] += 1
        return _DROP
    if verdict.redacted:
        agg["redacted"] = True
    return verdict.text


def _scan_retained(repo: Repo, value, agg: dict):
    """Recursively mask/drop every free-text string within a retained structure.

    Non-string leaves (bool `trusted`, numeric confidence, `None`) carry no free
    text and pass through untouched; a dropped string is removed from its container.
    """
    if isinstance(value, str):
        return _scan_one(repo, value, agg)
    if isinstance(value, list):
        out = []
        for x in value:
            r = _scan_retained(repo, x, agg)
            if r is not _DROP:
                out.append(r)
        return out
    if isinstance(value, dict):
        out = {}
        for k, x in value.items():
            r = _scan_retained(repo, x, agg)
            if r is not _DROP:
                out[k] = r
        return out
    return value


def _agg_touched(agg: dict) -> bool:
    return bool(agg["kinds"] or agg["dropped"])


def _agg_summary(agg: dict) -> dict:
    """An audit-safe record of what the retained-metadata scan saw. No matched text."""
    return {
        "decision": agg["decision"].value,
        "kinds": sorted(agg["kinds"]),
        "dropped_fields": agg["dropped"],
        "findings": agg["summaries"],
    }


# --- bundle scanning ---------------------------------------------------------
def _bundle_checksum(root: Path, limits: ValidationLimits) -> str:
    """A sha256 over the whole bundle's (relpath, bytes). The 'source checksum'.

    The bundle already passed validation (bounded size, no symlink escape), so this
    walk is safe. Regular files only; symlinks are skipped (validation reported them).
    """
    h = hashlib.sha256()
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        files.append(p)
    for p in files:
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
        h.update(b"\0")
    return h.hexdigest()


def _claim_docs(root: Path) -> list[tuple[str, Path]]:
    """Ordered `(relpath, path)` for every `claims/**.md` regular file."""
    claims_dir = root / "claims"
    if not claims_dir.is_dir():
        return []
    out = []
    for p in sorted(claims_dir.rglob("*.md")):
        if p.is_symlink() or not p.is_file():
            continue
        out.append((p.relative_to(root).as_posix(), p))
    return out


# --- canonical-state + idempotency lookups -----------------------------------
def _promoted_for(repo: Repo, source_ref: str):
    """The PROMOTED claim (with its import checksum) tracing to this external doc.

    Joins claims to the candidate they were promoted from and filters on the
    import source_ref. Returns `(claim_id, content_checksum)` or `None`. This is
    how import learns that an external id already owns a canonical claim — and
    therefore that a changed re-import is a CONFLICT, never an overwrite.
    """
    row = repo.one(
        """SELECT c.id AS claim_id, mc.metadata AS meta
             FROM claims c JOIN memory_candidates mc ON mc.id = c.candidate_id
            WHERE c.status = 'promoted' AND mc.source_ref = ?
            ORDER BY c.id DESC LIMIT 1""",
        (source_ref,))
    if not row:
        return None
    checksum = ""
    try:
        checksum = (json.loads(row["meta"] or "{}")
                    .get("okf_import", {}).get("content_checksum", ""))
    except (ValueError, AttributeError):
        checksum = ""
    return row["claim_id"], checksum


def _candidates_for(repo: Repo, source_ref: str) -> list[dict]:
    """Existing candidates (any status) for this external document."""
    rows = repo.q(
        "SELECT id, status, metadata FROM memory_candidates WHERE source_ref = ?"
        " ORDER BY id", (source_ref,))
    out = []
    for r in rows:
        try:
            meta = json.loads(r["metadata"] or "{}")
        except ValueError:
            meta = {}
        out.append({"id": r["id"], "status": r["status"],
                    "checksum": meta.get("okf_import", {}).get("content_checksum", "")})
    return out


# --- the importer ------------------------------------------------------------
def import_bundle(repo: Repo, request: ImportRequest) -> ImportResult:
    """Import an OKF bundle as PENDING candidates. Never promotes, by construction.

    Refuses an invalid bundle whole (no partial import). Scans every document
    through the `memory_candidate` safety surface before storing it. Refuses to
    overwrite any canonical claim an external id already owns.
    """
    limits = request.limits or ValidationLimits()
    scope = request.scope
    result = ImportResult(
        bundle_dir=str(Path(request.bundle_dir)),
        scope=str(scope), imported_by=request.imported_by,
        imported_by_type=request.imported_by_type, dry_run=request.dry_run)

    # (1) STRUCTURAL VALIDATION — reuse Stage 2. An invalid bundle is refused in
    # full; there is no partial import.
    verdict = validate_bundle(request.bundle_dir, limits)
    result.okf_version = verdict.okf_version
    result.validation_errors = [e.as_dict() for e in verdict.errors]
    result.validation_warnings = [w.as_dict() for w in verdict.warnings]
    result.valid = verdict.ok
    if not verdict.ok:
        result.warnings.append(
            f"bundle is structurally INVALID ({len(verdict.errors)} error(s)); "
            "nothing was imported (no partial import)")
        return result

    root = Path(request.bundle_dir).resolve()
    bundle_checksum = _bundle_checksum(root, limits)
    now = util.now_iso()

    for rel, path in _claim_docs(root):
        try:
            raw = path.read_bytes()
        except OSError as e:
            result.warnings.append(f"could not read {rel}: {e.strerror}")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            result.warnings.append(f"could not decode {rel} as UTF-8; skipped")
            continue
        # A Windows-authored bundle arrives with CRLF line endings; normalize
        # before parsing so the stored body is line-ending-independent (the
        # same posture validate.py documents for its own splitting). The
        # checksum above stays over the raw bytes.
        text = text.replace("\r\n", "\n")

        front = _parse_front(text, limits)
        bc = front.get("brainconnect") if isinstance(front, dict) else None
        external_id = ""
        if isinstance(bc, dict) and isinstance(bc.get("id"), str):
            external_id = bc["id"]
        if not external_id:
            external_id = f"path:{rel}"
        source_ref = _REF_PREFIX + external_id
        content_checksum = hashlib.sha256(raw).hexdigest()

        dr = DocResult(document_path=rel, external_id=external_id,
                       outcome="", content_checksum=content_checksum)

        # (canonical guard) An external id that already owns a PROMOTED claim may
        # never be overwritten by an import. If the content is byte-identical to
        # what was promoted, this is an idempotent no-op; if it differs, it is a
        # CONFLICT requiring operator action. Either way, no candidate is created
        # and the canonical claim is never touched.
        promoted = _promoted_for(repo, source_ref)
        if promoted is not None:
            claim_id, promoted_checksum = promoted
            dr.conflicting_claim = refs.claim(claim_id)
            if promoted_checksum and promoted_checksum == content_checksum:
                dr.outcome = "duplicate"
                dr.detail = (f"external id {external_id} already promoted to "
                             f"{refs.claim(claim_id)}; identical re-import ignored")
                result.duplicates.append(rel)
            else:
                dr.outcome = "conflict"
                dr.detail = (f"external id {external_id} already owns canonical "
                             f"{refs.claim(claim_id)}; a changed re-import cannot "
                             "overwrite it. Operator action required (supersede via "
                             "the normal claim governance path, not import).")
                result.conflicts.append(rel)
            result.documents.append(dr)
            continue

        # (idempotency / update) Compare against prior candidates for this external
        # document.
        existing = _candidates_for(repo, source_ref)
        identical = next((e for e in existing
                          if e["checksum"] and e["checksum"] == content_checksum
                          and e["status"] in ("pending", "promoted")), None)
        if identical is not None:
            dr.outcome = "duplicate"
            dr.candidate_ref = refs.candidate(identical["id"])
            dr.detail = (f"exact re-import of {refs.candidate(identical['id'])} "
                         f"({identical['status']}); no new candidate")
            result.duplicates.append(rel)
            result.documents.append(dr)
            continue

        is_update = bool(existing)
        if is_update:
            dr.prior_candidates = [refs.candidate(e["id"]) for e in existing]

        claim_text = _extract_claim_text(text)
        if not claim_text:
            result.warnings.append(f"{rel}: empty claim body; skipped")
            dr.outcome = "rejected"
            dr.detail = "empty claim body"
            result.rejected.append(rel)
            result.documents.append(dr)
            continue

        # (metadata SAFETY) Retained frontmatter is untrusted bundle content just
        # like the body. Mask secrets/PII, drop unscanned free-text fail-closed, and
        # flag high-risk injection BEFORE any of it is stored in recallable metadata.
        # `_scan_retained` recurses over every string (e.g. inside the `provenance`
        # dict); the aggregate verdict decides whether the whole candidate is
        # quarantined, on the same policy the body uses.
        meta_agg = _new_agg()
        safe_frontmatter = _scan_retained(repo, _safe_frontmatter(front), meta_agg)
        safe_relationships = _scan_retained(repo, _relationships(front), meta_agg)
        meta_quarantine = safety.at_least(meta_agg["decision"],
                                          safety.Decision.quarantine)
        okf_meta = {
            "bundle_path": str(Path(request.bundle_dir)),
            "bundle_checksum": bundle_checksum,
            "okf_version": verdict.okf_version,
            "document_path": rel,
            "external_id": external_id,
            "content_checksum": content_checksum,
            "imported_at": now,
            "imported_by": request.imported_by,
            "imported_by_type": request.imported_by_type,
            "relationships": safe_relationships,
            "frontmatter": safe_frontmatter,
        }
        if _agg_touched(meta_agg):
            okf_meta["metadata_safety"] = _agg_summary(meta_agg)
        meta = {"okf_import": okf_meta}
        if meta_quarantine:
            # A retained-metadata finding (injection / tool-control, or a required
            # engine that could not look) quarantines the candidate, exactly as the
            # body path does. `candidates.create_checked` only ever ADDS quarantine,
            # so pre-setting this survives its own body verdict.
            meta["quarantined"] = True

        if request.dry_run:
            dr.outcome = "updated" if is_update else "created"
            dr.quarantined = meta_quarantine
            if _agg_touched(meta_agg):
                dr.safety_kinds = sorted(meta_agg["kinds"])
            dr.detail = "dry-run: would create a pending candidate"
            (result.updated if is_update else result.created).append(rel)
            result.documents.append(dr)
            continue

        tags = sorted(set(_doc_tags(front)) | set(request.tags or []))
        # (3+4) SAFETY SCAN then PENDING candidate creation. create_checked scans
        # the `memory_candidate` surface, masks secrets BEFORE the text is written
        # anywhere, records a quarantine flag for injection / tool-control content,
        # and writes status='pending' unconditionally.
        harness = util.slug(f"{request.imported_by}-okf-{external_id}", 60)
        try:
            cid, sverdict = candmod.create_checked(
                repo, claim_text,
                proposed_by=request.imported_by,
                proposed_by_type=request.imported_by_type,
                source_ref=source_ref,
                proposed_scopes=[scope],
                tags=tags,
                metadata=meta,
                harness=harness)
        except candmod.SafetyRefused as e:
            # A safety BLOCK: nothing is stored. The attempt is recorded in the
            # result and the audit log — never the raw unsafe span.
            dr.outcome = "rejected"
            dr.safety_kinds = e.result.kinds()
            dr.detail = f"refused by safety policy: {e.result.reason()}"
            result.rejected.append(rel)
            result.documents.append(dr)
            repo.log("okf-import-rejected",
                     f"{rel} ({external_id}) refused: {e.result.reason()}")
            continue
        except (candmod.CandidateError, ingest.IngestError) as e:
            dr.outcome = "rejected"
            dr.detail = f"could not create candidate: {e}"
            result.rejected.append(rel)
            result.documents.append(dr)
            continue

        ref = refs.candidate(cid)
        dr.candidate_ref = ref
        dr.outcome = "updated" if is_update else "created"
        # The candidate is quarantined / redacted / flagged if EITHER the body OR the
        # retained metadata triggered it — the metadata scan is a peer of the body
        # scan, not an afterthought.
        dr.quarantined = candmod.safety.at_least(
            sverdict.decision, candmod.safety.Decision.quarantine) or meta_quarantine
        dr.redacted = sverdict.redacted or meta_agg["redacted"]
        if not sverdict.clean or meta_agg["kinds"]:
            dr.safety_kinds = sorted(set(sverdict.kinds()) | set(meta_agg["kinds"]))
        (result.updated if is_update else result.created).append(ref)
        if dr.quarantined:
            result.quarantined.append(ref)
        if dr.redacted:
            result.redacted.append(ref)
        detail = f"pending candidate {ref}"
        if dr.quarantined:
            detail += " [QUARANTINED]"
        elif dr.redacted:
            detail += " [redacted]"
        dr.detail = detail
        result.documents.append(dr)

    _finalize(repo, request, result)
    return result


def _finalize(repo: Repo, request: ImportRequest, result: ImportResult) -> None:
    """Record the import in the audit log. No-op writes are still auditable."""
    if request.dry_run:
        return
    summary = (f"okf import from {result.bundle_dir} by {request.imported_by} "
               f"({request.imported_by_type}) into {result.scope}: "
               f"{len(result.created)} created, {len(result.updated)} updated, "
               f"{len(result.duplicates)} duplicate, {len(result.conflicts)} conflict, "
               f"{len(result.rejected)} rejected, "
               f"{len(result.quarantined)} quarantined")
    # create_checked already finalized each candidate; this is a summary line so an
    # import that created nothing (all duplicates/conflicts/rejects) still leaves a
    # provenance record that an import was ATTEMPTED.
    repo.finalize("okf-import", summary)
