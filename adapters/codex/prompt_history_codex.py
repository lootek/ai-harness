#!/usr/bin/env python3
"""Codex UserPromptSubmit adapter around the ai-harness prompt_history engine.

Spawns $AI_HARNESS_HOME/hooks/prompt_history.py (byte-identical twin of the
claude hook at ~/.claude/hooks/prompt_history.py) and forwards codex's stdin
payload verbatim, appending the submitted prompt to the per-alias history
shared by every CLI:

    ~/tmp/claude/ctx/<alias>/.prompts_history.md

Envelope assumption: codex's hook runtime is Claude-Code-shaped. PreToolUse
was live-verified in step 3 (identical field names); codex 0.157.1 also
parses UserPromptSubmit definitions in hooks.json (its trust store records
`user_prompt_submit` keys). The prompt text is assumed to live in
payload["prompt"] as under Claude Code — flagged for live verification at
cutover, after the one-time manual hook trust ("Trust all and continue").

Adaptations (same two as log_commands_codex.py):
  - os.chdir(payload["cwd"]) first: the engine resolves the session dir from
    the hook process's PWD, so the payload's cwd is made authoritative.
  - Best-effort: any fault exits 0 silently — prompt history must never
    break a codex turn.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HARNESS_HOME = Path(__import__("os").environ.get("AI_HARNESS_HOME")
                     or Path.home() / ".ai-harness")
_ENGINE = _HARNESS_HOME / "hooks" / "prompt_history.py"
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
