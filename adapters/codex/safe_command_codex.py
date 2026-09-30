#!/usr/bin/env python3
"""Codex PreToolUse adapter around the ai-harness safe_command engine.

codex's hook runtime is Claude-Code-shaped, verified live against codex-cli
0.157.1 (2026-09-29, probe over `codex exec` with a stdin-dumping hook):

  hooks.json (at ~/.codex/hooks.json) — same shape as Claude Code settings:
      {"hooks": {"PreToolUse": [{"matcher": "<tool>",
                                 "hooks": [{"type": "command",
                                            "command": "<abs cmd>"}]}]}}

  stdin payload (identical field names to Claude Code plus codex extras):
      {"session_id", "turn_id", "transcript_path", "cwd",
       "hook_event_name": "PreToolUse", "model", "permission_mode",
       "tool_name": "Bash", "tool_input": {"command": "<cmd>"},
       "tool_use_id"}

  stdout envelope: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
      "permissionDecision": "allow"|"deny", "permissionDecisionReason": ...}}
      — "deny" blocks the call and surfaces the reason to the model.
      — "ask" is NOT honored in codex exec: the hook reports "PreToolUse
        Failed" and the command RUNS anyway (fail-open). So every engine ASK
        is demoted to deny here, with the original reason prefixed, and the
        same fail-closed rule as the engine applies to adapter faults.

Trust: codex requires a one-time per-definition trust ("hooks are new or
changed" → "2. Trust all and continue" in the TUI); until then the hook is
silently skipped and codex runs WITHOUT this gate.

Engine: imported by absolute path from $AI_HARNESS_HOME/hooks/safe_command.py
($AI_HARNESS_HOME defaults to ~/.ai-harness). Audit trail: the engine's
$AI_SAFE_COMMAND_AUDIT override applies; entries carry the codex session_id.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

_HARNESS_HOME = Path(os.environ.get("AI_HARNESS_HOME")
                     or Path.home() / ".ai-harness")
_ENGINE = _HARNESS_HOME / "hooks" / "safe_command.py"


def _emit(decision: str, reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": f"safe_command: {reason}",
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def _load_engine():
    spec = importlib.util.spec_from_file_location("safe_command", _ENGINE)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine


def _audit(decision: str, reason: str, command: str, session_id: str) -> None:
    # Best-effort, through the engine so AI_SAFE_COMMAND_AUDIT is honored.
    try:
        _load_engine()._audit(decision, reason, command, session_id)
    except Exception:
        pass


def main() -> None:
    session_id = ""
    command = ""
    try:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError, ValueError):
            _audit("deny", "codex adapter: malformed hook payload",
                   command, session_id)
            _emit("deny", "codex adapter: malformed hook payload")

        session_id = payload.get("session_id", "") or ""
        # Other tools reach codex too; the engine only reads Bash commands.
        if payload.get("tool_name") != "Bash":
            sys.exit(0)
        command = (payload.get("tool_input") or {}).get("command", "") or ""
        if not command:
            sys.exit(0)

        try:
            decision, reason = _load_engine().evaluate(command)
        except Exception as exc:  # noqa: BLE001
            _audit("deny", f"codex adapter: engine error ({type(exc).__name__})",
                   command, session_id)
            _emit("deny", f"codex adapter: engine error ({type(exc).__name__})")

        if decision == "deny":
            _audit("deny", reason, command, session_id)
            _emit("deny", reason)
        elif decision == "ask":
            # codex exec treats "ask" as a failed hook and runs the command:
            # demote to deny, keeping the original judgment visible.
            demoted = f"ASK-demotion (codex): {reason}"
            _audit("deny", demoted, command, session_id)
            _emit("deny", demoted)
        elif decision == "allow-explicit":
            _audit("allow", reason, command, session_id)
            _emit("allow", reason)
        else:
            # Silent default allow — same as the engine under Claude Code.
            sys.exit(0)

    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        _emit("deny", f"codex adapter internal error: {type(exc).__name__}")


if __name__ == "__main__":
    main()
