#!/usr/bin/env python3
"""
Claude Code PostToolUse hook: logs Bash, Edit, and Write tool calls
to commands_history.md in the active session context directory.

Session dir resolution (in order):
1. Match session_id from payload against ~/tmp/claude/ctx/*/.session files
2. Infer from file_path in Edit/Write tool_input if it's under ~/tmp/claude/ctx/<alias>/
3. Fall back to the most recently modified session dir that has a prompts_history.md
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

CTX_ROOT = Path.home() / "tmp" / "claude" / "ctx"


def find_session_dir_by_cwd() -> Path | None:
    """Determine session dir from PWD if under ctx."""
    cwd = Path(os.getcwd()).resolve()
    ctx_resolved = CTX_ROOT.resolve()

    if cwd == ctx_resolved:
        return None  # At ctx root, need fallback

    try:
        rel = cwd.relative_to(ctx_resolved)
    except ValueError:
        return None

    session_name = rel.parts[0]
    candidate = ctx_resolved / session_name
    if candidate.is_dir():
        return candidate
    return None


def find_session_dir_by_id(session_id: str) -> Path | None:
    """Match session_id against .session files."""
    if not session_id or not CTX_ROOT.is_dir():
        return None
    for session_file in CTX_ROOT.glob("*/.session"):
        try:
            if session_file.read_text().strip() == session_id:
                return session_file.parent
        except OSError:
            continue
    return None


def find_session_dir_by_path(tool_input: dict) -> Path | None:
    """Infer session dir from file_path in Edit/Write tool_input."""
    fp = tool_input.get("file_path", "")
    ctx_prefix = str(CTX_ROOT) + "/"
    if fp.startswith(ctx_prefix):
        remainder = fp[len(ctx_prefix):]
        alias = remainder.split("/")[0]
        if alias and alias != ".claude":
            candidate = CTX_ROOT / alias
            if candidate.is_dir():
                return candidate
    return None


def find_session_dir_by_recency() -> Path | None:
    """Last resort: most recently modified session with a .session file."""
    if not CTX_ROOT.is_dir():
        return None
    candidates = []
    for sf in CTX_ROOT.glob("*/.session"):
        try:
            candidates.append((sf.stat().st_mtime, sf.parent))
        except OSError:
            continue
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def format_bash(tool_input: dict) -> str:
    cmd = tool_input.get("command", "")
    desc = tool_input.get("description", "")
    header = "**Bash**"
    if desc:
        header += f" — {desc}"
    return f"{header}\n```bash\n{cmd}\n```"


def format_edit(tool_input: dict) -> str:
    fp = tool_input.get("file_path", "?")
    old = tool_input.get("old_string", "")
    new = tool_input.get("new_string", "")
    replace_all = tool_input.get("replace_all", False)
    lines = [f"**Edit** `{fp}`"]
    if replace_all:
        lines[0] += " (replace_all)"
    old_preview = old.split("\n")[0][:120] if old else ""
    new_preview = new.split("\n")[0][:120] if new else ""
    if old_preview:
        lines.append(f"  - `{old_preview}` {'...' if len(old) > 120 or chr(10) in old[1:] else ''}")
        lines.append(f"  + `{new_preview}` {'...' if len(new) > 120 or chr(10) in new[1:] else ''}")
    return "\n".join(lines)


def format_write(tool_input: dict) -> str:
    fp = tool_input.get("file_path", "?")
    content = tool_input.get("content", "")
    line_count = content.count("\n") + 1 if content else 0
    return f"**Write** `{fp}` ({line_count} lines)"


FORMATTERS = {
    "Bash": format_bash,
    "Edit": format_edit,
    "Write": format_write,
}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    if tool_name not in FORMATTERS:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    tool_input = payload.get("tool_input", {})

    # Resolve session directory — PWD first, then fallbacks
    session_dir = (
        find_session_dir_by_cwd()
        or find_session_dir_by_id(session_id)
        or find_session_dir_by_path(tool_input)
        or find_session_dir_by_recency()
    )
    if session_dir is None:
        sys.exit(0)

    # Update .session file if we found the dir by fallback
    session_file = session_dir / ".session"
    if session_id:
        try:
            current = session_file.read_text().strip() if session_file.exists() else ""
            if current != session_id:
                session_file.write_text(session_id + "\n")
        except OSError:
            pass

    entry = FORMATTERS[tool_name](tool_input)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    history_file = session_dir / ".commands_history.md"
    header_needed = not history_file.exists()

    with open(history_file, "a") as f:
        if header_needed:
            f.write("# Commands History\n\n")
        f.write(f"### {timestamp}\n{entry}\n\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
