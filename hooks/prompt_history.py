#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook: appends the submitted prompt to the
active session's prompts_history.md.

Session dir resolution mirrors log_commands.py:
1. $PWD if under ~/tmp/claude/ctx/<alias>/
2. Match session_id against ~/tmp/claude/ctx/*/.session files
3. Fall back to the most recently modified session dir
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

CTX_ROOT = Path.home() / "tmp" / "claude" / "ctx"


def find_session_dir_by_cwd() -> Path | None:
    cwd = Path(os.getcwd()).resolve()
    ctx_resolved = CTX_ROOT.resolve()
    if cwd == ctx_resolved:
        return None
    try:
        rel = cwd.relative_to(ctx_resolved)
    except ValueError:
        return None
    alias = rel.parts[0]
    candidate = ctx_resolved / alias
    return candidate if candidate.is_dir() else None


def find_session_dir_by_id(session_id: str) -> Path | None:
    if not session_id or not CTX_ROOT.is_dir():
        return None
    for session_file in CTX_ROOT.glob("*/.session"):
        try:
            if session_file.read_text().strip() == session_id:
                return session_file.parent
        except OSError:
            continue
    return None


def find_session_dir_by_recency() -> Path | None:
    if not CTX_ROOT.is_dir():
        return None
    candidates = []
    for sf in CTX_ROOT.glob("*/.session"):
        try:
            candidates.append((sf.stat().st_mtime, sf.parent))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    prompt = payload.get("prompt", "")
    session_id = payload.get("session_id", "")

    session_dir = (
        find_session_dir_by_cwd()
        or find_session_dir_by_id(session_id)
        or find_session_dir_by_recency()
    )
    if session_dir is None:
        sys.exit(0)

    history_file = session_dir / ".prompts_history.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header_needed = not history_file.exists()
    with open(history_file, "a") as f:
        if header_needed:
            f.write(f"# Session: {session_dir.name} — Prompts History\n")
        f.write(f"\n## {timestamp}\n")
        if prompt:
            f.write(f"{prompt}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
