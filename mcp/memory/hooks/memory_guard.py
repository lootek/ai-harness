#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Write|Edit|MultiEdit|NotebookEdit): keep file
tools out of the shared memory store.

The store is written only through the `memory` MCP server (memory_* tools),
which keeps MEMORY.md in sync, refuses duplicates and hash-checks edits. A
direct Write/Edit bypasses all of that, so it is denied with a pointer to the
right tool. The server itself writes from its own process and is unaffected.

The store's location comes from the server's registration
(~/.claude.json mcpServers.memory.env.AI_MEMORY_DIR), or $AI_MEMORY_DIR. No
registration means no guard. Any fault exits 0: a guard bug must not block
unrelated edits.
"""

import json
import os
import sys
from pathlib import Path

TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def memory_dir() -> Path | None:
    d = os.environ.get("AI_MEMORY_DIR")
    if not d:
        try:
            cfg = json.loads((Path.home() / ".claude.json").read_text())
            d = cfg["mcpServers"]["memory"]["env"]["AI_MEMORY_DIR"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return Path(os.path.expanduser(d)).resolve()


def target(tool_input: dict, cwd: str) -> Path | None:
    p = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not p:
        return None
    p = Path(os.path.expanduser(p))
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    return p.resolve()


def main() -> None:
    payload = json.load(sys.stdin)
    if payload.get("tool_name") not in TOOLS:
        return
    mem = memory_dir()
    t = target(payload.get("tool_input") or {}, payload.get("cwd") or "")
    if mem is None or t is None or not (t == mem or mem in t.parents):
        return
    reason = (f"{t.name} is in the shared memory store: write it with the memory_* tools "
              "(memory_create / memory_str_replace / memory_insert / memory_supersede; "
              "memory_view first for the hash). If they are unavailable in this session, "
              "say so and give the user the change instead.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
