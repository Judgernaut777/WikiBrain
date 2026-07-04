"""`wiki-librarian maintain`: the whole judgment cycle in one command.

Chains the librarian's advisory passes in maintain.md order — catch-up (extract
every pending source), triage, adjudicate, synthesize — then the zero-model
housekeeping tail (render + digest + lint + health), the same pure-code wiki
modules `extract.py` already drives. This is the keystone convenience wrapper so
a human runs ONE command instead of five subcommands by hand.

It stays inside every gate the individual passes honor: it DRAFTS and PROPOSES
only. It NEVER promotes a claim, resolves a contradiction, closes an escalation,
or approves a skill — those remain human gates (wiki-maintainer/maintain.md).
`--commit` is opt-in; git stays the user's call.

A PREFLIGHT runs before any model call: it verifies a model is configured and
the endpoint is reachable, failing fast with an actionable message (naming the
config key / the base_url) instead of a stack trace mid-run. One failing stage
is recorded and the remaining safe stages still run — a single bad claim never
aborts the whole pass.
"""
from __future__ import annotations

import subprocess

from wiki import health as healthmod
from wiki import lint as lintmod
from wiki import render as rendermod

from . import adjudicate as adjudicatemod
from . import client
from . import extract as extractmod
from . import synthesize as synthesizemod
from . import triage as triagemod
from .config import LibrarianConfig

# The optional model stages (catch-up always runs; housekeeping always runs).
STAGES = ("triage", "adjudicate", "synthesize")


class PreflightError(Exception):
    """The librarian is not runnable — no model configured, or the endpoint is
    unreachable. Carries an actionable, human-facing message (no stack trace)."""


def _preflight(cfg: LibrarianConfig) -> dict:
    """Verify the librarian can actually reach a model before spending any call.
    Raises PreflightError (or the config's LibrarianConfigError for a missing key
    env var) with a message that names the offending config key / base_url."""
    if not cfg.get("model") and not (cfg.get("models") or {}):
        raise PreflightError(
            "no model configured — set [librarian] model (or a per-task override "
            "under [librarian.models]) in config.toml before running maintain")
    cfg.api_key()  # raises LibrarianConfigError with a clear message if env unset
    base = cfg.get("base_url")
    if not client.reachable(cfg):
        raise PreflightError(
            f"model endpoint unreachable at base_url {base!r} — start your local "
            "server (Ollama/LM Studio) or fix [librarian] base_url in config.toml")
    return {"base_url": base, "model": cfg.get("model") or None}


def _housekeeping(repo) -> dict:
    """The pure-code tail: ensure today's digest, render, lint, health. No model."""
    digest = rendermod.ensure_digest(repo)
    render_rep = rendermod.render(repo)
    lint_rep = lintmod.lint(repo)
    health_rep = healthmod.compute(repo)
    return {
        "digest": digest,
        "pages_rendered": len(render_rep["rendered"]),
        "lint_findings": len(lint_rep["findings"]),
        "lint_queued": lint_rep.get("queued", 0),
        "health": health_rep,
    }


def _commit(repo) -> bool:
    """Mirror `wiki commit`: stage everything (git-ignored personal content is
    excluded by .gitignore) and commit. Returns True if a commit was created."""
    root = str(repo.root)
    subprocess.run(["git", "-C", root, "add", "-A"], check=False)
    r = subprocess.run(["git", "-C", root, "commit", "-m", "wiki-librarian maintain"],
                       capture_output=True)
    return r.returncode == 0


def _summarize(report: dict) -> dict:
    cu = report.get("catch_up") or {}
    tri = report.get("triage") or {}
    adj = report.get("adjudicate") or {}
    syn = report.get("synthesize") or {}
    hk = report.get("housekeeping") or {}
    recs = {"promote": 0, "reject": 0, "hold": 0}
    for d in tri.get("triaged", []):
        rec = d.get("recommendation")
        if rec in recs:
            recs[rec] += 1
    health = hk.get("health") or {}
    return {
        "sources_extracted": len(cu.get("processed", [])),
        "sources_failed": len(cu.get("failed", [])),
        "gate_promoted": cu.get("gate_promoted", 0),
        "gate_held": cu.get("gate_held", 0),
        "triage_recommendations": recs,
        "triage_failed": len(tri.get("failed", [])),
        "proposals_drafted": len(adj.get("proposed", [])),
        "proposals_failed": len(adj.get("failed", [])),
        "synthesis_pages": len(syn.get("pages", [])),
        "skill_drafts": len(syn.get("skills", [])),
        "health_score": health.get("score"),
    }


def run(repo, cfg: LibrarianConfig, *, stages=STAGES, commit: bool = False) -> dict:
    """Run the full judgment cycle over `repo`, returning an aggregated report.

    `stages` selects which of the optional model passes run (see STAGES); catch-up
    and the housekeeping tail always run. Preflight raises before any model call if
    the librarian is not runnable. One failing stage is recorded under `errors` and
    the rest still run — the run never aborts on a single stage. NEVER promotes,
    resolves, closes, or approves anything: advisory drafts/proposals only.
    """
    stages = set(stages)
    pf = _preflight(cfg)
    report: dict = {
        "preflight": pf,
        "catch_up": None,
        "triage": None,
        "adjudicate": None,
        "synthesize": None,
        "housekeeping": None,
        "stages_run": [],
        "stages_skipped": [s for s in STAGES if s not in stages],
        "errors": [],
        "committed": False,
    }

    def _stage(name, fn):
        try:
            report[name] = fn()
            report["stages_run"].append(name)
        except Exception as e:  # noqa: BLE001 — one bad pass must not abort the rest
            report["errors"].append({"stage": name, "error": str(e)})

    # 1. catch-up — extract every pending source, then gate + render
    _stage("catch_up", lambda: extractmod.catch_up(repo, cfg))
    # 2. triage — recommend on gate-held pending claims (advisory)
    if "triage" in stages:
        _stage("triage", lambda: triagemod.run(repo, cfg))
    # 3. adjudicate — propose contradiction/escalation resolutions (advisory)
    if "adjudicate" in stages:
        _stage("adjudicate", lambda: adjudicatemod.run(repo, cfg))
    # 4. synthesize — draft page prose + skill drafts (drafts only)
    if "synthesize" in stages:
        _stage("synthesize", lambda: synthesizemod.run(repo, cfg))
    # 5. housekeeping — pure code, zero model
    _stage("housekeeping", lambda: _housekeeping(repo))

    report["summary"] = _summarize(report)
    if commit:
        report["committed"] = _commit(repo)
    return report
