"""`wiki` command-line entry point. Pure code — zero model calls (§1)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .config import Config
from .db import Repo, init_db
from . import (ingest, search as searchmod, queue as queuemod, render as rendermod,
               lint as lintmod, health as healthmod, gather, gate as gatemod,
               review, fetch as fetchmod, drop as dropmod, extract as extractmod,
               embed as embedmod, skills as skillsmod)

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


# --- init -------------------------------------------------------------------
def cmd_init(args):
    cfg = Config.load()
    root = cfg.root
    for d in SCAFFOLD_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    log = root / "log.md"
    if not log.exists():
        log.write_text("# Operations log\n\n", encoding="utf-8")
    tlist = root / "inbox" / "_transcripts.list"
    if not tlist.exists():
        tlist.write_text("", encoding="utf-8")

    db_path = cfg.db_path
    if db_path.exists():
        print(f"DB already exists at {db_path} (leaving as-is)")
        repo = Repo.open()
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
            res = ingest.file_claims(repo, args.source, args.json_file)
        except ingest.IngestError as e:
            sys.exit(f"error: {e}")
        print(f"filed: {res['claims']} claims, summary={res['summary']}, "
              f"{res['contradictions']} contradiction(s), {res['questions']} question(s)"
              + (", escalated" if res['escalated'] else ""))


def cmd_capture(args):
    with Repo.open() as repo:
        try:
            sid = ingest.capture(repo, args.origin, args.text)
        except ingest.IngestError as e:
            sys.exit(f"error: {e}")
        print(f"captured as source #{sid} (origin session/{args.origin})")


def cmd_drop(args):
    with Repo.open() as repo:
        results = dropmod.scan(repo, move=not args.no_move)
    if _emit(results, args.json):
        return
    if not results:
        print("drop folder is empty (or not configured)")
        return
    ingested = [r for r in results if r["source_id"]]
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


def cmd_dump(args):
    with Repo.open() as repo:
        repo.dump()
        print("db/dump.sql refreshed")


# --- search / graph ---------------------------------------------------------
def cmd_search(args):
    with Repo.open() as repo:
        terms = " ".join(args.terms)
        try:
            if args.semantic:
                res = embedmod.semantic_search(repo, terms)
            elif args.hybrid:
                res = embedmod.hybrid_search(repo, terms)
            else:
                res = searchmod.search(repo, terms, promoted_only=args.promoted_only)
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
        res = searchmod.graph(repo, args.entity, hops=args.hops)
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
def cmd_gate(args):
    with Repo.open() as repo:
        rep = gatemod.gate(repo)
        if _emit(rep, args.json):
            return
        print(f"gate: auto-promoted {len(rep['promoted'])}, held {len(rep['held'])}")
        for h in rep["held"]:
            print(f"  held #{h['id']}: {'; '.join(h['reasons'])}")


# --- review levers ----------------------------------------------------------
def cmd_promote(args):
    with Repo.open() as repo:
        review.promote(repo, args.ids)
        print(f"promoted {len(args.ids)} claim(s)")


def cmd_reject(args):
    with Repo.open() as repo:
        review.reject(repo, args.ids)
        print(f"rejected {len(args.ids)} claim(s)")


def cmd_supersede(args):
    with Repo.open() as repo:
        review.supersede(repo, args.old, args.by)
        print(f"#{args.old} superseded by #{args.by}")


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
                print(f"  e#{r['id']} [{r['status']}] source #{r['source_id']}: {r['reason']}")
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
        name = skillsmod.new(repo, args.name, args.description, _parse_ids(args.claims))
        print(f"created draft skill {name!r}")
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


# --- parser -----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wiki", description="wiki-brain CLI (no model calls)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def addj(sp):
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("init").set_defaults(func=cmd_init)

    sa = sub.add_parser("add"); sa.add_argument("target")
    sa.add_argument("--origin", default="clip"); sa.add_argument("--title")
    sa.set_defaults(func=cmd_add)

    sp = sub.add_parser("pending"); addj(sp); sp.set_defaults(func=cmd_pending)

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
    sf.set_defaults(func=cmd_file_claims)

    ss = sub.add_parser("search"); ss.add_argument("terms", nargs="+")
    ss.add_argument("--promoted-only", action="store_true")
    ss.add_argument("--semantic", action="store_true", help="local-embedding search")
    ss.add_argument("--hybrid", action="store_true", help="merge keyword + semantic (RRF)")
    addj(ss)
    ss.set_defaults(func=cmd_search)

    sem = sub.add_parser("embed"); sem.add_argument("--all", action="store_true",
                                                    help="re-embed all claims (not just missing)")
    sem.set_defaults(func=cmd_embed)

    sg = sub.add_parser("graph"); sg.add_argument("entity")
    sg.add_argument("--hops", type=int, default=1); addj(sg)
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

    sc = sub.add_parser("capture")
    sc.add_argument("--origin", required=True); sc.add_argument("text")
    sc.set_defaults(func=cmd_capture)

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

    spr = sub.add_parser("promote"); spr.add_argument("ids", type=int, nargs="+")
    spr.set_defaults(func=cmd_promote)
    srj = sub.add_parser("reject"); srj.add_argument("ids", type=int, nargs="+")
    srj.set_defaults(func=cmd_reject)
    sup = sub.add_parser("supersede"); sup.add_argument("old", type=int)
    sup.add_argument("--by", type=int, required=True); sup.set_defaults(func=cmd_supersede)
    sps = sub.add_parser("promote-summary"); sps.add_argument("source", type=int)
    sps.set_defaults(func=cmd_promote_summary)

    sct = sub.add_parser("contradiction"); csub = sct.add_subparsers(dest="ccmd", required=True)
    cl = csub.add_parser("list"); cl.add_argument("--status", default="open"); addj(cl)
    cp = csub.add_parser("propose"); cp.add_argument("id", type=int); cp.add_argument("text")
    cr = csub.add_parser("resolve"); cr.add_argument("id", type=int); cr.add_argument("text")
    sct.set_defaults(func=cmd_contradiction)

    se = sub.add_parser("escalation"); esub = se.add_subparsers(dest="ecmd", required=True)
    el = esub.add_parser("list"); el.add_argument("--status", default="open"); addj(el)
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
    kr = ksub.add_parser("render", help="project approved skills to .claude/skills"); addj(kr)
    ki = ksub.add_parser("install", help="opt-in copy to ~/.claude/skills (human)")
    ki.add_argument("name")
    ku = ksub.add_parser("uninstall"); ku.add_argument("name")
    sk.set_defaults(func=cmd_skill)

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
