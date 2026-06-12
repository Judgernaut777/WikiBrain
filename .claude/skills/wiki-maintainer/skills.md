# skills.md — authoring Claude skills from promoted truth

The brain can promote durable, promoted knowledge into **Claude skills** — a
third one-way projection out of the DB, after the wiki:

```
promoted claims (truth) --[your judgment]--> skill body --[render]--> .claude/skills/<name>/SKILL.md
                                                          --[install]-> ~/.claude/skills/  (opt-in, human)
```

A skill is **instructions a future agent will execute**, so it is held to a
higher bar than a wiki page. Two rules are absolute:

1. **Promoted-only.** Author a skill body **only** from `promoted` claims. Never
   fold raw/pending source text into a skill — that would turn untrusted data
   into instructions. (`wiki skill new --claims` records which promoted claims it
   came from; that link is also the drift basis.)
2. **The gate is human.** `wiki skill approve` is what puts a skill on disk and
   makes it real. Given the blast radius, **only approve interactively** (a human
   running `/maintain`, or the human directly). The unattended night/morning pass
   may *draft and surface* skill candidates — it must not approve them.

## States
`draft` → `approved` → `archived`. Draft skills live in the DB but **never touch
disk**. Only `approved` skills render to `.claude/skills/`. The global
`~/.claude/skills/` copy is a separate, explicit `wiki skill install` (human only,
never a pass).

## Unattended pass (gather/maintain): draft + surface only
1. `wiki skill suggest` — heuristic candidates (dense promoted-claim clusters with
   no skill yet). Judgment is yours; most candidates are not worth a skill.
2. For a candidate that is genuinely a reusable *procedure* (not just facts):
   - `wiki skill new <name> --description "<one-line activation desc>" --claims <ids>`
   - Write the body from those promoted claims and `wiki skill set <name> -` (stdin)
     or `wiki skill set <name> "<body>"`. Keep it a tight, self-contained procedure.
   - Leave it **draft**. List it in the commit "surface for the human" block:
     `wiki skill list --status draft`.
3. `wiki skill check` — report approved skills that have **drifted** (a source
   claim was superseded/rejected since approval). Surface these too; re-author the
   body and leave the re-approval for the human.

## Interactive `/maintain` or human: the gate
- Review drafts: `wiki skill get <name>`, `wiki skill lint <name>`.
- Approve (renders to the repo): `wiki skill approve <name>`. It refuses on empty
  body/description or bad `allowed-tools`. The change lands in the worktree branch
  and reaches the real repo only when you merge.
- Make it available everywhere (optional): `wiki skill install <name>` copies it to
  `~/.claude/skills/` for the account currently running Claude Code. `wiki skill
  uninstall <name>` reverses it. Archiving is refused while installed.

## Refreshing a drifted skill
`wiki skill check` flags it → re-read its promoted claims → rewrite with
`wiki skill set` → `wiki skill approve` again (re-stamps the drift basis). If it
was installed, re-run `wiki skill install` to push the refreshed copy.

## Command cheat-sheet
```
wiki skill suggest [--min-claims N]          # candidates (read-only)
wiki skill new <name> --description "…" [--claims 1,2,3]
wiki skill set|get <name> [body|-]           # author/read the body
wiki skill describe <name> "<desc>" · wiki skill tools <name> "Read,Grep"
wiki skill attach|detach <name> <ids>        # manage provenance claim links
wiki skill list [--status draft|approved|archived] · wiki skill lint [<name>]
wiki skill check                             # drift report (approved skills)
wiki skill approve <name>                    # THE GATE — human; drafts→approved+render
wiki skill render                            # project all approved skills to disk
wiki skill install|uninstall <name>          # opt-in ~/.claude/skills copy (human)
wiki skill archive <name>                    # retire + remove the generated dir
```

Reserved name `wiki-maintainer` (this hand-authored skill) can never be generated
or overwritten. Generated dirs carry a `.generated` marker; the renderer only
deletes dirs it owns.
