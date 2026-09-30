#!/usr/bin/env python3
"""Codex PostToolUse adapter around the ai-harness log_commands engine.

Spawns $AI_HARNESS_HOME/hooks/log_commands.py (byte-identical twin of the
claude hook at ~/.claude/hooks/log_commands.py) and forwards codex's stdin
payload to it verbatim, so every CLI appends to the same per-alias history:

    ~/tmp/claude/ctx/<alias>/.commands_history.md

Envelope assumption: codex's hook runtime is Claude-Code-shaped. PreToolUse
was live-verified in step 3 (identical field names: session_id,
transcript_path, cwd, hook_event_name, tool_name, tool_input); codex 0.157.1
also *parses* PostToolUse/UserPromptSubmit definitions in hooks.json (its
trust store records `post_tool_use` keys for them). The exact PostToolUse
payload fields are assumed to match the Claude Code shape (tool_name +
tool_input) — flagged for live verification at cutover, after the one-time
manual hook trust ("Trust all and continue" in the TUI).

Adaptations (the only behavior this adapter adds):
  - os.chdir(payload["cwd"]) before spawning: the engine resolves the
    session dir from the hook process's PWD first (under claude the hook
    inherits the session cwd); codex may spawn hooks elsewhere, so the
    adapter makes the payload's cwd authoritative.
  - Best-effort by design: any fault (bad payload, missing engine, engine
    error, 10s timeout) exits 0 silently. PostToolUse cannot block a tool
    call and history must never break a codex turn — the exact opposite of
    the safe_command adapter's fail-closed contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HARNESS_HOME = Path(__import__("os").environ.get("AI_HARNESS_HOME")
                     or Path.home() / ".ai-harness")
_ENGINE = _HARNESS_HOME / "hooks" / "log_commands.py"
_TIMEOUT_S = 10


def main() -> None:
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        cwd = payload.get("cwd") or ""
        if cwd:
            import os
            os.chdir(cwd)
        subprocess.run(
            [sys.executable, str(_ENGINE)],
            input=raw, timeout=_TIMEOUT_S,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:  # noqa: BLE001 — history logging is best-effort only
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
