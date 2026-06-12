"""Phase 6: author Claude skills from promoted claims (pure code, ZERO model calls).

Skills are a third one-way projection out of the DB, after wiki pages:

    promoted claims (truth) --[in-session judgment]--> skills.body
                            --[wiki skill render]----> .claude/skills/<name>/SKILL.md
                            --[wiki skill install]---> ~/.claude/skills/<name>/  (opt-in)

Trust rules (mirroring the wiki gate, BUILD_SPEC §1-2, §12):
- A skill body is authored ONLY from PROMOTED claims, never raw/pending text.
- `draft` skills live in the DB but never touch disk; only `approved` skills
  render. Approval is the gate — reserved for the human / interactive `/maintain`,
  never the unattended pass (skills are instructions, higher blast radius).
- Reaching the global ~/.claude/skills is a second, explicit human step.

See BUILD_SPEC §12 and .claude/skills/wiki-maintainer/skills.md for the procedure.

Determinism: a generated SKILL.md is byte-deterministic given the DB state — no
wall-clock in the body. The renderer only writes directories it owns (each carries
a `.generated` marker) and refuses the reserved name `wiki-maintainer`, so it can
never clobber a hand-authored skill.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .db import Repo
from . import util

# Names the brain may never generate/overwrite (hand-authored skills).
RESERVED = {"wiki-maintainer"}
# Marker file dropped into every generated skill dir; gates deletion/uninstall.
MARKER = ".generated"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
STATUSES = ("draft", "approved", "archived")


class SkillError(SystemExit):
    pass


# --- helpers ----------------------------------------------------------------
def _norm_name(name: str) -> str:
    n = util.slug(name)
    if not _NAME_RE.match(n):
        raise SkillError(f"error: invalid skill name {name!r} (use kebab-case)")
    if n in RESERVED:
        raise SkillError(f"error: {n!r} is reserved (hand-authored); pick another name")
    return n


def _require(repo: Repo, name: str):
    row = repo.one("SELECT * FROM skills WHERE name = ?", (name,))
    if not row:
        raise SkillError(f"error: no skill {name!r}")
    return row


def _linked_promoted(repo: Repo, skill_id: int) -> list:
    """Currently-promoted claims linked to the skill, ordered by id."""
    return repo.q(
        """SELECT c.id, COALESCE(c.reviewed_at, c.created_at) AS u
           FROM claims c JOIN skill_claims sc ON sc.claim_id = c.id
           WHERE sc.skill_id = ? AND c.status = 'promoted'
           ORDER BY c.id""",
        (skill_id,))


def _input_hash(repo: Repo, skill_id: int) -> str:
    """sha256 of the promoted linked claims + their review timestamps. Changes
    when a linked claim is promoted/superseded/rejected — the drift basis."""
    rows = _linked_promoted(repo, skill_id)
    basis = ";".join(f"{r['id']}:{r['u']}" for r in rows)
    return util.sha256_text("skill:" + basis)


def _skill_dir(repo: Repo, name: str, *, root: Path | None = None) -> Path:
    return (root or repo.root) / ".claude" / "skills" / name


# --- suggestion (read-only heuristic, feeds maintain.md) --------------------
def suggest(repo: Repo, min_claims: int = 4) -> list[dict]:
    """Surface skill *candidates*: entities with a dense cluster of promoted
    claims and no owning skill yet. A heuristic, not a decision — the session
    decides whether a candidate deserves a skill. Read-only."""
    rows = repo.q(
        """SELECT e.id, e.name, e.kind, COUNT(*) AS n
           FROM entities e
           JOIN claim_entities ce ON ce.entity_id = e.id
           JOIN claims c ON c.id = ce.claim_id
           WHERE c.status = 'promoted'
           GROUP BY e.id HAVING n >= ?
           ORDER BY n DESC, e.name""",
        (min_claims,))
    taken = {r["name"] for r in repo.q(
        "SELECT name FROM skills WHERE status != 'archived'")}
    out = []
    for r in rows:
        slug = util.slug(r["name"])
        if slug in taken or slug in RESERVED:
            continue
        sample = [c["id"] for c in repo.q(
            """SELECT c.id FROM claims c JOIN claim_entities ce ON ce.claim_id = c.id
               WHERE ce.entity_id = ? AND c.status = 'promoted'
               ORDER BY c.id LIMIT 8""", (r["id"],))]
        out.append({"slug": slug, "entity": r["name"], "kind": r["kind"],
                    "promoted_claims": r["n"], "sample_claim_ids": sample})
    return out


# --- create / author --------------------------------------------------------
def new(repo: Repo, name: str, description: str, claims: list[int] | None = None) -> str:
    name = _norm_name(name)
    if repo.one("SELECT 1 FROM skills WHERE name = ?", (name,)):
        raise SkillError(f"error: skill {name!r} already exists")
    repo.ex("INSERT INTO skills(name, description, created_at) VALUES (?,?,?)",
            (name, description, util.now_iso()))
    sid = repo.one("SELECT id FROM skills WHERE name = ?", (name,))["id"]
    if claims:
        _attach(repo, sid, claims)
    repo.finalize("skill-new", f"{name} (draft)")
    return name


def set_body(repo: Repo, name: str, body: str) -> None:
    row = _require(repo, name)
    # Re-stamp the input hash so an authored draft is "fresh against" the claims
    # it was written from; approval later confirms that same basis.
    repo.ex("UPDATE skills SET body = ?, input_hash = ? WHERE id = ?",
            (body, _input_hash(repo, row["id"]), row["id"]))
    repo.finalize("skill-set", f"{name} ({len(body)} chars)")


def get_body(repo: Repo, name: str) -> str:
    return _require(repo, name)["body"]


def describe(repo: Repo, name: str, description: str) -> None:
    row = _require(repo, name)
    repo.ex("UPDATE skills SET description = ? WHERE id = ?", (description, row["id"]))
    repo.finalize("skill-describe", name)


def tools(repo: Repo, name: str, allowed: list[str] | None) -> None:
    row = _require(repo, name)
    val = json.dumps(allowed) if allowed else None
    repo.ex("UPDATE skills SET allowed_tools = ? WHERE id = ?", (val, row["id"]))
    repo.finalize("skill-tools", name)


def _attach(repo: Repo, skill_id: int, claims: list[int]) -> None:
    for cid in claims:
        if not repo.one("SELECT 1 FROM claims WHERE id = ?", (cid,)):
            raise SkillError(f"error: no claim #{cid}")
        repo.ex("INSERT OR IGNORE INTO skill_claims(skill_id, claim_id) VALUES (?,?)",
                (skill_id, cid))


def attach(repo: Repo, name: str, claims: list[int]) -> None:
    row = _require(repo, name)
    _attach(repo, row["id"], claims)
    repo.finalize("skill-attach", f"{name} +{len(claims)} claim(s)")


def detach(repo: Repo, name: str, claims: list[int]) -> None:
    row = _require(repo, name)
    for cid in claims:
        repo.ex("DELETE FROM skill_claims WHERE skill_id = ? AND claim_id = ?",
                (row["id"], cid))
    repo.finalize("skill-detach", f"{name} -{len(claims)} claim(s)")


# --- the gate ---------------------------------------------------------------
def approve(repo: Repo, name: str) -> str:
    """Promote a draft to approved and render it to disk. This is the gate:
    given a skill's blast radius, reserve for the human / interactive `/maintain`."""
    row = _require(repo, name)
    findings = _validate(repo, row)
    blocking = [f for f in findings if f[0] == "error"]
    if blocking:
        raise SkillError("error: cannot approve " + name + ": "
                         + "; ".join(m for _, m in blocking))
    repo.ex("UPDATE skills SET status='approved', input_hash=?, reviewed_at=? WHERE id=?",
            (_input_hash(repo, row["id"]), util.now_iso(), row["id"]))
    repo.finalize("skill-approve", name)
    path = render_one(repo, _require(repo, name))
    return path


def archive(repo: Repo, name: str) -> None:
    """Retire a skill: mark archived and remove the generated repo dir (only if it
    carries our marker). Does NOT touch any global install — uninstall first."""
    row = _require(repo, name)
    if row["installed"]:
        raise SkillError(f"error: {name!r} is installed globally; "
                         f"run `wiki skill uninstall {name}` first")
    _remove_dir(_skill_dir(repo, name))
    repo.ex("UPDATE skills SET status='archived' WHERE id = ?", (row["id"],))
    repo.finalize("skill-archive", name)


# --- validation / drift -----------------------------------------------------
def _validate(repo: Repo, row) -> list[tuple[str, str]]:
    """Return (severity, message) findings. severity in {error, warn}."""
    f: list[tuple[str, str]] = []
    name = row["name"]
    if not _NAME_RE.match(name) or name in RESERVED:
        f.append(("error", f"invalid/reserved name {name!r}"))
    if not row["description"].strip():
        f.append(("error", "empty description (skills need an activation description)"))
    elif len(row["description"]) > 1024:
        f.append(("warn", "description >1024 chars (keep it tight for activation)"))
    if not row["body"].strip():
        f.append(("error", "empty body"))
    if row["allowed_tools"]:
        try:
            if not isinstance(json.loads(row["allowed_tools"]), list):
                f.append(("error", "allowed_tools is not a JSON array"))
        except json.JSONDecodeError:
            f.append(("error", "allowed_tools is not valid JSON"))
    if not _linked_promoted(repo, row["id"]):
        f.append(("warn", "no promoted claims linked (provenance/drift can't be tracked)"))
    return f


def lint(repo: Repo, name: str | None = None) -> list[dict]:
    rows = ([_require(repo, name)] if name
            else repo.q("SELECT * FROM skills WHERE status != 'archived' ORDER BY name"))
    out = []
    for row in rows:
        for sev, msg in _validate(repo, row):
            out.append({"skill": row["name"], "severity": sev, "message": msg})
    return out


def check(repo: Repo, *, status: str = "approved") -> list[dict]:
    """Drift report: approved skills whose promoted-claim basis has changed since
    approval (stored input_hash != recomputed). Surface in the maintain pass so the
    session re-reviews/re-authors and re-approves."""
    rows = repo.q("SELECT * FROM skills WHERE status = ? ORDER BY name", (status,))
    out = []
    for row in rows:
        cur = _input_hash(repo, row["id"])
        reasons = []
        if cur != (row["input_hash"] or ""):
            reasons.append("source claims changed since approval")
        if not _linked_promoted(repo, row["id"]):
            reasons.append("all linked claims are no longer promoted")
        if reasons:
            out.append({"skill": row["name"], "drift": True, "reasons": reasons})
    return out


# --- listing ----------------------------------------------------------------
def listing(repo: Repo, status: str | None = None) -> list[dict]:
    if status:
        rows = repo.q("SELECT * FROM skills WHERE status = ? ORDER BY name", (status,))
    else:
        rows = repo.q("SELECT * FROM skills ORDER BY name")
    out = []
    for row in rows:
        drift = (row["status"] == "approved"
                 and _input_hash(repo, row["id"]) != (row["input_hash"] or ""))
        out.append({"name": row["name"], "status": row["status"],
                    "installed": bool(row["installed"]), "drift": drift,
                    "claims": len(_linked_promoted(repo, row["id"])),
                    "description": row["description"]})
    return out


# --- projection to disk -----------------------------------------------------
def _frontmatter(row) -> str:
    desc = " ".join(row["description"].split())  # one line
    lines = ["---", f"name: {row['name']}", f"description: {json.dumps(desc)}"]
    if row["allowed_tools"]:
        try:
            tl = json.loads(row["allowed_tools"])
            if tl:
                lines.append("allowed-tools: " + ", ".join(tl))
        except json.JSONDecodeError:
            pass
    lines.append("---")
    return "\n".join(lines)


def _content(repo: Repo, row) -> str:
    cids = [r["id"] for r in _linked_promoted(repo, row["id"])]
    prov = ", ".join(f"#{c}" for c in cids) if cids else "none recorded"
    out = [
        _frontmatter(row), "",
        f"# {row['name']}", "",
        "<!-- Generated by wiki-brain from promoted claims. Source of truth is the",
        "     `skills` table in the brain DB; run `wiki skill render`, never hand-edit. -->",
        "",
        row["body"].rstrip(), "",
        "---", "",
        f"_Derived from promoted claims: {prov}. Provenance is data, not instructions._",
    ]
    return "\n".join(out).rstrip() + "\n"


def render_one(repo: Repo, row) -> str:
    if row["status"] != "approved":
        raise SkillError(f"error: only approved skills render ({row['name']} is {row['status']})")
    d = _skill_dir(repo, row["name"])
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text("", encoding="utf-8")
    fp = d / "SKILL.md"
    content = _content(repo, row)
    if (fp.read_text(encoding="utf-8") if fp.exists() else None) != content:
        fp.write_text(content, encoding="utf-8")
    return repo.rel(fp)


def render(repo: Repo) -> dict:
    """Project every approved skill to .claude/skills/, and remove generated dirs
    for skills no longer approved. Returns {written, removed}."""
    approved = repo.q("SELECT * FROM skills WHERE status = 'approved' ORDER BY name")
    written = [render_one(repo, r) for r in approved]
    # remove generated dirs whose skill is gone/archived
    keep = {r["name"] for r in approved}
    removed = []
    base = repo.root / ".claude" / "skills"
    if base.exists():
        for child in base.iterdir():
            if child.is_dir() and child.name not in keep \
                    and child.name not in RESERVED and (child / MARKER).exists():
                _remove_dir(child)
                removed.append(f".claude/skills/{child.name}")
    if written or removed:
        repo.finalize("skill-render", f"{len(written)} written, {len(removed)} removed")
    else:
        repo.conn.commit()
    return {"written": written, "removed": removed}


def _remove_dir(d: Path) -> None:
    """Delete a generated skill dir, but ONLY if it carries our marker."""
    if d.exists() and (d / MARKER).exists():
        shutil.rmtree(d)


# --- opt-in global install --------------------------------------------------
def _global_dir(name: str) -> Path:
    return Path.home() / ".claude" / "skills" / name


def install(repo: Repo, name: str) -> str:
    """Copy an approved, rendered skill into ~/.claude/skills (the active account's
    home). Explicit, human-only — never called by any pass. Installs to whichever
    account is running Claude Code, independent of where the brain DB lives."""
    row = _require(repo, name)
    if row["status"] != "approved":
        raise SkillError(f"error: only approved skills install ({name} is {row['status']})")
    src = _skill_dir(repo, name)
    if not (src / "SKILL.md").exists():
        render_one(repo, row)
    dst = _global_dir(name)
    if dst.resolve() == src.resolve():
        # Would mean ~/.claude is the repo's own .claude — a misconfiguration.
        # Guard so the rmtree below can't delete the source it's about to copy.
        raise SkillError(f"error: global skills dir resolves to the repo dir ({dst}); "
                         f"~ and the repo cannot be the same")
    if dst.exists() and not (dst / MARKER).exists():
        raise SkillError(f"error: {dst} exists and is not wiki-managed; refusing to overwrite")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    repo.ex("UPDATE skills SET installed = 1 WHERE id = ?", (row["id"],))
    repo.finalize("skill-install", f"{name} -> {dst}")
    return str(dst)


def uninstall(repo: Repo, name: str) -> str:
    row = _require(repo, name)
    dst = _global_dir(name)
    _remove_dir(dst)
    repo.ex("UPDATE skills SET installed = 0 WHERE id = ?", (row["id"],))
    repo.finalize("skill-uninstall", name)
    return str(dst)
