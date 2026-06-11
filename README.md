# wiki-brain

![CI](https://github.com/Judgernaut777/WikiBrain/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Model calls in CLI: zero](https://img.shields.io/badge/CLI%20model%20calls-zero-success.svg)

A personal, compounding knowledge base with one direction of flow:

```
raw sources → SQLite DB (claims + context graph = truth) → Obsidian wiki (generated view)
```

**Key-free by default.** The CLI does pure-code structure (ingest, render, search,
budgeted fetch) with **zero billable LLM calls and no API keys** — judgment lives
in your Claude Code sessions, which read content and emit structured claims. An
optional *premium research tier* (Firecrawl / mcp-omnisearch) can be layered in at
the session level, with its keys held outside this repo, so the project itself
stays secret-free (`wiki lint` enforces it).

```
            ┌──────────────────── ingest (one door) ────────────────────┐
 bookmarks  │  wiki add · wiki capture · wiki drop · wiki transcribe     │
 web pages ─┼─▶  raw/  ──▶  SQLite DB (claims + entities + context) ◀── truth
 PDFs/imgs  │        ▲                    │                              │
 captures   └────────┼────────────────────┼──────────────────────────────┘
                     │ (model judgment)    │ wiki render (pure code)
            Claude sessions:               ▼
            night = gather (Haiku)   wiki/  (generated Obsidian vault — never hand-edit)
            morning = maintain (Sonnet)
```

Knowledge is compiled once at ingest and maintained, never re-derived per query.
The wiki is always a regenerable projection of the database — humans and models
change the DB, and the renderer rebuilds the pages. See **BUILD_SPEC.md** for the
full design and **SCHEMA.md** for conventions.

## Design boundaries
- The `wiki` CLI contains **zero model calls** (a billing + determinism
  boundary). All judgment happens inside Claude Code sessions (see the
  `wiki-maintainer` skill).
- **Subscription only.** No API keys anywhere; `claude -p` / headless children
  are denied in `.claude/settings.json`.
- The live DB lives at an absolute path outside the tree (default
  `~/.wiki-brain/wiki.db`) so scheduled-task worktrees share one truth.
- **Your knowledge stays local.** This is a code/design repo: `raw/`, `inbox/`,
  `wiki/`, `db/dump.sql`, `config.toml`, and `log.md` are git-ignored, so your
  actual notes never publish. Un-ignore them locally if you want a private repo
  that versions your knowledge too.

## Setup
```powershell
# from the repo root
Copy-Item config.example.toml config.toml           # then edit paths.db etc.
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e .\cli    # installs the CLI + trafilatura
.venv\Scripts\wiki.exe init                          # create DB + scaffold dirs
```
`config.toml` is git-ignored (it holds your machine-specific paths); the tracked
`config.example.toml` is the template. The live DB lives at an absolute path
**outside** the repo so scheduled-task worktrees share one source of truth.

Optional extras (each guarded — the core CLI runs without them):
`[search]` (robust DuckDuckGo via `ddgs`) · `[docs]` (Docling + Tesseract OCR for
PDFs/images) · `[media]` (YouTube transcripts) · `[semantic]` (local-embedding
search). E.g. `pip install -e ".\cli[search,docs]"`.
Run the CLI any of these ways:
- `.venv\Scripts\wiki.exe <cmd>` (the installed console script), or
- `.\wiki <cmd>` from the repo root (wrapper → repo venv), or
- add `.venv\Scripts` to PATH, or `pipx install .\cli` into a PATH'd Python so
  scheduled tasks can call a bare `wiki`.

## Quick tour
```powershell
.\wiki add https://example.com/article --origin clip   # one door in
.\wiki pending                                          # what needs extraction
# (a model produces extraction JSON per the contract in the skill)
.\wiki file-claims --source 1 --json extract.json
.\wiki gate                                             # auto-promote the boring tier
.\wiki render                                           # rebuild dirty pages
.\wiki lint ; .\wiki health                             # self-check
.\wiki commit "manual ingest"
```
Open the `wiki/` folder as an Obsidian vault to browse (graph view works via
`[[wikilinks]]`).

## Tests
```powershell
.venv\Scripts\python.exe tests\acceptance.py
```
Offline harness covering phases 1–5 against a throwaway temp DB (never touches
the live DB). Network paths (URL fetch, websearch, live bookmark fetch) are
exercised separately.

## Scheduled tasks (Claude Code Desktop)
Desktop scheduled tasks are created via the **Routines** UI (there is no
config-file way to create them). Create two, both with working folder = this
repo and **Isolated worktree** ON:

| Task | ~Time | Model | Instructions |
|---|---|---|---|
| `night-gather` | 02:00 daily | Haiku | `Follow .claude/skills/wiki-maintainer/gather.md exactly.` |
| `morning-maintain` | 06:30 daily | Sonnet | `Follow .claude/skills/wiki-maintainer/maintain.md exactly.` |

Worktree note: generated `wiki/` + `db/dump.sql` commit from the worktree branch;
fast-forward to `main` on success (or do it during your morning review). The live
DB is shared via its absolute path, so both tasks see the same truth.

Interactive fallback (identical procedure): run the morning pass yourself by
telling Claude Code in this repo to *follow maintain.md*.

## Billing guardrails
No API keys in repo/env/config. Leave overflow billing OFF in account settings.
After running the tasks for a few days, check the account usage page — expect
zero Agent SDK credit consumed. If credit IS drawn, disable the tasks and fall
back to the interactive maintain pass. (BUILD_SPEC §10.)
