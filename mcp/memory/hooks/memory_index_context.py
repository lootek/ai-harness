#!/usr/bin/env python3
"""Codex SessionStart hook: put the shared memory index (MEMORY.md) into the
session's context, as Claude Code does natively for its auto-memory.

The store's location comes from the `memory` MCP server's registration
($CODEX_HOME/config.toml [mcp_servers.memory.env] AI_MEMORY_DIR), or
$AI_MEMORY_DIR. Capped like Claude's injection (200 lines / 25 KB). Any fault
exits 0 with no output: a missing index must not break a session start.
"""

import json
import os
import sys
import tomllib
from pathlib import Path

MAX_LINES = 200
MAX_BYTES = 25_000


def memory_dir() -> Path | None:
    d = os.environ.get("AI_MEMORY_DIR")
    if not d:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            cfg = tomllib.loads((home / "config.toml").read_text())
            d = cfg["mcp_servers"]["memory"]["env"]["AI_MEMORY_DIR"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return Path(os.path.expanduser(d))


def main() -> None:
    mem = memory_dir()
    if mem is None:
        return
    index = mem / "MEMORY.md"
    try:
        lines = index.read_text().splitlines()
    except OSError:
        return
    body, size, cut = [], 0, False
    for line in lines:
        size += len(line.encode()) + 1
        if len(body) >= MAX_LINES or size > MAX_BYTES:
            cut = True
            break
        body.append(line)
    text = (f"Contents of {index} (shared memory index, one line per memory; "
            "memory_view <file> for the full text, memory_search for anything else. "
            "Memories are dated leads: re-verify before acting):\n\n" + "\n".join(body))
    if cut:
        text += f"\n\n[index truncated at {len(body)} of {len(lines)} lines: use memory_search]"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text,
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
