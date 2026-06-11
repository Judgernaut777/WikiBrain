#!/usr/bin/env python3
"""SessionEnd hook (pure code, NO model call).

Appends "<timestamp>\t<transcript_path>" to inbox/_transcripts.list so the
morning maintain pass can distill durable findings from session transcripts
(BUILD_SPEC.md §5.3). Reads the hook payload (JSON) from stdin.

Registered in .claude/settings.json under hooks.SessionEnd.
"""
import json
import os
import sys
from datetime import datetime, timezone


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    transcript = payload.get("transcript_path", "")
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not transcript:
        return  # nothing to record
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inbox = os.path.join(cwd, "inbox")
    os.makedirs(inbox, exist_ok=True)
    line = f"{ts}\t{transcript}\n"
    with open(os.path.join(inbox, "_transcripts.list"), "a", encoding="utf-8") as fh:
        fh.write(line)


if __name__ == "__main__":
    main()
