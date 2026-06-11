@echo off
REM Convenience wrapper so `wiki ...` works from the repo root on Windows.
REM Calls the console script (NOT `python -m wiki`): the repo root holds the
REM generated `wiki\` vault which would shadow the `wiki` package for `-m`.
setlocal
set "VENVWIKI=%~dp0.venv\Scripts\wiki.exe"
if exist "%VENVWIKI%" (
  "%VENVWIKI%" %*
) else (
  wiki %*
)
exit /b %ERRORLEVEL%
