#!/usr/bin/env python3
"""
Claude Code SessionStart hook: flush any stale, un-dumped transcripts
for the current session dir before the new session starts.

Handles the case where a previous session ended via ^C, crash, OS reboot,
or anything else that skipped the SessionEnd hook.

Walks all transcripts under ~/.claude/projects/**/ that have been touched
since the last flush, matches them to a session dir via .session file,
and appends any new content to that session's full_dump.md.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from export_session import (  # noqa: E402
    CTX_ROOT,
    dump_transcript,
    find_session_dir_by_cwd,
    find_session_dir_by_id,
    find_session_dir_by_recency,
    load_state,
    save_state,
)

PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def session_id_from_transcript(path: Path) -> str:
    return path.stem


def session_dir_for_transcript(path: Path) -> Path | None:
    """Resolve the context session dir owning this transcript.

    Prefer matching .session file (by session_id). Fall back to cwd recorded in
    the last entry (if it lies under ~/tmp/claude/ctx/<alias>/).
    """
    sid = session_id_from_transcript(path)
    hit = find_session_dir_by_id(sid)
    if hit:
        return hit

    try:
        with open(path) as f:
            last_cwd = None
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd")
                if cwd:
                    last_cwd = cwd
            if last_cwd:
                cwd_path = Path(last_cwd).resolve()
                ctx_resolved = CTX_ROOT.resolve()
                try:
                    rel = cwd_path.relative_to(ctx_resolved)
                except ValueError:
                    return None
                if not rel.parts:
                    return None
                candidate = ctx_resolved / rel.parts[0]
                if candidate.is_dir():
                    return candidate
    except OSError:
        pass
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        payload = {}

    current_session_id = payload.get("session_id", "")
    current_transcript = payload.get("transcript_path", "")

    current_dir = (
        find_session_dir_by_cwd()
        or find_session_dir_by_id(current_session_id)
        or find_session_dir_by_recency()
    )
    if current_dir is None or not PROJECTS_ROOT.is_dir():
        sys.exit(0)

    state_file = current_dir / ".full_dump_state.json"
    state = load_state(state_file)
    transcripts_seen = state.get("transcripts", {})

    flushed = 0
    for jsonl in PROJECTS_ROOT.glob("*/*.jsonl"):
        tpath = str(jsonl)
        if tpath == current_transcript:
            continue

        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        last_seen_mtime = transcripts_seen.get(f"__mtime__:{tpath}")
        if last_seen_mtime and mtime <= last_seen_mtime:
            continue

        owning_dir = session_dir_for_transcript(jsonl)
        if owning_dir != current_dir:
            continue

        if dump_transcript(current_dir, tpath, "flush-on-start"):
            flushed += 1

        state = load_state(state_file)
        state.setdefault("transcripts", {})[f"__mtime__:{tpath}"] = mtime
        save_state(state_file, state)

    if flushed:
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"flushed {flushed} stale transcript(s) to .full_dump.md",
            }
        }, sys.stdout)

    sys.exit(0)


if __name__ == "__main__":
    # main()
    pass
