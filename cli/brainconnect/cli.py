"""`brainconnect` command-line entry point. Pure code — zero model calls (§1)."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import Config
from .db import Repo, init_db
from . import (ingest, search as searchmod, queue as queuemod, render as rendermod,
               lint as lintmod, health as healthmod, gather, gate as gatemod,
               review, fetch as fetchmod, drop as dropmod, extract as extractmod,
               embed as embedmod, skills as skillsmod, mcp_server as mcpmod,
               evidence as evidencemod, triage as triagemod)
from . import (api as apimod, backends, candidates as candmod,
               confidence as confmod, feedback as feedbackmod,
               profiles as profilesmod, refs, safety as safetymod,
               scopes as scopesmod)
from . import server as servermod
from . import backup as backupmod

# Ledger errors that are a user mistake at the terminal, not a bug: print the
# message and exit non-zero rather than dumping a traceback.
_LEDGER_ERRORS = (
    candmod.CandidateError, feedbackmod.FeedbackError, scopesmod.ScopeError,
    confmod.ConfidenceError, profilesmod.ProfileError, refs.RefError,
    backends.BackendError, apimod.ApiError,
)

SCAFFOLD_DIRS = [
    "raw", "raw/assets", "inbox",
    "wiki", "wiki/entities", "wiki/concepts", "wiki/sources", "wiki/syntheses",
    "wiki/digests", "db",
]


def _emit(obj, as_json: bool):
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return True
    return False


def _whoami() -> str:
    """Default reviewer for the human-gated levers: whoever is at the terminal."""
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry (containers); the flag is still accepted
        return "human"


def _spawn_librarian(cfg: Config, source_ids: list[int]):
    """Fire-and-forget `brainconnect-librarian extract` for freshly-ingested sources.

    Opt-in via `[librarian] auto_extract` in config.toml. The model call happens
    in a SEPARATE detached process — this CLI itself still makes zero model
    calls. If the librarian is missing or fails, sources simply stay 'new' and
    `brainconnect-librarian catch-up` (or a session) picks them up later.
    """
    if not source_ids or not cfg.data.get("librarian", {}).get("auto_extract"):
        return
    import os
    kwargs: dict = {"cwd": str(cfg.root), "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlive this console.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    for sid in source_ids:
        try:
            subprocess.Popen([sys.executable, "-m", "librarian",
                              "extract", "--source", str(sid)], **kwargs)
            print(f"librarian: extraction started for source #{sid}")
        except OSError as e:
            print(f"librarian: could not start ({e}); "
                  f"run `brainconnect-librarian catch-up` later")
            return


# --- init -------------------------------------------------------------------
def cmd_init(args):
    cfg = Config.load()
    root = cfg.root
    for d in SCAFFOLD_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    cfg_path = root / "config.toml"
    if not cfg_path.exists():
        # Every other command finds the repo root via the nearest config.toml
        # ancestor, so an init that writes none leaves behind a directory in
        # which `brainconnect health` / `brainconnect serve` immediately fail
        # with "not inside a wiki-brain repo". Write a minimal config pointing
        # at the DB this init resolved (env override included); never touch an
        # existing config.toml.
        cfg_path.write_text(
            "# Written by `brainconnect init`. Minimal config; every key is\n"
            "# optional and documented in config.example.toml.\n"
            f'[paths]\ndb = "{cfg.db_path.as_posix()}"\n',
            encoding="utf-8")
        print(f"wrote {cfg_path}")
    log = root / "log.md"
    if not log.exists():
        log.write_text("# Operations log\n\n", encoding="utf-8")
    tlist = root / "inbox" / "_transcripts.list"
    if not tlist.exists():
        tlist.write_text("", encoding="utf-8")

    db_path = cfg.db_path
    if db_path.exists():
        print(f"DB already exists at {db_path} (leaving as-is)")
        # must_exist=False: config.toml may not exist yet in a fresh directory
        # (that's exactly the case `brainconnect init` handles) — the "not inside a
        # wiki-brain repo" guard in Repo.open() must not fire here.
        repo = Repo.open(must_exist=False)
    else:
        repo = init_db()
        print(f"created DB at {db_path}")
    repo.dump()
    repo.log("init", f"db {db_path}")
    repo.close()
    print(f"repo root: {root}")


# --- ingest -----------------------------------------------------------------
def cmd_add(args):
    with Repo.open() as repo:
        try:
            sid, warns = ingest.add(repo, args.target, origin=args.origin, title=args.title)
        except (ingest.IngestError, fetchmod.FetchError) as e:
            sys.exit(f"error: {e}")
        for w in warns:
            print(w)
        print(f"added source #{sid} (origin {args.origin})")
    _spawn_librarian(Config.load(), [sid])


def cmd_pending(args):
    with Repo.open() as repo:
        rows = repo.q("SELECT * FROM sources WHERE status='new' ORDER BY id")
        if _emit([dict(r) for r in rows], args.json):
            return
        if not rows:
            print("no sources awaiting extraction")
            return
        print(f"{len(rows)} source(s) needing extraction:")
        for r in rows:
            print(f"  #{r['id']:<4} [{r['origin']}] {r['title'] or r['path']}")


def cmd_file_claims(args):
    with Repo.open() as repo:
        try:
            res = ingest.file_claims(repo, args.source, args.json_file,
                                     refile=args.refile)
        except ingest.IngestError as e:
            sys.exit(f"error: {e}")
        print(f"filed: {res['claims']} claims, summary={res['summary']}, "
              f"{res['contradictions']} contradiction(s), {res['questions']} question(s)"
              + (", escalated" if res['escalated'] else ""))
        if res.get("filed", {}).get("moved"):
            print(f"evidence: {res['filed']['old_path']} -> {res['filed']['new_path']}")


def cmd_capture(args):
    """Propose a memory. Files evidence + a PENDING candidate; never promotes."""
    text = (args.text or " ".join(args.text_pos or [])).strip()
    if not text:
        sys.exit("error: nothing to capture (pass text positionally or with --text)")
    with Repo.open() as repo:
        try:
            scope_list = [scopesmod.parse(s) for s in (args.scope or [])]
            source_id = (refs.parse(args.source, refs.SOURCE)
                         if args.source else None)
            cid, verdict = candmod.create_checked(
                repo, text, proposed_by=(args.proposed_by or args.origin),
                proposed_by_type=args.proposed_by_type, source_id=source_id,
                source_ref=args.source_ref, task_id=args.task_id,
                proposed_scopes=scope_list, tags=args.tags or [],
                harness=args.origin)
        except (ingest.IngestError, *_LEDGER_ERRORS) as e:
            sys.exit(f"error: {e}")
        row = repo.one("SELECT source_id FROM memory_candidates WHERE id=?", (cid,))
        sid = row["source_id"]
        quarantined = safetymod.at_least(verdict.decision,
                                         safetymod.Decision.quarantine)
        out = {"candidate_id": refs.candidate(cid), "source_id": sid,
               "status": "pending"}
        if not verdict.clean:
            out["safety"] = verdict.summary()
        if _emit(out, args.json):
            return
        print(f"captured as {refs.candidate(cid)} (pending) "
              f"backed by source #{sid}")
        if verdict.redacted:
            print(f"safety: masked content in it ({verdict.reason()}); "
                  "the original text was not stored.")
        if quarantined:
            print(f"safety: QUARANTINED ({verdict.reason()}). It cannot be "
                  "promoted without `brainconnect promote --safety-override "
                  "--override-reason ...`.")
        print("It is unvetted; a human must `brainconnect promote` it before it is "
              "returned by trusted recall.")
    _spawn_librarian(Config.load(), [sid])


def cmd_drop(args):
    with Repo.open() as repo:
        results = dropmod.scan(repo, move=False if args.no_move else None)
    ingested = [r for r in results if r["source_id"]]
    _spawn_librarian(Config.load(), [r["source_id"] for r in ingested])
    if _emit(results, args.json):
        return
    if not results:
        print("no ingestion folders configured, or all are empty")
        return
    warned = [r for r in results if r["warning"]]
    print(f"drop: ingested {len(ingested)} file(s)"
          + (f", {len(warned)} skipped" if warned else ""))
    for r in ingested:
        print(f"  + #{r['source_id']} [{r['kind']}] {r['file']}")
    for r in warned:
        print(f"  ! {r['file']}: {r['warning']}")


def cmd_transcribe(args):
    with Repo.open() as repo:
        try:
            sid = ingest.transcribe(
                repo, args.target,
                whisper_model=repo.cfg.extract_cfg("whisper_model") or "base")
        except (ingest.IngestError, extractmod.ExtractError) as e:
            sys.exit(f"error: {e}")
    print(f"transcribed source #{sid} (origin transcript)")
    _spawn_librarian(Config.load(), [sid])


def cmd_dump(args):
    with Repo.open() as repo:
        repo.dump()
        print("db/dump.sql refreshed")


def cmd_evidence(args):
    with Repo.open() as repo:
        try:
            if args.ecmd == "file":
                if args.source is not None:
                    results = [evidencemod.file_source(repo, args.source)]
                else:
                    results = evidencemod.file_all(repo, extracted_only=not args.include_new)
                idx = evidencemod.write_index(repo)
                moved = sum(1 for r in results if r["moved"])
                errors = [r for r in results if r.get("error")]
                repo.finalize("evidence-file", f"{moved}/{len(results)} moved; index {idx}")
                if _emit(results, args.json):
                    return
                print(f"evidence filed: {moved}/{len(results)} moved"
                      + (f", {len(errors)} error(s)" if errors else ""))
                for r in results:
                    if r.get("error"):
                        print(f"  ! #{r['source_id']}: {r['error']}")
                        continue
                    mark = "moved" if r["moved"] else "ok"
                    print(f"  #{r['source_id']} [{r['bucket']}] {mark}: {r['new_path']}")
            elif args.ecmd == "index":
                idx = evidencemod.write_index(repo)
                repo.finalize("evidence-index", idx)
                if _emit({"path": idx}, args.json):
                    return
                print(f"evidence index written: {idx}")
        except evidencemod.EvidenceError as e:
            sys.exit(f"error: {e}")


# --- search / graph ---------------------------------------------------------
def cmd_search(args):
    with Repo.open() as repo:
        terms = " ".join(args.terms)
        try:
            if args.semantic:
                res = embedmod.semantic_search(repo, terms, promoted_only=args.promoted_only)
            elif args.hybrid:
                res = embedmod.hybrid_search(repo, terms, promoted_only=args.promoted_only)
            else:
                res = searchmod.search(repo, terms, promoted_only=args.promoted_only,
                                        limit=args.limit)
        except embedmod.EmbedError as e:
            sys.exit(f"error: {e}")
        if _emit(res, args.json):
            return
        if not res:
            print("no matches")
            return
        for r in res:
            if r.get("kind") == "claim":
                extra = (f" score={r['score']}" if "score" in r
                         else f" rrf={r['rrf']}" if "rrf" in r else "")
                src = f": {r['source_title']}" if r.get("source_title") else ""
                print(f"  claim #{r['id']} [{r.get('status','?')}/{r.get('origin','?')}]"
                      f" (src #{r['source_id']}{src}){extra}")
                print(f"    {r['text']}")
            else:
                print(f"  summary #{r['id']} [{r.get('status','?')}] (src #{r['source_id']})")
                print(f"    {r['text']}")


def cmd_embed(args):
    with Repo.open() as repo:
        try:
            n = embedmod.index(repo, only_missing=not args.all)
        except embedmod.EmbedError as e:
            sys.exit(f"error: {e}")
        print(f"embedded {n} claim(s)")


def cmd_graph(args):
    with Repo.open() as repo:
        res = searchmod.graph(repo, args.entity, hops=args.hops,
                              promoted_only=args.promoted_only)
        if _emit(res, args.json):
            return
        print(f"graph for '{res['entity']}' (≤{res['hops']} hop(s)):")
        if not res["edges"]:
            print("  (no relations)")
        for e in res["edges"]:
            ev = f"  [claim #{e['claim_id']}]" if e["claim_id"] else ""
            print(f"  {e['src']} --{e['rel']}--> {e['dst']}{ev}")


# --- queue ------------------------------------------------------------------
def cmd_queue(args):
    with Repo.open() as repo:
        if args.qcmd == "add":
            qid = queuemod.add(repo, args.question, priority=args.priority, origin=args.origin)
            print(f"queued #{qid}")
        elif args.qcmd == "list":
            rows = queuemod.listing(repo, args.status)
            if _emit([dict(r) for r in rows], args.json):
                return
            for r in rows:
                print(f"  q#{r['id']} [{r['status']}] p={r['priority']} "
                      f"a={r['attempts']} ({r['origin']}) {r['question']}")
        elif args.qcmd == "next":
            row = queuemod.next_open(repo)
            if _emit(dict(row) if row else None, args.json):
                return
            if not row:
                print("queue empty")
            else:
                print(f"q#{row['id']} (p={row['priority']}, attempts={row['attempts']}): "
                      f"{row['question']}")
        elif args.qcmd == "done":
            queuemod.done(repo, args.id, note=args.note)
            print(f"q#{args.id} marked done")
        elif args.qcmd == "attempt":
            st = queuemod.attempt(repo, args.id)
            print(f"q#{args.id} now {st}")


# --- render / synthesis / commit --------------------------------------------
def cmd_render(args):
    with Repo.open() as repo:
        rep = rendermod.render(repo, all_pages=args.all)
        if _emit(rep, args.json):
            return
        print(f"rendered {len(rep['rendered'])} page(s); changed={rep['changed']}")
        if rep["needs_synthesis_review"]:
            print("needs synthesis review:")
            for p in rep["needs_synthesis_review"]:
                print(f"  {p}")


def cmd_digest(args):
    with Repo.open() as repo:
        path = rendermod.ensure_digest(repo, args.day)
        rendermod.render(repo)
        print(f"digest written: {path}")


def cmd_synthesis(args):
    with Repo.open() as repo:
        if args.scmd == "get":
            print(rendermod.synthesis_get(repo, args.page))
        elif args.scmd == "set":
            text = args.text
            if text == "-" or text is None:
                text = sys.stdin.read()
            rendermod.synthesis_set(repo, args.page, text)
            print(f"synthesis set for {args.page}")


def cmd_commit(args):
    with Repo.open() as repo:
        root = repo.root
    # Stage everything not covered by .gitignore. In this code/design repo the
    # brain's personal content (raw/, inbox/, wiki/, db/dump.sql, config.toml,
    # log.md) is git-ignored, so it is never staged or published; un-ignore those
    # paths locally if you want a private repo that versions your knowledge too.
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    r = subprocess.run(["git", "-C", str(root), "commit", "-m", args.message])
    if r.returncode != 0:
        print("(nothing to commit or commit failed)")


# --- lint / health ----------------------------------------------------------
def cmd_lint(args):
    with Repo.open() as repo:
        rep = lintmod.lint(repo)
        if _emit(rep, args.json):
            return
        if not rep["findings"]:
            print("lint: clean")
        else:
            print(f"lint: {len(rep['findings'])} finding(s)")
            for f in rep["findings"]:
                print(f"  [{f['check']}] {f['message']}")
        if rep["queued"]:
            print(f"  (+{rep['queued']} research-queue item(s) from question-shaped findings)")


def cmd_health(args):
    with Repo.open() as repo:
        b = healthmod.compute(repo)
        if _emit(b, args.json):
            return
        print(f"health score: {b['score']} (lower is better)")
        for k, v in b.items():
            if k != "score":
                print(f"  {k}: {v}")


# --- gather -----------------------------------------------------------------
def cmd_bookmarks(args):
    with Repo.open() as repo:
        if args.bcmd == "sync":
            res = gather.bookmarks_sync(repo)
            print(f"bookmarks: +{len(res['added'])} added, "
                  f"{len(res['failed'])} failed, {res['skipped']} skipped")


def cmd_fetch(args):
    with Repo.open() as repo:
        try:
            sid = gather.fetch_for(repo, args.url, args.for_qid)
        except (gather.BudgetError, fetchmod.FetchError, ingest.IngestError) as e:
            sys.exit(f"error: {e}")
        print(f"fetched source #{sid} (autoresearch) for q#{args.for_qid}")


def cmd_websearch(args):
    with Repo.open() as repo:
        try:
            res = gather.websearch(repo, args.query, qid=args.for_qid)
        except gather.BudgetError as e:
            sys.exit(f"error: {e}")
        except Exception as e:  # network errors
            sys.exit(f"error: websearch failed: {e}")
        if _emit(res, args.json):
            return
        for r in res:
            print(f"  {r['title']}\n    {r['url']}")


def cmd_gather_prep(args):
    with Repo.open() as repo:
        bm = gather.bookmarks_sync(repo)
        pend = repo.q("SELECT * FROM sources WHERE status='new' ORDER BY id")
        qn = queuemod.next_open(repo)
        bud = gather.budget_status(repo)
        out = {
            "bookmarks_added": len(bm["added"]),
            "bookmarks_failed": len(bm["failed"]),
            "pending_sources": [dict(r) for r in pend],
            "next_question": dict(qn) if qn else None,
            "budgets": bud,
        }
        if _emit(out, args.json):
            return
        print(f"bookmarks: +{len(bm['added'])} ({len(bm['failed'])} failed)")
        print(f"pending sources: {len(pend)}")
        if qn:
            print(f"next question: q#{qn['id']} {qn['question']}")
        print(f"budget today: {bud['queries_today']} queries, {bud['fetches_today']} fetches")


# --- gate -------------------------------------------------------------------
def cmd_triage(args):
    with Repo.open() as repo:
        if args.tcmd == "summary" or args.tcmd is None:
            s = triagemod.summary(repo)
            if _emit(s, args.json):
                return
            total = sum(s.values())
            print(f"pending claims: {total} "
                  f"(promote {s['promote']}, reject {s['reject']}, "
                  f"hold {s['hold']}, untriaged {s['untriaged']})")
            print("act on recommendations with `brainconnect promote/reject <ids>`; "
                  "generate them with `brainconnect-librarian triage`.")
            return
        rows = triagemod.listing(repo, recommendation=args.recommendation)
        if _emit(rows, args.json):
            return
        if not rows:
            print("no pending claims" + (f" recommended '{args.recommendation}'"
                                         if args.recommendation else ""))
            return
        for r in rows:
            rec = r["recommendation"] or "untriaged"
            tc = f" conf {r['triage_confidence']:.2f}" if r["triage_confidence"] is not None else ""
            print(f"  [{rec}{tc}] claim #{r['id']} (src: {r['source_title'] or r['source_id']})")
            print(f"    {r['text']}")
            if r["reason"]:
                print(f"    ↳ {r['reason']}")


def cmd_gate(args):
    with Repo.open() as repo:
        rep = gatemod.gate(repo)
        if _emit(rep, args.json):
            return
        print(f"gate: auto-promoted {len(rep['promoted'])}, held {len(rep['held'])}")
        for h in rep["held"]:
            print(f"  held #{h['id']}: {'; '.join(h['reasons'])}")


# --- review levers ----------------------------------------------------------
def _split_refs(ids: list[str], action: str) -> tuple[list[int], list[int]]:
    """Sort `brainconnect promote/reject` arguments into (claim ids, candidate ids).

    A bare integer is a claim — the pre-ledger morning-gate path, unchanged. A
    `candidate_N` ref takes the ledger path, which requires a scope and confidence.
    Mixing them in one invocation is refused: they are different operations with
    different arguments, and silently applying only one would be worse.
    """
    claims, cands = [], []
    for raw in ids:
        kind = refs.kind_of(raw)
        if kind == refs.CANDIDATE:
            cands.append(refs.parse(raw, refs.CANDIDATE))
        elif kind is None and raw.isdigit():
            claims.append(int(raw))
        else:
            sys.exit(f"error: cannot {action} {raw!r}: expected a claim id (e.g. 12) "
                     f"or a candidate ref (e.g. candidate_12)")
    if claims and cands:
        sys.exit(f"error: {action} claims and candidates separately — they take "
                 "different arguments")
    return claims, cands


def cmd_promote(args):
    claims, cands = _split_refs(args.ids, "promote")
    with Repo.open() as repo:
        if cands:
            if not args.scope or not args.confidence:
                sys.exit("error: promoting a candidate requires --scope and "
                         "--confidence (low|medium|high|verified)")
            try:
                scope = scopesmod.parse(args.scope)
                for cid in cands:
                    claim = apimod.promote(
                        repo, refs.candidate(cid), reviewer=args.reviewer,
                        confidence=args.confidence, scope=scope, note=args.note,
                        safety_override=args.safety_override,
                        override_reason=args.override_reason)
                    line = (f"{refs.candidate(cid)} -> {claim['id']} "
                            f"({scope}, {args.confidence}) by {args.reviewer}")
                    if args.safety_override:
                        line += "  [SAFETY OVERRIDE recorded]"
                    print(line)
            except _LEDGER_ERRORS as e:
                sys.exit(f"error: {e}")
            return
        review.promote(repo, claims)
        print(f"promoted {len(claims)} claim(s)")


def cmd_reject(args):
    claims, cands = _split_refs(args.ids, "reject")
    with Repo.open() as repo:
        if cands:
            if not args.reason:
                sys.exit("error: rejecting a candidate requires --reason")
            try:
                for cid in cands:
                    apimod.reject(repo, refs.candidate(cid), reviewer=args.reviewer,
                                  reason=args.reason)
                    print(f"rejected {refs.candidate(cid)}: {args.reason}")
            except _LEDGER_ERRORS as e:
                sys.exit(f"error: {e}")
            return
        review.reject(repo, claims)
        print(f"rejected {len(claims)} claim(s)")


def cmd_supersede(args):
    with Repo.open() as repo:
        review.supersede(repo, args.old, args.by, reason=args.reason or "",
                         reviewer=args.reviewer)
        print(f"#{args.old} superseded by #{args.by}")


# --- ledger commands (LEDGER_SPEC.md §12) -----------------------------------
def cmd_recall(args):
    with Repo.open() as repo:
        try:
            pack = apimod.recall(repo, {
                "query": args.query,
                "profile": args.profile,
                "scopes": args.scope or [],
                "trusted_only": not args.untrusted,
                "include_pending": args.include_pending,
                "include_superseded": args.include_superseded,
                "max_items": args.limit,
            })
        except _LEDGER_ERRORS as e:
            sys.exit(f"error: {e}")
    if _emit(pack.as_dict(), args.json):
        return
    print(f"[{pack.profile}] {len(pack.items)} item(s) via {pack.backend}"
          f" ({pack.retrieval_mode})")
    for it in pack.items:
        scope = scopesmod.from_dict(it.scope)
        flag = "" if it.trusted else "  ⚠ UNTRUSTED"
        print(f"  {it.id} [{it.status}/{it.confidence}] ({scope}){flag}")
        print(f"    {it.text}")
        for s in it.sources:
            print(f"    ← {s['id']} ({s['evidence_type']}, {s['origin']})")
    for w in pack.warnings:
        print(f"  ! {w}")


def cmd_export(args):
    """Export the ledger as a portable OKF bundle (read-only; no ledger mutation)."""
    from .okf import OKFAdapter, ExportRequest, ExportError
    try:
        scope_list = [scopesmod.parse(s) for s in (args.scope or [])]
    except _LEDGER_ERRORS as e:
        sys.exit(f"error: {e}")
    # Read-only: the exporter never calls repo.finalize(), so no dump/log churn.
    with Repo.open() as repo:
        try:
            result = OKFAdapter().export_bundle(repo, ExportRequest(
                output_dir=args.output, scopes=scope_list,
                trusted_only=args.trusted_only,
                include_superseded=args.include_superseded))
        except (ExportError,) + _LEDGER_ERRORS as e:
            sys.exit(f"error: {e}")
    if _emit(result.as_dict(), args.json):
        return
    print(f"exported {result.claim_count} claim(s) and {result.source_count} "
          f"source(s) to {result.output_dir} (OKF {result.okf_version})")
    if result.redacted:
        print(f"  {len(result.redacted)} claim(s) masked by safety policy")
    for w in result.withheld:
        print(f"  ! withheld {w['id']}: {w['reason']}")
    for w in result.warnings:
        print(f"  ! {w}")


def cmd_import(args):
    """Import an OKF bundle as PENDING memory candidates (never auto-promoted).

    Flow: structural validation (an invalid bundle imports NOTHING) -> provenance
    registration -> the memory_candidate safety scan -> pending candidate creation.
    Human promotion is a separate, unchanged surface. An external id never
    overwrites a canonical claim; a conflict is reported for operator action.
    """
    from .okf import OKFAdapter, ImportRequest
    try:
        scope = scopesmod.parse(args.scope)
    except _LEDGER_ERRORS as e:
        sys.exit(f"error: {e}")
    by = args.by or _whoami()
    with Repo.open() as repo:
        try:
            result = OKFAdapter().import_bundle(repo, ImportRequest(
                bundle_dir=args.dir, scope=scope, imported_by=by,
                imported_by_type=args.by_type,
                tags=args.tag or [], dry_run=args.dry_run))
        except _LEDGER_ERRORS as e:
            sys.exit(f"error: {e}")
    if _emit(result.as_dict(), args.json):
        sys.exit(0 if result.valid else 1)
    if not result.valid:
        print(f"OKF bundle is structurally INVALID: {len(result.validation_errors)} "
              "error(s). Nothing was imported (no partial import).")
        for e in result.validation_errors:
            loc = f" [{e['path']}]" if e.get("path") else ""
            print(f"  ! {e['code']}: {e['message']}{loc}")
        sys.exit(1)
    verb = "would import" if result.dry_run else "imported"
    print(f"{verb} from {result.bundle_dir} (OKF {result.okf_version}) "
          f"into {result.scope} by {result.imported_by} ({result.imported_by_type})")
    print(f"  created: {len(result.created)}  updated: {len(result.updated)}  "
          f"duplicate: {len(result.duplicates)}  conflict: {len(result.conflicts)}  "
          f"rejected: {len(result.rejected)}")
    if result.quarantined:
        print(f"  {len(result.quarantined)} candidate(s) QUARANTINED — a human must "
              "override to promote")
    if result.redacted:
        print(f"  {len(result.redacted)} candidate(s) had content MASKED by safety policy")
    for d in result.documents:
        if d.outcome == "conflict":
            print(f"  ! CONFLICT {d.document_path}: {d.detail}")
    print("Imported documents are PENDING candidates. None is trusted or promoted; "
          "promotion is a separate, human-only step.")
    sys.exit(0)


def cmd_okf(args):
    """Structurally validate or summarize an OKF bundle. Reads files only.

    STRUCTURAL ONLY: a clean result means the bundle is well-formed, NOT that its
    claims are trusted, promoted, or safe. Exits non-zero on an invalid bundle.
    """
    from .okf import OKFAdapter, ValidationLimits
    limits = ValidationLimits()
    if getattr(args, "max_file_bytes", None):
        limits.max_file_bytes = args.max_file_bytes
    if getattr(args, "max_bundle_bytes", None):
        limits.max_bundle_bytes = args.max_bundle_bytes
    result = OKFAdapter().validate_bundle(args.dir, limits)

    if args.ocmd == "inspect":
        if _emit(result.as_dict(), args.json):
            sys.exit(0 if result.ok else 1)
        state = "VALID" if result.ok else "INVALID"
        print(f"OKF bundle: {state} (OKF {result.okf_version or '?'})")
        print(f"  documents: {result.document_count}  claims: {result.claim_count}"
              f"  sources: {result.source_count}")
        if result.ids:
            shown = ", ".join(result.ids[:12])
            more = "" if len(result.ids) <= 12 else f", … (+{len(result.ids) - 12})"
            print(f"  ids: {shown}{more}")
        print(f"  errors: {len(result.errors)}  warnings: {len(result.warnings)}")
        for w in result.warnings:
            loc = f" [{w.path}]" if w.path else ""
            print(f"  ~ {w.code}: {w.message}{loc}")
        for e in result.errors:
            loc = f" [{e.path}]" if e.path else ""
            print(f"  ! {e.code}: {e.message}{loc}")
        sys.exit(0 if result.ok else 1)

    # validate
    if _emit(result.as_dict(), args.json):
        sys.exit(0 if result.ok else 1)
    if result.ok:
        print(f"OKF bundle is structurally VALID (OKF {result.okf_version}). "
              "This does NOT mean its claims are trusted, promoted, or safe.")
    else:
        print(f"OKF bundle is INVALID: {len(result.errors)} error(s).")
    for w in result.warnings:
        loc = f" [{w.path}]" if w.path else ""
        print(f"  ~ {w.code}: {w.message}{loc}")
    for e in result.errors:
        loc = f" [{e.path}]" if e.path else ""
        print(f"  ! {e.code}: {e.message}{loc}")
    sys.exit(0 if result.ok else 1)


def cmd_candidates(args):
    with Repo.open() as repo:
        if args.pcmd == "show":
            try:
                row = candmod.get(repo, refs.parse(args.id, refs.CANDIDATE))
            except _LEDGER_ERRORS as e:
                sys.exit(f"error: {e}")
            if _emit(row, args.json):
                return
            for k in ("ref", "status", "text", "proposed_by", "proposed_by_type",
                      "source_id", "source_ref", "task_id", "tags",
                      "proposed_scopes", "created_at", "reviewed_at", "reviewed_by",
                      "review_reason", "promoted_claim"):
                if row.get(k) not in (None, "", [], {}):
                    print(f"  {k:18s} {row[k]}")
            return
        rows = candmod.listing(repo, status=args.status, limit=args.limit)
        if _emit(rows, args.json):
            return
        if not rows:
            print(f"no {args.status or 'any'} candidates")
            return
        for r in rows:
            scopes_txt = ", ".join(str(scopesmod.from_dict(s))
                                   for s in r["proposed_scopes"]) or "—"
            print(f"  {r['ref']} [{r['status']}] by {r['proposed_by']} "
                  f"({r['proposed_by_type']}) scopes: {scopes_txt}")
            print(f"    {r['text'][:110]}")


def cmd_claims(args):
    with Repo.open() as repo:
        try:
            if args.ccmd == "show":
                detail = review.claim_detail(repo, refs.parse(args.id, refs.CLAIM))
                if _emit(detail, args.json):
                    return
                for k, v in detail.items():
                    if v not in (None, "", [], {}):
                        print(f"  {k:20s} {v}")
                return
            if args.ccmd == "supersede":
                old = refs.parse(args.old, refs.CLAIM)
                new = refs.parse(args.new, refs.CLAIM)
                apimod.supersede(repo, refs.claim(old), refs.claim(new),
                                 reason=args.reason or "", reviewer=args.reviewer)
                print(f"{refs.claim(old)} superseded by {refs.claim(new)}")
        except _LEDGER_ERRORS as e:
            sys.exit(f"error: {e}")


def cmd_feedback(args):
    with Repo.open() as repo:
        try:
            apimod.record_feedback(repo, {
                "feedback": args.feedback, "actor_id": args.actor,
                "actor_type": args.actor_type, "claim_id": args.id,
                "note": args.note, "task_id": args.task_id})
        except _LEDGER_ERRORS as e:
            sys.exit(f"error: {e}")
        print(f"recorded {args.feedback} on {args.id}")


def cmd_project(args):
    with Repo.open() as repo:
        rep = rendermod.render(repo, all_pages=args.all)
        if _emit(rep, args.json):
            return
        print(f"projected {len(rep['rendered'])} page(s) to the Obsidian vault; "
              f"changed={rep['changed']}")


def cmd_ledger_health(args):
    with Repo.open() as repo:
        h = apimod.health(repo)
    if _emit(h, getattr(args, "json", False)):
        return
    print(f"{h['service']}: {h['role']} — schema v{h['schema_version']}")
    print(f"  backend: {h['backend'].get('backend')} "
          f"(ok={h['backend'].get('ok')}, {h['backend'].get('note', '')})")
    for k, v in h["ledger"].items():
        print(f"  {k:22s} {v}")


def cmd_promote_summary(args):
    with Repo.open() as repo:
        review.promote_summary(repo, args.source)
        print(f"summary for source #{args.source} promoted")


def cmd_contradiction(args):
    with Repo.open() as repo:
        if args.ccmd == "list":
            rows = review.contradiction_list(repo, args.status)
            if _emit([dict(r) for r in rows], args.json):
                return
            for r in rows:
                print(f"  c#{r['id']} [{r['status']}] claims #{r['claim_a']} vs #{r['claim_b']}"
                      + (f"  proposal: {r['proposal']}" if r['proposal'] else ""))
        elif args.ccmd == "propose":
            review.contradiction_propose(repo, args.id, args.text)
            print(f"proposal recorded for c#{args.id}")
        elif args.ccmd == "resolve":
            review.contradiction_resolve(repo, args.id, args.text)
            print(f"c#{args.id} resolved")


def cmd_escalation(args):
    with Repo.open() as repo:
        if args.ecmd == "list":
            rows = review.escalation_list(repo, args.status)
            if _emit([dict(r) for r in rows], args.json):
                return
            for r in rows:
                print(f"  e#{r['id']} [{r['status']}] source #{r['source_id']}: {r['reason']}"
                      + (f"  proposal: {r['proposal']}" if r['proposal'] else ""))
        elif args.ecmd == "propose":
            review.escalation_propose(repo, args.id, args.text)
            print(f"proposal recorded for e#{args.id}")
        elif args.ecmd == "close":
            review.escalation_close(repo, args.id)
            print(f"e#{args.id} closed")


# --- skills -----------------------------------------------------------------
def _parse_ids(s: str | None) -> list[int]:
    if not s:
        return []
    return [int(x) for x in s.replace(",", " ").split()]


def cmd_skill(args):
    with Repo.open() as repo:
        try:
            _dispatch_skill(repo, args)
        except skillsmod.SkillError:
            raise
        except ValueError as e:
            sys.exit(f"error: {e}")


def _dispatch_skill(repo, args):
    c = args.skcmd
    if c == "suggest":
        res = skillsmod.suggest(repo, min_claims=args.min_claims)
        if _emit(res, args.json):
            return
        if not res:
            print("no skill candidates (need a denser claim cluster)")
            return
        print(f"{len(res)} candidate(s):")
        for r in res:
            print(f"  {r['slug']:<28} {r['promoted_claims']} promoted claim(s) "
                  f"[{r['kind']}: {r['entity']}]  e.g. "
                  + ", ".join(f"#{i}" for i in r['sample_claim_ids'][:5]))
    elif c == "new":
        name, warns = skillsmod.new(repo, args.name, args.description, _parse_ids(args.claims))
        print(f"created draft skill {name!r}")
        for w in warns:
            print(f"  warning: {w}")
    elif c == "list":
        res = skillsmod.listing(repo, args.status)
        if _emit(res, args.json):
            return
        if not res:
            print("no skills")
            return
        for r in res:
            flags = []
            if r["installed"]:
                flags.append("installed")
            if r["drift"]:
                flags.append("DRIFT")
            tail = f"  ({', '.join(flags)})" if flags else ""
            print(f"  {r['name']:<28} [{r['status']}] {r['claims']} claim(s){tail}")
            print(f"      {r['description']}")
    elif c == "get":
        print(skillsmod.get_body(repo, skillsmod._norm_name(args.name)))
    elif c == "set":
        body = args.text
        if body == "-" or body is None:
            body = sys.stdin.read()
        skillsmod.set_body(repo, skillsmod._norm_name(args.name), body)
        print(f"body set for {args.name}")
    elif c == "describe":
        skillsmod.describe(repo, skillsmod._norm_name(args.name), args.description)
        print(f"description set for {args.name}")
    elif c == "tools":
        allowed = [t.strip() for t in (args.allowed or "").split(",") if t.strip()] or None
        skillsmod.tools(repo, skillsmod._norm_name(args.name), allowed)
        print(f"allowed-tools set for {args.name}")
    elif c == "attach":
        skillsmod.attach(repo, skillsmod._norm_name(args.name), _parse_ids(args.claims))
        print(f"attached claim(s) to {args.name}")
    elif c == "detach":
        skillsmod.detach(repo, skillsmod._norm_name(args.name), _parse_ids(args.claims))
        print(f"detached claim(s) from {args.name}")
    elif c == "approve":
        path = skillsmod.approve(repo, skillsmod._norm_name(args.name))
        print(f"approved {args.name}; rendered {path}")
    elif c == "archive":
        skillsmod.archive(repo, skillsmod._norm_name(args.name))
        print(f"archived {args.name}")
    elif c == "lint":
        res = skillsmod.lint(repo, skillsmod._norm_name(args.name) if args.name else None)
        if _emit(res, args.json):
            return
        if not res:
            print("skill lint: clean")
            return
        for r in res:
            print(f"  [{r['severity']}] {r['skill']}: {r['message']}")
    elif c == "check":
        res = skillsmod.check(repo)
        if _emit(res, args.json):
            return
        if not res:
            print("skill check: no drift")
            return
        print(f"{len(res)} skill(s) need review:")
        for r in res:
            print(f"  {r['skill']}: {'; '.join(r['reasons'])}")
    elif c == "audit":
        res = skillsmod.audit(repo)
        if _emit(res, args.json):
            return
        d, rd = res["drift"], res["redundant"]
        if not d and not rd:
            print("skill audit: clean (no drift, no redundancy)")
            return
        if d:
            print(f"drift ({len(d)}):")
            for r in d:
                print(f"  {r['skill']}: {'; '.join(r['reasons'])}")
        if rd:
            print(f"redundant pairs ({len(rd)}):")
            for r in rd:
                print(f"  {r['a']} <-> {r['b']} (claims {r['claim_overlap']}, "
                      f"text {r['text_overlap']}) — consider `brainconnect skill merge`")
    elif c == "merge":
        skillsmod.merge(repo, skillsmod._norm_name(args.old), skillsmod._norm_name(args.into))
        print(f"merged {args.old} into {args.into} (archived {args.old}); "
              f"re-audit {args.into} for drift")
    elif c == "versions":
        res = skillsmod.versions(repo, skillsmod._norm_name(args.name))
        if _emit(res, args.json):
            return
        if not res:
            print("no versions yet (approve the skill to record v1)")
            return
        for r in res:
            mark = " <- current" if r["current"] else ""
            print(f"  v{r['version']}  {r['created_at']}  {r['chars']}ch  "
                  f"[{r['note']}]{mark}")
    elif c == "diff":
        out = skillsmod.diff(repo, skillsmod._norm_name(args.name), args.frm, args.to)
        print(out or "(no differences)")
    elif c == "revert":
        res = skillsmod.revert(repo, skillsmod._norm_name(args.name), args.to)
        print(f"reverted {args.name} to v{res['restored_from']} "
              f"(recorded as v{res['new_version']}); rendered {res['path']}"
              + ("; re-installed globally" if res["reinstalled"] else ""))
    elif c == "render":
        rep = skillsmod.render(repo)
        if _emit(rep, args.json):
            return
        print(f"skill render: {len(rep['written'])} written, {len(rep['removed'])} removed")
        for p in rep["written"]:
            print(f"  + {p}")
        for p in rep["removed"]:
            print(f"  - {p}")
    elif c == "install":
        dst = skillsmod.install(repo, skillsmod._norm_name(args.name))
        print(f"installed {args.name} -> {dst}")
    elif c == "uninstall":
        dst = skillsmod.uninstall(repo, skillsmod._norm_name(args.name))
        print(f"uninstalled {args.name} (removed {dst})")


# --- mcp server (Phase 7) ---------------------------------------------------
def cmd_mcp(args):
    # The --read-only flag forces it on; otherwise honor the [mcp] config default.
    # --review must not inherit that default, or a config with read_only=true would
    # turn a human review session into a mutually-exclusive-flags error.
    review_mode = getattr(args, "review", False)
    read_only = args.read_only or (
        not review_mode and bool(Config.load().mcp_cfg("read_only")))
    contribute_only = getattr(args, "contribute_only", False)
    try:
        mcpmod.check_modes(read_only=read_only, contribute_only=contribute_only,
                           review=review_mode)
    except ValueError as e:
        sys.exit(f"error: {e}")
    if args.mcmd == "serve":
        try:
            mcpmod.serve(read_only=read_only, contribute_only=contribute_only,
                         review=review_mode)
        except mcpmod.McpUnavailable as e:
            sys.exit(f"error: {e}")
    elif args.mcmd == "info":
        with Repo.open() as repo:
            cfg = mcpmod.client_config(repo, read_only=read_only,
                                       contribute_only=contribute_only,
                                       review=review_mode)
        if _emit(cfg, getattr(args, "json", False)):
            return
        print("Add this to your MCP client config (e.g. Claude Desktop's "
              "claude_desktop_config.json):\n")
        print(json.dumps(cfg, indent=2))
        if contribute_only:
            print("\nThe server exposes ONLY brain_capture — write-only, no recall "
                  "(the face for an agent fleet that may contribute but must not read back).")
        else:
            print("\nThe server exposes read-only retrieval tools "
                  "(brain_search/hybrid/graph/recall)"
                  + ("." if read_only else " plus brain_capture (gated write)."))


def cmd_backup(args):
    """WAL-safe snapshot of the ledger DB to a single file (online backup API)."""
    with Repo.open() as repo:
        info = backupmod.backup(repo, args.out)
    if _emit(info, args.json):
        return
    print(f"backup: wrote {info['backup']} ({info['bytes']} bytes, "
          f"integrity={info['integrity']}, schema v{info['schema_version']})")
    print("  contents: " + ", ".join(f"{k}={v}" for k, v in info["counts"].items()))


def cmd_restore(args):
    """Replace the live ledger DB with a verified backup. Stop `serve` first."""
    target = backupmod.resolve_db_path()
    pre = args.pre_restore_out
    if pre is None and not args.no_pre_restore:
        pre = str(Path(str(target) + ".pre-restore"))
    try:
        info = backupmod.restore(args.source, target, make_pre_restore=pre)
    except backupmod.BackupError as e:
        sys.exit(f"error: {e}")
    if _emit(info, args.json):
        return
    print(f"restore: replaced {info['restored']} from {info['from']} "
          f"(integrity={info['integrity']}, schema v{info['schema_version']}, "
          f"counts_match={info['counts_match']})")
    if info.get("pre_restore_backup"):
        print(f"  prior state snapshotted to {info['pre_restore_backup']} "
              "(restore it to roll forward)")


def cmd_serve(args):
    token = (args.token or os.environ.get(servermod.TOKEN_ENV_VAR, "")).strip() or None
    try:
        servermod.serve(args.host, args.port, token=token)
    except OSError as e:
        sys.exit(f"error: could not bind {args.host}:{args.port} ({e})")


# --- parser -----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brainconnect", description="BrainConnect CLI (no model calls)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def addj(sp):
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("init").set_defaults(func=cmd_init)

    sa = sub.add_parser("add"); sa.add_argument("target")
    sa.add_argument("--origin", default="clip"); sa.add_argument("--title")
    sa.set_defaults(func=cmd_add)

    # `brainconnect pending` (bare) keeps its pre-ledger meaning: sources awaiting
    # extraction. `pending list` / `pending show` are the ledger's candidate
    # review queue (LEDGER_SPEC.md §12); `pending sources` names the old behaviour
    # explicitly.
    sp = sub.add_parser("pending", help="review queues: candidates, or sources "
                                        "awaiting extraction")
    addj(sp)
    psub = sp.add_subparsers(dest="pcmd")
    pls = psub.add_parser("list", help="memory candidates awaiting promotion")
    pls.add_argument("--status", default="pending",
                     choices=candmod.STATUSES + ("",))
    pls.add_argument("--limit", type=int, default=50)
    addj(pls); pls.set_defaults(func=cmd_candidates)
    psh = psub.add_parser("show", help="one candidate in full")
    psh.add_argument("id", help="candidate ref, e.g. candidate_12")
    addj(psh); psh.set_defaults(func=cmd_candidates)
    psr = psub.add_parser("sources", help="sources awaiting extraction (pre-ledger)")
    addj(psr); psr.set_defaults(func=cmd_pending)
    sp.set_defaults(func=cmd_pending)

    sdr = sub.add_parser("drop"); addj(sdr)
    sdr.add_argument("--no-move", action="store_true",
                     help="don't archive originals to .processed/")
    sdr.set_defaults(func=cmd_drop)

    stt = sub.add_parser("transcribe")
    stt.add_argument("target", help="YouTube URL or local audio/video file")
    stt.set_defaults(func=cmd_transcribe)

    sf = sub.add_parser("file-claims")
    sf.add_argument("--source", type=int, required=True)
    sf.add_argument("--json", dest="json_file", required=True, metavar="FILE")
    sf.add_argument("--refile", action="store_true",
                    help="replace a prior extraction (clears non-promoted claims first)")
    sf.set_defaults(func=cmd_file_claims)

    sev = sub.add_parser("evidence", help="file and index raw primary evidence")
    evsub = sev.add_subparsers(dest="ecmd", required=True)
    evf = evsub.add_parser("file", help="move source artifacts into raw/<bucket>/<year>")
    evt = evf.add_mutually_exclusive_group(required=True)
    evt.add_argument("--source", type=int, help="file one source id")
    evt.add_argument("--all", action="store_true", help="file all processed sources")
    evf.add_argument("--include-new", action="store_true",
                     help="include sources still awaiting extraction")
    addj(evf)
    evi = evsub.add_parser("index", help="write raw/INDEX.md from the sources table")
    addj(evi)
    sev.set_defaults(func=cmd_evidence)

    ss = sub.add_parser("search"); ss.add_argument("terms", nargs="+")
    ss.add_argument("--promoted-only", action="store_true")
    ss.add_argument("--semantic", action="store_true", help="local-embedding search")
    ss.add_argument("--hybrid", action="store_true", help="merge keyword + semantic (RRF)")
    ss.add_argument("--limit", type=int, default=20, help="max results per kind (default 20)")
    addj(ss)
    ss.set_defaults(func=cmd_search)

    sem = sub.add_parser("embed"); sem.add_argument("--all", action="store_true",
                                                    help="re-embed all claims (not just missing)")
    sem.set_defaults(func=cmd_embed)

    sg = sub.add_parser("graph"); sg.add_argument("entity")
    sg.add_argument("--hops", type=int, default=1)
    sg.add_argument("--promoted-only", action="store_true",
                    help="only edges whose evidence claim is promoted")
    addj(sg)
    sg.set_defaults(func=cmd_graph)

    sq = sub.add_parser("queue"); qsub = sq.add_subparsers(dest="qcmd", required=True)
    qa = qsub.add_parser("add"); qa.add_argument("question")
    qa.add_argument("--priority", type=float, default=0.5)
    qa.add_argument("--origin", default="user")
    ql = qsub.add_parser("list"); ql.add_argument("--status"); addj(ql)
    qn = qsub.add_parser("next"); addj(qn)
    qd = qsub.add_parser("done"); qd.add_argument("id", type=int); qd.add_argument("--note")
    qt = qsub.add_parser("attempt"); qt.add_argument("id", type=int)
    sq.set_defaults(func=cmd_queue)

    sc = sub.add_parser("capture", help="propose a memory (files a PENDING candidate)")
    sc.add_argument("--origin", required=True, help="capturing harness, e.g. claude-code")
    sc.add_argument("text_pos", nargs="*", help="the memory text")
    sc.add_argument("--text", help="the memory text (alternative to positional)")
    sc.add_argument("--scope", action="append",
                    help="proposed scope, repeatable, e.g. repo:my-app")
    sc.add_argument("--tags", action="append",
                    help="proposed tag, repeatable (decision, constraint, gotcha, …)")
    sc.add_argument("--source", help="existing evidence source ref (source_7); "
                                     "omit to file the text as its own source")
    sc.add_argument("--source-ref", dest="source_ref",
                    help="opaque external evidence pointer, e.g. an AgentConnect attempt id")
    sc.add_argument("--task-id", dest="task_id", help="opaque task id (not owned here)")
    sc.add_argument("--proposed-by", dest="proposed_by",
                    help="actor proposing (defaults to --origin)")
    sc.add_argument("--proposed-by-type", dest="proposed_by_type", default="agent",
                    choices=candmod.PROPOSER_TYPES)
    addj(sc)
    sc.set_defaults(func=cmd_capture)

    # --- ledger surface (LEDGER_SPEC.md §12) ---
    srec = sub.add_parser("recall", help="trusted, scoped, bounded context pack")
    srec.add_argument("--query", required=True)
    srec.add_argument("--scope", action="append",
                      help="repeatable, e.g. repo:my-app; omit for global facts only")
    srec.add_argument("--profile", choices=profilesmod.NAMES,
                      default=profilesmod.DEFAULT)
    srec.add_argument("--limit", type=int, help="max items (default from profile)")
    srec.add_argument("--include-pending", dest="include_pending",
                      action="store_true",
                      help="also return UNVETTED pending claims (labeled untrusted)")
    srec.add_argument("--include-superseded", dest="include_superseded",
                      action="store_true")
    srec.add_argument("--untrusted", action="store_true",
                      help="also admit contradicted claims")
    addj(srec); srec.set_defaults(func=cmd_recall)

    # export: project the ledger into a portable OKF bundle (read-only)
    sxp = sub.add_parser("export", help="project the ledger into a portable bundle")
    xpsub = sxp.add_subparsers(dest="xcmd", required=True)
    xokf = xpsub.add_parser("okf", help="Open Knowledge Format Markdown bundle")
    xokf.add_argument("--output", required=True,
                      help="output directory (atomically created/replaced)")
    xokf.add_argument("--scope", action="append",
                      help="repeatable scope filter, e.g. repo:my-app; "
                           "omit for a global export of every scope")
    xokf.add_argument("--trusted-only", dest="trusted_only", action="store_true",
                      help="export only trusted claims (promoted, not contradicted)")
    xokf.add_argument("--include-superseded", dest="include_superseded",
                      action="store_true",
                      help="also export superseded claims and a history log")
    addj(xokf)
    sxp.set_defaults(func=cmd_export)

    # import: bring an OKF bundle in as PENDING candidates (never auto-promoted)
    simp = sub.add_parser("import", help="import an external bundle as pending candidates")
    impsub = simp.add_subparsers(dest="icmd", required=True)
    iokf = impsub.add_parser("okf", help="Open Knowledge Format Markdown bundle")
    iokf.add_argument("dir", help="bundle directory to import")
    iokf.add_argument("--scope", required=True,
                      help="scope assigned to every created candidate, e.g. repo:my-app "
                           "(the operator governs blast radius, not the bundle)")
    iokf.add_argument("--by", default=None,
                      help="importing actor (recorded on every candidate; "
                           "defaults to the current user)")
    iokf.add_argument("--by-type", dest="by_type", default="human",
                      choices=candmod.PROPOSER_TYPES,
                      help="actor type (recorded only; no type can bypass promotion)")
    iokf.add_argument("--tag", action="append",
                      help="repeatable tag applied to every created candidate")
    iokf.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="validate and plan only; create nothing")
    addj(iokf)
    simp.set_defaults(func=cmd_import)

    # okf: structurally validate / summarize a bundle (read-only; no ledger)
    sokf = sub.add_parser("okf", help="work with OKF bundles (validate, inspect)")
    okfsub = sokf.add_subparsers(dest="ocmd", required=True)
    for _name, _help in (("validate", "structurally validate an OKF bundle"),
                         ("inspect", "summarize an OKF bundle's structure")):
        _sp = okfsub.add_parser(_name, help=_help)
        _sp.add_argument("dir", help="bundle directory to check")
        _sp.add_argument("--max-file-bytes", dest="max_file_bytes", type=int,
                         help="reject any single file larger than this")
        _sp.add_argument("--max-bundle-bytes", dest="max_bundle_bytes", type=int,
                         help="reject a bundle whose total size exceeds this")
        addj(_sp)
    sokf.set_defaults(func=cmd_okf)

    scl = sub.add_parser("claims", help="inspect and supersede claims")
    clsub = scl.add_subparsers(dest="ccmd", required=True)
    clshow = clsub.add_parser("show", help="provenance, scope, validity, feedback")
    clshow.add_argument("id", help="claim ref, e.g. claim_4 (or a bare id)")
    addj(clshow)
    clsup = clsub.add_parser("supersede", help="retire a claim in favour of another")
    clsup.add_argument("old"); clsup.add_argument("new")
    clsup.add_argument("--reason", help="why the old claim was retired")
    clsup.add_argument("--reviewer", default=_whoami())
    addj(clsup)
    scl.set_defaults(func=cmd_claims)

    sfb = sub.add_parser("feedback", help="report retrieval quality on a claim")
    sfb.add_argument("id", help="claim ref, e.g. claim_4")
    sfb.add_argument("--feedback", required=True, choices=feedbackmod.FEEDBACK_VALUES)
    sfb.add_argument("--note")
    sfb.add_argument("--actor", default=_whoami())
    sfb.add_argument("--actor-type", dest="actor_type", default="human",
                     choices=feedbackmod.ACTOR_TYPES)
    sfb.add_argument("--task-id", dest="task_id")
    sfb.set_defaults(func=cmd_feedback)

    spj = sub.add_parser("project", help="render a projection out of the ledger")
    pjsub = spj.add_subparsers(dest="jcmd", required=True)
    pjo = pjsub.add_parser("obsidian", help="render the Obsidian vault + ledger page")
    pjo.add_argument("--all", action="store_true", help="re-render every page")
    addj(pjo)
    spj.set_defaults(func=cmd_project)

    slh = sub.add_parser("ledger-health",
                         help="the §14 adapter health check (backend + ledger shape)")
    addj(slh); slh.set_defaults(func=cmd_ledger_health)

    sub.add_parser("dump").set_defaults(func=cmd_dump)

    sr = sub.add_parser("render"); sr.add_argument("--all", action="store_true"); addj(sr)
    sr.set_defaults(func=cmd_render)

    sdg = sub.add_parser("digest")
    sdg.add_argument("--day", help="YYYY-MM-DD (default: today)")
    sdg.set_defaults(func=cmd_digest)

    sy = sub.add_parser("synthesis"); ysub = sy.add_subparsers(dest="scmd", required=True)
    yg = ysub.add_parser("get"); yg.add_argument("page")
    yt = ysub.add_parser("set"); yt.add_argument("page")
    yt.add_argument("text", nargs="?", default="-", help="text, or - for stdin")
    sy.set_defaults(func=cmd_synthesis)

    sm = sub.add_parser("commit"); sm.add_argument("message"); sm.set_defaults(func=cmd_commit)

    sl = sub.add_parser("lint"); addj(sl); sl.set_defaults(func=cmd_lint)
    sh = sub.add_parser("health"); addj(sh); sh.set_defaults(func=cmd_health)

    sb = sub.add_parser("bookmarks"); bsub = sb.add_subparsers(dest="bcmd", required=True)
    bsub.add_parser("sync"); sb.set_defaults(func=cmd_bookmarks)

    sfe = sub.add_parser("fetch"); sfe.add_argument("url")
    sfe.add_argument("--for", dest="for_qid", type=int, required=True)
    sfe.set_defaults(func=cmd_fetch)

    sw = sub.add_parser("websearch"); sw.add_argument("query")
    sw.add_argument("--for", dest="for_qid", type=int, default=None); addj(sw)
    sw.set_defaults(func=cmd_websearch)

    sgp = sub.add_parser("gather-prep"); addj(sgp); sgp.set_defaults(func=cmd_gather_prep)

    sgt = sub.add_parser("gate"); addj(sgt); sgt.set_defaults(func=cmd_gate)

    # triage: read the librarian's advisory recommendations over pending claims
    strg = sub.add_parser("triage",
                          help="show librarian promote/reject/hold recommendations "
                               "for pending claims (read-only)")
    tsub = strg.add_subparsers(dest="tcmd")
    tls = tsub.add_parser("list", help="pending claims with their recommendation")
    tls.add_argument("--recommendation", choices=("promote", "reject", "hold"),
                     help="filter to one recommendation")
    addj(tls)
    tsm = tsub.add_parser("summary", help="counts by recommendation")
    addj(tsm)
    addj(strg)
    strg.set_defaults(func=cmd_triage)

    # promote is polymorphic: bare integers are claims (the morning-gate path),
    # `candidate_N` refs take the ledger path and need a scope + confidence.
    spr = sub.add_parser(
        "promote", help="promote claim ids (12 13) or a candidate (candidate_12)")
    spr.add_argument("ids", nargs="+",
                     help="claim ids, or candidate refs like candidate_12")
    spr.add_argument("--scope", help="claim scope, e.g. repo:my-app or global "
                                     "(required for a candidate)")
    spr.add_argument("--confidence", choices=confmod.LABELS,
                     help="confidence label (required for a candidate)")
    spr.add_argument("--reviewer", default=_whoami(),
                     help="who is promoting (defaults to the current user)")
    spr.add_argument("--note", help="note recorded on the promotion")
    spr.add_argument("--safety-override", action="store_true",
                     help="promote a candidate that safety policy blocks "
                          "(requires --override-reason; the findings are kept)")
    spr.add_argument("--override-reason",
                     help="why the safety finding is acceptable; recorded on the "
                          "candidate alongside the original findings")
    spr.set_defaults(func=cmd_promote)
    srj = sub.add_parser(
        "reject", help="reject claim ids (12 13) or a candidate (candidate_12)")
    srj.add_argument("ids", nargs="+",
                     help="claim ids, or candidate refs like candidate_12")
    srj.add_argument("--reason", help="why (required for a candidate)")
    srj.add_argument("--reviewer", default=_whoami())
    srj.set_defaults(func=cmd_reject)

    sup = sub.add_parser("supersede"); sup.add_argument("old", type=int)
    sup.add_argument("--by", type=int, required=True)
    sup.add_argument("--reason", help="why the old claim was retired")
    sup.add_argument("--reviewer", default=_whoami())
    sup.set_defaults(func=cmd_supersede)
    sps = sub.add_parser("promote-summary"); sps.add_argument("source", type=int)
    sps.set_defaults(func=cmd_promote_summary)

    sct = sub.add_parser("contradiction"); csub = sct.add_subparsers(dest="ccmd", required=True)
    cl = csub.add_parser("list"); cl.add_argument("--status", default="open"); addj(cl)
    cp = csub.add_parser("propose"); cp.add_argument("id", type=int); cp.add_argument("text")
    cr = csub.add_parser("resolve"); cr.add_argument("id", type=int); cr.add_argument("text")
    sct.set_defaults(func=cmd_contradiction)

    se = sub.add_parser("escalation"); esub = se.add_subparsers(dest="ecmd", required=True)
    el = esub.add_parser("list"); el.add_argument("--status", default="open"); addj(el)
    ep = esub.add_parser("propose"); ep.add_argument("id", type=int); ep.add_argument("text")
    ec = esub.add_parser("close"); ec.add_argument("id", type=int)
    se.set_defaults(func=cmd_escalation)

    # skills: author Claude skills from promoted claims (Phase 6)
    sk = sub.add_parser("skill", help="author/approve/install skills from promoted claims")
    ksub = sk.add_subparsers(dest="skcmd", required=True)
    ks = ksub.add_parser("suggest", help="surface skill candidates (read-only)")
    ks.add_argument("--min-claims", type=int, default=4); addj(ks)
    kn = ksub.add_parser("new"); kn.add_argument("name")
    kn.add_argument("--description", required=True)
    kn.add_argument("--claims", help="comma/space-separated promoted claim ids")
    kl = ksub.add_parser("list"); kl.add_argument("--status"); addj(kl)
    kg = ksub.add_parser("get"); kg.add_argument("name")
    kt = ksub.add_parser("set"); kt.add_argument("name")
    kt.add_argument("text", nargs="?", default="-", help="body text, or - for stdin")
    kd = ksub.add_parser("describe"); kd.add_argument("name"); kd.add_argument("description")
    ktl = ksub.add_parser("tools"); ktl.add_argument("name")
    ktl.add_argument("allowed", nargs="?", help="comma-separated tool names ('' to clear)")
    ka = ksub.add_parser("attach"); ka.add_argument("name"); ka.add_argument("claims")
    kx = ksub.add_parser("detach"); kx.add_argument("name"); kx.add_argument("claims")
    kap = ksub.add_parser("approve", help="THE GATE: promote a draft + render (human)")
    kap.add_argument("name")
    kar = ksub.add_parser("archive"); kar.add_argument("name")
    kli = ksub.add_parser("lint"); kli.add_argument("name", nargs="?"); addj(kli)
    kc = ksub.add_parser("check", help="drift report for approved skills"); addj(kc)
    kau = ksub.add_parser("audit", help="drift + cross-skill redundancy report"); addj(kau)
    km = ksub.add_parser("merge", help="move <old>'s claims into <into>, archive <old>")
    km.add_argument("old"); km.add_argument("--into", required=True)
    kv = ksub.add_parser("versions", help="version history of a skill")
    kv.add_argument("name"); addj(kv)
    kdf = ksub.add_parser("diff", help="diff skill bodies between versions")
    kdf.add_argument("name")
    kdf.add_argument("--from", dest="frm", type=int, help="version (default: previous)")
    kdf.add_argument("--to", type=int, help="version (default: current live body)")
    krv = ksub.add_parser("revert", help="restore a prior version (rollback)")
    krv.add_argument("name")
    krv.add_argument("--to", type=int, help="version to restore (default: previous)")
    kr = ksub.add_parser("render", help="project approved skills to .claude/skills"); addj(kr)
    ki = ksub.add_parser("install", help="opt-in copy to ~/.claude/skills (human)")
    ki.add_argument("name")
    ku = ksub.add_parser("uninstall"); ku.add_argument("name")
    sk.set_defaults(func=cmd_skill)

    # mcp: expose the brain as an MCP server (Phase 7)
    ssv = sub.add_parser(
        "serve",
        help="serve the memory ledger over HTTP (AgentConnect's adapter routes)")
    ssv.add_argument("--host", default=servermod.DEFAULT_HOST,
                     help=f"bind address (default {servermod.DEFAULT_HOST})")
    ssv.add_argument("--port", type=int, default=servermod.DEFAULT_PORT,
                     help=f"port (default {servermod.DEFAULT_PORT})")
    ssv.add_argument("--token", default="",
                     help="require this bearer token on every route except GET "
                          f"/health (or set {servermod.TOKEN_ENV_VAR})")
    ssv.set_defaults(func=cmd_serve)

    # backup / restore: WAL-safe snapshot & recovery (docs/OPERATIONS.md)
    sbk = sub.add_parser(
        "backup", help="WAL-safe snapshot of the ledger DB to a single file")
    sbk.add_argument("--out", required=True, help="destination .db path")
    addj(sbk)
    sbk.set_defaults(func=cmd_backup)

    srs = sub.add_parser(
        "restore",
        help="replace the live ledger DB with a verified backup (stop serve first)")
    srs.add_argument("--from", dest="source", required=True,
                     help="backup .db to restore from")
    srs.add_argument("--pre-restore-out",
                     help="where to snapshot current state first "
                          "(default: <db>.pre-restore)")
    srs.add_argument("--no-pre-restore", action="store_true",
                     help="skip snapshotting current state before overwriting it")
    addj(srs)
    srs.set_defaults(func=cmd_restore)

    smc = sub.add_parser("mcp", help="serve the brain over MCP (query door)")
    mcsub = smc.add_subparsers(dest="mcmd", required=True)
    mcserve = mcsub.add_parser("serve", help="run the stdio MCP server")
    mcserve.add_argument("--read-only", action="store_true",
                         help="disable the brain_capture write tool")
    mcserve.add_argument("--contribute-only", action="store_true",
                         help="expose ONLY brain_capture (write-only; no recall) — for an agent fleet")
    mcserve.add_argument("--review", action="store_true",
                         help="also expose the HUMAN-GATED brain_pending/promote/reject "
                              "tools — never point an agent at this")
    mcinfo = mcsub.add_parser("info", help="print MCP client config snippet")
    mcinfo.add_argument("--contribute-only", action="store_true",
                        help="emit a config for a write-only (contribute-only) server")
    mcinfo.add_argument("--read-only", action="store_true",
                        help="emit a config for a read-only server")
    mcinfo.add_argument("--review", action="store_true",
                        help="emit a config for a human review server")
    addj(mcinfo)
    smc.set_defaults(func=cmd_mcp)

    return p


def main(argv=None):
    # Windows consoles default to cp1252; our output uses arrows/em-dashes.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
