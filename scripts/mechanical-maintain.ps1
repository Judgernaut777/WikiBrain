# mechanical-maintain.ps1 — the zero-model half of the morning maintain pass.
#
# Part of the "hybrid" scheduling approach (BUILD_SPEC §7 + the MCP/scheduling
# note): a plain Windows Task Scheduler job runs the pure-code steps every day —
# NO model calls, no Claude client involved — while the judgment half of
# maintain.md (claim extraction, synthesis, contradiction adjudication, skill
# drafting) is done interactively via `/maintain` when convenient.
#
# Safe to run unattended: every step here is a zero-model `wiki` command. It
# commits locally but NEVER pushes — you review the morning diff and push.
#
# Written to be Windows PowerShell 5.1-compatible (Task Scheduler's default
# host): no &&/||, ternary, or null-coalescing.

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Call the console script (NOT `python -m wiki`): the repo root holds the
# generated wiki\ vault, which would shadow the `wiki` package for -m.
$wiki = Join-Path $repo '.venv\Scripts\wiki.exe'
if (-not (Test-Path $wiki)) { $wiki = 'wiki' }

$logDir = Join-Path $repo 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir 'mechanical-maintain.log'
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') mechanical maintain ====" | Add-Content $log

function Step($desc, [string[]]$cmdArgs) {
    "-- $desc : wiki $($cmdArgs -join ' ')" | Add-Content $log
    & $wiki @cmdArgs *>&1 | Add-Content $log
    "   exit=$LASTEXITCODE" | Add-Content $log
}

Step 'bookmarks sync'                  @('bookmarks', 'sync')
Step 'gate (auto-promote boring tier)' @('gate')
Step 'render'                          @('render')
Step 'lint'                            @('lint')
Step 'health'                          @('health')
# Local commit only — never push. `wiki commit` is a no-op-safe if nothing changed.
Step 'commit'                          @('commit', "cron: mechanical maintain $(Get-Date -Format 'yyyy-MM-dd')")

"==== done ====" | Add-Content $log
