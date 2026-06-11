# Convenience wrapper so `.\wiki ...` works from the repo root on Windows
# without activating the venv. Prefers the repo venv's console script.
#
# NOTE: we call wiki.exe (the console script), NOT `python -m wiki`. The repo
# root contains the generated `wiki/` Obsidian vault, which shadows the `wiki`
# package for `-m`/`import` when CWD is the repo root. The console script doesn't
# put CWD on sys.path, so it resolves the installed package correctly.
$ErrorActionPreference = "Stop"
$venvWiki = Join-Path $PSScriptRoot ".venv\Scripts\wiki.exe"
if (Test-Path $venvWiki) {
    & $venvWiki @args
} else {
    wiki @args
}
exit $LASTEXITCODE
