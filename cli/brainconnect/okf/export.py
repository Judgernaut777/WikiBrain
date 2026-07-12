"""OKF export — projecting the canonical ledger into a portable Markdown bundle.

The ledger is canonical; the bundle is a **projection**. This module reads the
ledger, applies the recall output safety policy to every claim body, and writes a
self-contained directory. It performs **no writes to the ledger** — no `finalize`,
no `UPDATE`, no `INSERT`. That invariant is asserted by the acceptance suite by
fingerprinting every table before and after an export.

Four properties this file is responsible for:

  * **Deterministic + reproducible.** Everything is ordered by id; no wall-clock
    time is ever written into the bundle; frontmatter key order is fixed. Identical
    ledger state + identical request produce a byte-identical bundle.
  * **Atomic.** The bundle is built in a sibling staging directory, structurally
    self-validated, and only then swapped into place. A mid-write failure removes
    the staging directory and leaves any existing bundle untouched — never a
    half-written bundle.
  * **Safe.** Before any human/agent-readable body is written, the text runs
    through the existing `memory_recall` safety surface (docs/SAFETY.md). Secrets
    and PII are masked; high-risk injection / tool-control content is WITHHELD with
    a visible warning; the exported safety metadata never contains matched text and
    never the raw unsafe span. Canonical claim text in the ledger is never mutated.
  * **Validated before success.** What was written is re-read and structurally
    checked (frontmatter present, OKF version pinned, ids match filenames, relative
    links resolve, no withheld body leaks its canonical text) before the swap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from ..db import Repo
from .. import confidence as conf, refs, safety, scopes as scopesmod, trust
from ..scopes import Scope
from . import yamlfmt
from .model import ExportRequest, ExportResult

#: The one OKF version this exporter writes. Pinned into every bundle. A reader
#: keys its parser off this, and a future importer rejects an unsupported major.
OKF_VERSION = "0.1"
FORMAT_NAME = "okf"

#: Never projected, under any flag — mirrors recall's NEVER_RECALLED. A rejected
#: or archived claim is a decision of record, not current knowledge.
_NEVER_EXPORTED = ("rejected", "archived")

#: What a withheld claim's body says instead of its text. Deterministic and
#: text-free, so it is safe as a title and cannot leak the original.
_WITHHELD_BODY = "_Body withheld by safety policy. The claim remains in the ledger._"

#: An explicit, unambiguous machine boundary written after every claim body and
#: before any appended scaffolding (## Sources / ## Superseded by / ## Contradicts).
#: The importer splits on THIS, not on a human `##` heading, so a claim body that
#: itself contains a `## Sources` heading is no longer truncated on import — the
#: body/scaffold boundary is machine-defined, not guessed. It is an HTML comment,
#: so it renders invisibly and carries no markdown link (validation is unaffected).
BODY_END_MARKER = "<!-- okf:body-end -->"

_LINK_RE = re.compile(r"\]\(([^)]+)\)")


class ExportError(Exception):
    pass


# --- claim selection ---------------------------------------------------------
def _open_contradictions(repo: Repo) -> dict[int, set[int]]:
    """`claim_id -> set of claim_ids it is in an OPEN contradiction with`."""
    out: dict[int, set[int]] = {}
    for r in repo.q(
            "SELECT claim_a, claim_b FROM contradictions WHERE status='open'"):
        a, b = r["claim_a"], r["claim_b"]
        out.setdefault(a, set()).add(b)
        out.setdefault(b, set()).add(a)
    return out


def _select_claims(repo: Repo, req: ExportRequest, contradicted: dict[int, set[int]]):
    """Ordered claim rows for this request. Ordering is by id, always."""
    rows = repo.q("SELECT * FROM claims ORDER BY id")
    out = []
    for row in rows:
        status = row["status"]
        if status in _NEVER_EXPORTED:
            continue
        if status == "superseded" and not req.include_superseded:
            continue
        claim_scope = Scope(row["scope_type"], row["scope_id"])
        if req.scopes and not scopesmod.matches(claim_scope, req.scopes):
            continue
        if req.trusted_only:
            trusted = trust.is_trusted(
                status=status, contradicted=row["id"] in contradicted)
            if not trusted:
                continue
        out.append(row)
    return out


# --- per-claim document ------------------------------------------------------
def _safe_safety_block(verdict) -> dict:
    """A non-sensitive projection of the safety verdict for the frontmatter.

    Deliberately narrower than `verdict.summary()`: engine `message` strings are
    dropped even though the safety contract guarantees they carry no matched text.
    A bundle is written to disk and shared; the export surface emits only decision,
    kinds, and per-finding rule/severity/span/engine attribution. Never a value.
    """
    findings = []
    for f in verdict.findings:
        fd = {"engine": f.engine, "kind": f.kind.value, "rule": f.rule,
              "severity": f.severity.value}
        if f.has_span:
            fd["span"] = [f.start, f.end]
        findings.append(fd)
    return {
        "decision": verdict.decision.value,
        "kinds": verdict.kinds(),
        "redacted": verdict.redacted,
        "findings": findings,
    }


def _sources_for(repo: Repo, claim_id: int) -> list[dict]:
    rows = repo.q(
        """SELECT cs.source_id, cs.evidence_type, cs.quote_or_pointer,
                  s.title, s.origin, s.url
             FROM claim_sources cs JOIN sources s ON s.id = cs.source_id
            WHERE cs.claim_id = ? ORDER BY cs.source_id, cs.id""", (claim_id,))
    out = []
    for r in rows:
        d = {"id": refs.source(r["source_id"]), "evidence_type": r["evidence_type"],
             "origin": r["origin"]}
        if r["title"]:
            d["title"] = r["title"]
        if r["url"]:
            d["url"] = r["url"]
        if r["quote_or_pointer"]:
            d["quote_or_pointer"] = r["quote_or_pointer"]
        out.append(d)
    return out


def _title_from(text: str, fallback: str) -> str:
    line = ""
    for ln in text.strip().splitlines():
        if ln.strip():
            line = ln.strip()
            break
    line = line[:72].rstrip()
    return line or fallback


class _Doc:
    """A planned file: its bundle-relative path, its bytes, and cross-checks."""

    def __init__(self, relpath: str, content: str, *,
                 links: list[str] | None = None,
                 forbid_text: str | None = None):
        self.relpath = relpath
        self.content = content
        self.links = links or []
        #: If set, the self-check asserts this exact text is absent from `content`
        #: (a withheld claim must never leak its canonical body).
        self.forbid_text = forbid_text


def _claim_doc(repo: Repo, row, *, contradicted: dict[int, set[int]],
               result: ExportResult) -> tuple["_Doc", int]:
    cid = row["id"]
    ref = refs.claim(cid)
    status = row["status"]
    is_contradicted = cid in contradicted
    trusted = trust.is_trusted(status=status, contradicted=is_contradicted)

    # Safety on the way out: the recall output policy, applied to the body.
    verdict = safety.scan_for(repo, row["text"], safety.MEMORY_RECALL)
    withheld = safety.at_least(verdict.decision, safety.Decision.quarantine)

    forbid = None
    if withheld:
        body = _WITHHELD_BODY
        forbid = row["text"]
        result.withheld.append({"id": ref, "reason": verdict.reason()})
    elif verdict.redacted:
        body = verdict.text                    # masked representation only
        result.redacted.append(ref)
    else:
        body = row["text"]

    safe_title = _title_from(_WITHHELD_BODY if withheld else body, ref)

    scope = Scope(row["scope_type"], row["scope_id"])
    sources = _sources_for(repo, cid)
    tags = json.loads(row["tags"] or "[]")

    bc: dict = {
        "id": ref,
        "status": status,
        "trusted": trusted,
        "scope": str(scope),
        "confidence": conf.label_of(row),
        "sources": sources,
    }
    for key in ("valid_from", "valid_until", "learned_at", "last_verified_at"):
        if row[key]:
            bc[key] = row[key]
    if row["superseded_by"]:
        bc["superseded_by"] = refs.claim(row["superseded_by"])
    if is_contradicted:
        bc["contradictions"] = [refs.claim(o) for o in sorted(contradicted[cid])]
    provenance = {"origin": row["origin"]}
    if row["promoted_by"]:
        provenance["promoted_by"] = row["promoted_by"]
    if row["candidate_id"]:
        provenance["candidate_id"] = refs.candidate(row["candidate_id"])
    bc["provenance"] = provenance
    if not verdict.clean:
        bc["safety"] = _safe_safety_block(verdict)

    front = {
        "title": safe_title,
        "okf_version": OKF_VERSION,
        "brainconnect": bc,
    }
    if tags:
        front["tags"] = tags

    # Body + relationship links (relative, resolvable inside the bundle). An explicit
    # machine marker terminates the body so the importer never has to guess where the
    # claim text ends and appended scaffolding begins (a body may legitimately contain
    # a `## Sources` heading of its own).
    parts = [yamlfmt.frontmatter(front), f"# {safe_title}\n", body.rstrip() + "\n",
             f"\n{BODY_END_MARKER}\n"]
    links: list[str] = []
    if sources:
        parts.append("\n## Sources\n")
        for s in sources:
            anchor = s["id"]
            parts.append(f"- [{anchor}](../sources/source-index.md#{anchor}) "
                         f"({s['evidence_type']}, {s['origin']})\n")
        links.append("sources/source-index.md")
    if row["superseded_by"]:
        target = refs.claim(row["superseded_by"])
        parts.append("\n## Superseded by\n")
        parts.append(f"- [{target}]({target}.md)\n")
        links.append(f"claims/{target}.md")
    if is_contradicted:
        parts.append("\n## Contradicts\n")
        for o in sorted(contradicted[cid]):
            target = refs.claim(o)
            parts.append(f"- [{target}]({target}.md)\n")
            links.append(f"claims/{target}.md")

    doc = _Doc(f"claims/{ref}.md", "".join(parts), links=links, forbid_text=forbid)
    return doc, cid


# --- index / sources / history ----------------------------------------------
def _index_doc(req: ExportRequest, claim_rows, exported_ids: set[int],
               contradicted, result: ExportResult, source_count: int) -> _Doc:
    scope_filter = ", ".join(str(s) for s in req.scopes) if req.scopes else "(all scopes)"
    lines = [
        f"# Knowledge bundle (OKF {OKF_VERSION})\n",
        "\nA portable **projection** of a BrainConnect ledger. The ledger is "
        "canonical; this bundle is read-only knowledge. OKF-valid does not mean "
        "trusted, promoted, or safe.\n",
        "\n## Bundle\n",
        f"- format: `{FORMAT_NAME}` {OKF_VERSION}\n",
        f"- claims: {len(claim_rows)}\n",
        f"- sources: {source_count}\n",
        f"- scope filter: {scope_filter}\n",
        f"- trusted only: {str(req.trusted_only).lower()}\n",
        f"- includes superseded: {str(req.include_superseded).lower()}\n",
    ]
    links = ["sources/source-index.md"]
    lines.append("\n## Claims\n")
    for row in claim_rows:
        ref = refs.claim(row["id"])
        trusted = trust.is_trusted(
            status=row["status"], contradicted=row["id"] in contradicted)
        flag = "trusted" if trusted else f"{row['status']}, UNTRUSTED"
        lines.append(f"- [{ref}](claims/{ref}.md) — {flag}\n")
        links.append(f"claims/{ref}.md")
    if result.withheld:
        lines.append("\n## Withheld by safety policy\n")
        lines.append("These claims are present as documents but their body text was "
                     "withheld; nothing was deleted from the ledger.\n")
        for w in result.withheld:
            lines.append(f"- {w['id']} — {w['reason']}\n")
    lines.append("\n## Sources\n")
    lines.append("- [source-index](sources/source-index.md)\n")
    if req.include_superseded:
        lines.append("\n## History\n")
        lines.append("- [supersession log](history/log.md)\n")
        links.append("history/log.md")
    return _Doc("index.md", "".join(lines), links=links)


def _source_index_doc(repo: Repo, exported_ids: set[int]) -> tuple[_Doc, int]:
    # Which sources do the exported claims cite? (primary + join rows.)
    used: dict[int, set[int]] = {}
    if exported_ids:
        marks = ",".join("?" for _ in exported_ids)
        ids = list(exported_ids)
        for r in repo.q(
                f"SELECT id, source_id FROM claims WHERE id IN ({marks})", ids):
            used.setdefault(r["source_id"], set()).add(r["id"])
        for r in repo.q(
                f"SELECT claim_id, source_id FROM claim_sources "
                f"WHERE claim_id IN ({marks})", ids):
            used.setdefault(r["source_id"], set()).add(r["claim_id"])
    lines = ["# Sources\n",
             "\nEvidence the exported claims cite. A source is evidence, never a "
             "trusted fact on its own.\n"]
    for sid in sorted(used):
        s = repo.one("SELECT * FROM sources WHERE id = ?", (sid,))
        if s is None:
            continue
        ref = refs.source(sid)
        lines.append(f"\n## {ref} {{#{ref}}}\n")
        lines.append(f"- origin: {s['origin']}\n")
        if s["title"]:
            lines.append(f"- title: {s['title']}\n")
        if s["url"]:
            lines.append(f"- url: {s['url']}\n")
        cited = ", ".join(refs.claim(c) for c in sorted(used[sid]))
        lines.append(f"- referenced by: {cited}\n")
    return _Doc("sources/source-index.md", "".join(lines)), len(used)


def _history_doc(repo: Repo, exported_ids: set[int]) -> _Doc:
    lines = ["# History\n", "\n## Supersessions\n"]
    rows = repo.q(
        "SELECT old_claim_id, new_claim_id, reason, created_at, created_by "
        "FROM supersessions ORDER BY old_claim_id, new_claim_id")
    any_row = False
    links: list[str] = []
    for r in rows:
        if exported_ids and r["old_claim_id"] not in exported_ids \
                and r["new_claim_id"] not in exported_ids:
            continue
        any_row = True
        old, new = refs.claim(r["old_claim_id"]), refs.claim(r["new_claim_id"])
        reason = f" — {r['reason']}" if r["reason"] else ""
        by = f" (by {r['created_by']})" if r["created_by"] else ""
        when = f" at {r['created_at']}" if r["created_at"] else ""
        lines.append(f"- {old} superseded by {new}{reason}{by}{when}\n")
    if not any_row:
        lines.append("- (none)\n")
    return _Doc("history/log.md", "".join(lines), links=links)


# --- write / validate / swap -------------------------------------------------
def _write_bundle(staging: Path, docs: list[_Doc], fault_hook=None) -> None:
    staging.mkdir(parents=True, exist_ok=False)
    (staging / ".okf-bundle").write_text(
        f"format={FORMAT_NAME}\nversion={OKF_VERSION}\n", encoding="utf-8")
    for i, doc in enumerate(docs):
        if fault_hook is not None:
            fault_hook(i, doc)          # test seam: raise to simulate a mid-write fault
        target = staging / doc.relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(doc.content, encoding="utf-8", newline="\n")


def _self_validate(staging: Path, docs: list[_Doc]) -> None:
    """Structural self-check of what was just written. Raises on any defect."""
    for doc in docs:
        p = staging / doc.relpath
        if not p.is_file():
            raise ExportError(f"self-check: missing file {doc.relpath}")
        written = p.read_text(encoding="utf-8")
        if written != doc.content:
            raise ExportError(f"self-check: {doc.relpath} does not match its plan")
        if doc.forbid_text and doc.forbid_text in written:
            raise ExportError(
                f"self-check: withheld body leaked into {doc.relpath}")
        # claim docs must carry parseable frontmatter, a pinned OKF version, and an
        # id that matches the filename.
        if doc.relpath.startswith("claims/"):
            front, _ = yamlfmt.split_frontmatter(written)
            if f'okf_version: "{OKF_VERSION}"' not in front:
                raise ExportError(
                    f"self-check: {doc.relpath} missing pinned okf_version")
            stem = Path(doc.relpath).stem
            if f'id: "{stem}"' not in front:
                raise ExportError(
                    f"self-check: {doc.relpath} id does not match filename")
        # every relative link resolves inside the bundle.
        for m in _LINK_RE.finditer(written):
            link = m.group(1).split("#", 1)[0]
            if not link or link.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (p.parent / link).resolve()
            if not resolved.is_file():
                raise ExportError(
                    f"self-check: broken relative link {link!r} in {doc.relpath}")


def _digest(docs: list[_Doc]) -> str:
    h = hashlib.sha256()
    for doc in sorted(docs, key=lambda d: d.relpath):
        h.update(doc.relpath.encode("utf-8"))
        h.update(b"\0")
        h.update(doc.content.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _swap_into_place(staging: Path, out: Path) -> None:
    """Atomically replace `out` with `staging` (same parent -> atomic rename).

    An existing bundle is moved aside first and only removed once the new bundle is
    in place; a failure during the swap restores it. A non-empty directory that is
    not itself an OKF bundle is refused rather than clobbered.
    """
    if out.exists():
        if not out.is_dir():
            raise ExportError(f"output path {out} exists and is not a directory")
        if any(out.iterdir()) and not (out / ".okf-bundle").is_file():
            raise ExportError(
                f"refusing to overwrite {out}: it is not an OKF bundle "
                "(no .okf-bundle marker). Choose an empty or new directory.")
    token = os.urandom(8).hex()
    backup = out.parent / f".{out.name}.okf-backup-{token}"
    moved = False
    if out.exists():
        os.rename(out, backup)
        moved = True
    try:
        os.rename(staging, out)
    except Exception:
        if moved:
            os.rename(backup, out)      # restore the prior bundle
        raise
    if moved:
        shutil.rmtree(backup, ignore_errors=True)


def export_bundle(repo: Repo, req: ExportRequest, *, _fault_hook=None) -> ExportResult:
    """Export the ledger as an OKF bundle. Never mutates the ledger.

    `_fault_hook(index, doc)` is a private test seam: raising from it simulates a
    mid-write failure, which must leave no partial bundle.
    """
    contradicted = _open_contradictions(repo)
    claim_rows = _select_claims(repo, req, contradicted)
    exported_ids = {r["id"] for r in claim_rows}

    result = ExportResult(
        output_dir=str(Path(req.output_dir)), format_name=FORMAT_NAME,
        okf_version=OKF_VERSION, claim_count=len(claim_rows), source_count=0)

    docs: list[_Doc] = []
    for row in claim_rows:
        doc, _ = _claim_doc(repo, row, contradicted=contradicted, result=result)
        docs.append(doc)

    source_doc, source_count = _source_index_doc(repo, exported_ids)
    result.source_count = source_count

    index = _index_doc(req, claim_rows, exported_ids, contradicted, result,
                       source_count)
    docs.append(index)
    docs.append(source_doc)
    if req.include_superseded:
        docs.append(_history_doc(repo, exported_ids))

    if result.withheld:
        result.warnings.append(
            f"{len(result.withheld)} claim(s) had their body WITHHELD by safety "
            "policy; they appear as documents without body text. Nothing was "
            "deleted from the ledger.")
    if result.redacted:
        result.warnings.append(
            f"{len(result.redacted)} claim(s) had content MASKED by safety policy; "
            "the ledger text is unchanged.")

    result.files = sorted(d.relpath for d in docs) + [".okf-bundle"]
    result.bundle_digest = _digest(docs)

    out = Path(req.output_dir).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    token = os.urandom(8).hex()
    staging = out.parent / f".{out.name}.okf-staging-{token}"
    try:
        _write_bundle(staging, docs, fault_hook=_fault_hook)
        _self_validate(staging, docs)
        _swap_into_place(staging, out)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return result
