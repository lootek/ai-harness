#!/usr/bin/env python3
"""
Claude Code SessionEnd hook: append the session transcript to
full_dump.md in the active session context directory.

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


def render_content(content) -> str:
    """Render a message.content field (string or list of parts) to markdown."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", "").strip())
        elif btype == "thinking":
            thinking = block.get("thinking", "").strip()
            if thinking:
                parts.append(f"<thinking>\n{thinking}\n</thinking>")
        elif btype == "tool_use":
            name = block.get("name", "?")
            tool_input = block.get("input", {})
            try:
                rendered = json.dumps(tool_input, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                rendered = str(tool_input)
            parts.append(f"**Tool call: {name}**\n```json\n{rendered}\n```")
        elif btype == "tool_result":
            tr = block.get("content", "")
            if isinstance(tr, list):
                tr = "\n".join(
                    b.get("text", "") for b in tr if isinstance(b, dict) and b.get("type") == "text"
                )
            tr = str(tr).strip()
            if tr:
                if len(tr) > 4000:
                    tr = tr[:4000] + "\n... [truncated]"
                parts.append(f"**Tool result:**\n```\n{tr}\n```")
    return "\n\n".join(p for p in parts if p)


def render_transcript(transcript_path: Path, skip_until_uuid: str | None) -> tuple[str, str | None]:
    """Render new entries after skip_until_uuid. Returns (rendered, last_uuid)."""
    lines = []
    last_uuid: str | None = skip_until_uuid
    skipping = skip_until_uuid is not None

    try:
        with open(transcript_path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                uuid = entry.get("uuid")

                if skipping:
                    if uuid == skip_until_uuid:
                        skipping = False
                    continue

                etype = entry.get("type")
                if etype not in ("user", "assistant"):
                    if uuid:
                        last_uuid = uuid
                    continue

                message = entry.get("message", {})
                content = message.get("content", "")
                body = render_content(content)
                if uuid:
                    last_uuid = uuid
                if not body:
                    continue

                ts = entry.get("timestamp", "")
                role = "User" if etype == "user" else "Assistant"
                header = f"### {role}"
                if ts:
                    header += f" — {ts}"
                lines.append(f"{header}\n\n{body}")
    except OSError as e:
        return f"_Failed to read transcript: {e}_", last_uuid

    return "\n\n---\n\n".join(lines), last_uuid


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_file: Path, state: dict) -> None:
    try:
        state_file.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def dump_transcript(session_dir: Path, transcript_path: str, reason: str, event: str = "SessionEnd") -> bool:
    """Append any new content from transcript_path to full_dump.md. Returns True if content was written."""
    tpath = Path(transcript_path)
    if not tpath.is_file():
        return False

    dump_file = session_dir / ".full_dump.md"
    state_file = session_dir / ".full_dump_state.json"
    header_needed = not dump_file.exists()

    state = load_state(state_file)
    transcripts = state.setdefault("transcripts", {})
    skip_until = transcripts.get(transcript_path)

    body, last_uuid = render_transcript(tpath, skip_until)
    if not body:
        return False

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(dump_file, "a") as f:
        if header_needed:
            f.write(f"# Session: {session_dir.name} — Full Dump\n")
        f.write(f"\n## {event} ({reason}) at {timestamp}\n")
        f.write(f"_Transcript: `{transcript_path}`_\n\n")
        f.write(body + "\n")

    if last_uuid:
        transcripts[transcript_path] = last_uuid
        save_state(state_file, state)
    return True


def resolve_session_dir(session_id: str) -> Path | None:
    return (
        find_session_dir_by_cwd()
        or find_session_dir_by_id(session_id)
        or find_session_dir_by_recency()
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        sys.exit(0)

    session_dir = resolve_session_dir(payload.get("session_id", ""))
    if session_dir is None:
        sys.exit(0)

    dump_transcript(
        session_dir,
        transcript_path,
        payload.get("reason", "unknown"),
        payload.get("hook_event_name", "SessionEnd"),
    )
    sys.exit(0)


if __name__ == "__main__":
    # main()
    pass
