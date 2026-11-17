#!/usr/bin/env python3
"""
Claude Code / Codex SessionEnd + PreCompact hook: append the session
transcript to .full_dump.md in the active session context directory.

Two transcript formats:
- Claude Code JSONL (`type` user/assistant, progress keyed by `uuid`).
- Codex rollout JSONL (~/.codex/sessions/**/rollout-*.jsonl: first line
  `session_meta`, conversation in `response_item` lines). Progress is a byte
  offset, so a re-run seeks past what is already dumped instead of re-parsing.
  Codex clamps SessionEnd hooks to 3 s; the renderer stops at a time budget
  and the next PreCompact/SessionEnd continues from the saved offset.

Run gate: off by default for both CLIs. Each keeps its full transcripts
anyway (~/.claude/projects, ~/.codex/sessions), so the .md copy is opt-in:
AI_EXPORT_SESSION_CLAUDE=1 for Claude transcripts, AI_EXPORT_SESSION_CODEX=1
for Codex rollouts.

Session dir resolution mirrors log_commands.py:
1. payload cwd, then $PWD, if under ~/tmp/claude/ctx/<alias>/
2. Match session_id against ~/tmp/claude/ctx/*/.session files
3. Fall back to the most recently modified session dir
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

CTX_ROOT = Path.home() / "tmp" / "claude" / "ctx"
CODEX_BUDGET_S = 2.0       # of the 3 s SessionEnd clamp; python startup eats the rest
CODEX_OFFSET_PREFIX = "codex-offset:"
TOOL_OUTPUT_MAX = 4000


def find_session_dir_by_cwd(cwd_str: str | None = None) -> Path | None:
    try:
        cwd = Path(cwd_str or os.getcwd()).resolve()
    except OSError:
        return None
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


def is_codex_rollout(transcript_path: Path) -> bool:
    try:
        with open(transcript_path, "rb") as f:
            head = f.read(200)
    except OSError:
        return False
    return b'"type":"session_meta"' in head or b'"type": "session_meta"' in head


# Injected context Codex records as role=user messages; not something Piotr typed.
_CODEX_INJECTED = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions",
                   "<permissions instructions>", "<skills_instructions>", "<turn_aborted>", "<skill>")


def _codex_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        str(c.get("text", "")).strip() for c in content
        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")
        and str(c.get("text", "")).strip()
    )


def _truncate(s: str) -> str:
    s = s.strip()
    return s[:TOOL_OUTPUT_MAX] + "\n... [truncated]" if len(s) > TOOL_OUTPUT_MAX else s


def _codex_block(p: dict) -> tuple[str, str] | None:
    """One response_item payload -> (role, markdown body), or None to skip."""
    t = p.get("type")
    if t == "message":
        role = p.get("role")
        if role not in ("user", "assistant"):
            return None
        body = _codex_text(p.get("content"))
        if not body or (role == "user" and body.lstrip().startswith(_CODEX_INJECTED)):
            return None
        return ("User" if role == "user" else "Assistant", body)
    if t == "reasoning":
        summ = "\n".join(str(s.get("text", "")).strip() for s in p.get("summary") or []
                         if isinstance(s, dict) and s.get("text"))
        return ("Assistant", f"<thinking>\n{summ}\n</thinking>") if summ else None
    if t in ("custom_tool_call", "function_call", "local_shell_call"):
        name = p.get("name") or t
        if p.get("namespace"):
            name = f"{p['namespace']}.{name}"
        raw = p.get("input") if t == "custom_tool_call" else p.get("arguments", p.get("action"))
        if not isinstance(raw, str):
            raw = json.dumps(raw, indent=2, ensure_ascii=False)
        return ("Assistant", f"**Tool call: {name}**\n```\n{_truncate(raw)}\n```")
    if t in ("custom_tool_call_output", "function_call_output"):
        out = p.get("output")
        if isinstance(out, dict):
            out = out.get("content", out)
        text = _codex_text(out) if isinstance(out, list) else str(out or "")
        return ("Tool", f"**Tool result:**\n```\n{_truncate(text)}\n```") if text.strip() else None
    return None


def render_codex_rollout(transcript_path: Path, skip_marker: str | None,
                         budget_s: float = CODEX_BUDGET_S) -> tuple[str, str | None]:
    """Render response_items after the saved byte offset. Returns (rendered, new marker)."""
    offset = 0
    if skip_marker and skip_marker.startswith(CODEX_OFFSET_PREFIX):
        try:
            offset = int(skip_marker[len(CODEX_OFFSET_PREFIX):])
        except ValueError:
            offset = 0
    deadline = time.monotonic() + budget_s
    lines = []
    try:
        with open(transcript_path, "rb") as f:
            if offset:
                f.seek(offset)
            while True:
                if time.monotonic() > deadline:
                    break
                raw = f.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break  # partial line still being written: resume here next time
                offset = f.tell()
                # Cheap prefilter: world_state/turn_context lines are most of the bytes.
                if b'"type":"response_item"' not in raw[:200]:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                blk = _codex_block(entry.get("payload") or {})
                if not blk:
                    continue
                role, body = blk
                ts = entry.get("timestamp", "")
                lines.append(f"### {role}{' — ' + ts if ts else ''}\n\n{body}")
    except OSError as e:
        return f"_Failed to read transcript: {e}_", skip_marker
    return "\n\n---\n\n".join(lines), f"{CODEX_OFFSET_PREFIX}{offset}"


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

    if is_codex_rollout(tpath):
        body, last_uuid = render_codex_rollout(tpath, skip_until)
    else:
        body, last_uuid = render_transcript(tpath, skip_until)
    if not body:
        if last_uuid and last_uuid != skip_until:
            transcripts[transcript_path] = last_uuid
            save_state(state_file, state)
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


def resolve_session_dir(session_id: str, cwd: str | None = None) -> Path | None:
    return (
        (find_session_dir_by_cwd(cwd) if cwd else None)
        or find_session_dir_by_cwd()
        or find_session_dir_by_id(session_id)
        or find_session_dir_by_recency()
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "") or ""
    if not transcript_path:
        sys.exit(0)
    flag = "AI_EXPORT_SESSION_CODEX" if is_codex_rollout(Path(transcript_path)) else "AI_EXPORT_SESSION_CLAUDE"
    if os.environ.get(flag) != "1":
        sys.exit(0)  # off by default: both CLIs keep their own full transcripts

    session_dir = resolve_session_dir(payload.get("session_id", ""), payload.get("cwd"))
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
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — a dump must never break a session end
        sys.exit(0)
