"""Unit tests for the codex history adapters (adapters/codex/log_commands_codex.py
and prompt_history_codex.py).

The adapters are thin: parse the codex hook payload, chdir to payload["cwd"],
spawn the shared engine ($AI_HARNESS_HOME/hooks/{log_commands,prompt_history}.py)
with the payload forwarded verbatim, and exit 0 no matter what. So these tests
run the REAL end-to-end path as subprocesses against an isolated HOME (a fake
~ with the repo's engine copies and a ~/tmp/claude/ctx/<alias> session dir) —
no mocks — and assert the exact history-file lines the claude hooks produce.

Payloads use the Claude-Code envelope shape (session_id, transcript_path, cwd,
hook_event_name, tool_name, tool_input / prompt). PreToolUse field names were
live-verified on codex 0.157.1 in step 3; the PostToolUse/UserPromptSubmit
field names carry the same assumption (flagged in the adapters' docstrings —
live-verify at cutover after the one-time hook trust).

Best-effort contract (the mirror image of safe_command_codex's fail-closed
one): malformed stdin, a missing engine, or any other fault must exit 0
silently and write nothing — history must never break a codex turn.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LOG_ADAPTER = _REPO / "adapters" / "codex" / "log_commands_codex.py"
_PROMPT_ADAPTER = _REPO / "adapters" / "codex" / "prompt_history_codex.py"
_ENGINE_DIR = _REPO / "hooks"

TS_RE = re.compile(r"^### \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", re.M)
PTS_RE = re.compile(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", re.M)


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    """Isolated HOME: .ai-harness/hooks engines + one ctx session dir."""
    home = tmp_path / "home"
    (home / ".ai-harness" / "hooks").mkdir(parents=True)
    for eng in ("log_commands.py", "prompt_history.py"):
        shutil.copy2(_ENGINE_DIR / eng, home / ".ai-harness" / "hooks" / eng)
    alias = home / "tmp" / "claude" / "ctx" / "probe-alias"
    alias.mkdir(parents=True)
    (alias / ".session").write_text("claude-ses-1\n")
    return home


def run_adapter(adapter: Path, payload: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(adapter)],
        input=payload.encode(), capture_output=True,
        env={**os.environ, "HOME": str(home),
             "AI_HARNESS_HOME": str(home / ".ai-harness")},
        timeout=30,
    )


def post_payload(home: Path, tool: str, tool_input: dict, session_id: str = "ses_codex_1") -> str:
    return json.dumps({
        "session_id": session_id, "turn_id": "t", "transcript_path": "",
        "cwd": str(home / "tmp" / "claude" / "ctx" / "probe-alias"),
        "hook_event_name": "PostToolUse", "model": "m",
        "permission_mode": "bypassPermissions", "tool_name": tool,
        "tool_input": tool_input, "tool_use_id": "call_x",
    })


def prompt_payload(home: Path, prompt: str, session_id: str = "ses_codex_1") -> str:
    return json.dumps({
        "session_id": session_id, "transcript_path": "",
        "cwd": str(home / "tmp" / "claude" / "ctx" / "probe-alias"),
        "hook_event_name": "UserPromptSubmit", "prompt": prompt,
    })


def history(fake_home: Path, name: str) -> str:
    return (fake_home / "tmp" / "claude" / "ctx" / "probe-alias" / name).read_text()


def test_bash_command_appended(fake_home: Path):
    proc = run_adapter(_LOG_ADAPTER, post_payload(
        fake_home, "Bash",
        {"command": "echo hi-probe", "description": "probe greeting"}), fake_home)
    assert proc.returncode == 0 and proc.stdout == b""
    text = history(fake_home, ".commands_history.md")
    assert text.startswith("# Commands History\n\n")
    assert TS_RE.search(text)
    assert "**Bash** — probe greeting\n```bash\necho hi-probe\n```" in text


def test_edit_and_write_formats_match_engine(fake_home: Path):
    run_adapter(_LOG_ADAPTER, post_payload(fake_home, "Edit", {
        "file_path": str(fake_home / "tmp" / "claude" / "ctx" / "probe-alias" / "x.py"),
        "old_string": "a\nb", "new_string": "c\nd"}), fake_home)
    run_adapter(_LOG_ADAPTER, post_payload(fake_home, "Write", {
        "file_path": "/tmp/new-file.md", "content": "l1\nl2"}), fake_home)
    text = history(fake_home, ".commands_history.md")
    assert re.search(r"\*\*Edit\*\* `.*probe-alias/x.py`", text)
    assert "  - `a`" in text and "  + `c`" in text
    assert "**Write** `/tmp/new-file.md` (2 lines)" in text


def test_unknown_tool_noop(fake_home: Path):
    run_adapter(_LOG_ADAPTER, post_payload(fake_home, "Bash",
                                           {"command": "echo x"}), fake_home)
    # Read is not in the engine's FORMATTERS -> no entry for it
    run_adapter(_LOG_ADAPTER, post_payload(fake_home, "Read",
                                           {"file_path": "/etc/hosts"}), fake_home)
    assert history(fake_home, ".commands_history.md").count("### ") == 1


def test_session_file_refreshed_with_codex_id(fake_home: Path):
    run_adapter(_LOG_ADAPTER, post_payload(fake_home, "Bash",
                                           {"command": "true"}), fake_home)
    assert (fake_home / "tmp" / "claude" / "ctx" / "probe-alias" / ".session"
            ).read_text().strip() == "ses_codex_1"


def test_prompt_appended(fake_home: Path):
    proc = run_adapter(_PROMPT_ADAPTER,
                       prompt_payload(fake_home, "run: echo hi"), fake_home)
    assert proc.returncode == 0 and proc.stdout == b""
    text = history(fake_home, ".prompts_history.md")
    assert text.startswith(
        "# Session: probe-alias — Prompts History\n")
    assert PTS_RE.search(text)
    assert "\n## " in text and text.rstrip().endswith("run: echo hi")


def test_malformed_stdin_silent_noop(fake_home: Path):
    for adapter in (_LOG_ADAPTER, _PROMPT_ADAPTER):
        proc = run_adapter(adapter, "NOT JSON {", fake_home)
        assert proc.returncode == 0 and proc.stdout == b""
    assert not (fake_home / "tmp" / "claude" / "ctx" / "probe-alias"
                / ".commands_history.md").exists()
    assert not (fake_home / "tmp" / "claude" / "ctx" / "probe-alias"
                / ".prompts_history.md").exists()


def test_missing_engine_silent_noop(fake_home: Path):
    # AI_HARNESS_HOME override pointing nowhere: engine spawn fails, adapter
    # still exits 0 and writes nothing.
    proc = subprocess.run(
        [sys.executable, str(_LOG_ADAPTER)],
        input=post_payload(fake_home, "Bash", {"command": "true"}).encode(),
        capture_output=True, timeout=30,
        env={**os.environ, "HOME": str(fake_home),
             "AI_HARNESS_HOME": str(fake_home / "nope")})
    assert proc.returncode == 0 and proc.stdout == b""
    assert not (fake_home / "tmp" / "claude" / "ctx" / "probe-alias"
                / ".commands_history.md").exists()
