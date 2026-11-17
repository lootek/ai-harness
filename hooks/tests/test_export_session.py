"""Tests for hooks/export_session.py: the Codex rollout renderer and the run gate.

Codex rollouts (~/.codex/sessions/**/rollout-*.jsonl) start with a session_meta
line and keep the conversation in response_item lines; progress is a byte
offset so a re-run never re-renders. Claude transcripts are dumped only with
AI_EXPORT_SESSION_CLAUDE=1 (the dump has been off for Claude since the first
harness commit).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "export_session.py"
_spec = importlib.util.spec_from_file_location("export_session", _HOOK)
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)


def line(t: str, payload: dict, ts: str = "2026-11-17T10:00:00.000Z") -> str:
    # compact separators, like codex writes them (the prefilter relies on it)
    return json.dumps({"timestamp": ts, "type": t, "payload": payload},
                      separators=(",", ":")) + "\n"


def rollout(tmp_path: Path, extra: list[str] = ()) -> Path:
    p = tmp_path / "rollout-2026-11-17T10-00-00-abc.jsonl"
    p.write_text("".join([
        line("session_meta", {"id": "abc", "cwd": "/x"}),
        line("response_item", {"type": "message", "role": "developer",
                               "content": [{"type": "input_text", "text": "SYSTEM"}]}),
        line("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": "<environment_context>x"}]}),
        line("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": "hello codex"}]}),
        line("world_state", {"full": True, "state": {"big": "x" * 5000}}),
        line("response_item", {"type": "custom_tool_call", "name": "exec",
                               "input": "await tools.exec_command({cmd:'ls'})"}),
        line("response_item", {"type": "custom_tool_call_output",
                               "output": [{"type": "input_text", "text": "a.txt"}]}),
        line("response_item", {"type": "function_call", "namespace": "collaboration",
                               "name": "spawn_agent", "arguments": "{\"task_name\":\"t\"}"}),
        line("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "gAAA"}),
        line("response_item", {"type": "message", "role": "assistant",
                               "content": [{"type": "output_text", "text": "hi Piotr"}]}),
        *extra,
    ]))
    return p


def test_detects_codex_rollout(tmp_path):
    assert es.is_codex_rollout(rollout(tmp_path))
    claude = tmp_path / "c.jsonl"
    claude.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n")
    assert not es.is_codex_rollout(claude)


def test_renders_conversation_and_tools(tmp_path):
    body, marker = es.render_codex_rollout(rollout(tmp_path), None)
    assert "### User — 2026-11-17T10:00:00.000Z\n\nhello codex" in body
    assert "### Assistant — 2026-11-17T10:00:00.000Z\n\nhi Piotr" in body
    assert "**Tool call: exec**" in body and "exec_command" in body
    assert "**Tool call: collaboration.spawn_agent**" in body
    assert "**Tool result:**\n```\na.txt\n```" in body
    assert "SYSTEM" not in body and "environment_context" not in body
    assert "gAAA" not in body  # encrypted reasoning is skipped
    assert marker == f"codex-offset:{(tmp_path / 'rollout-2026-11-17T10-00-00-abc.jsonl').stat().st_size}"


def test_offset_resume_renders_only_new_lines(tmp_path):
    p = rollout(tmp_path)
    _, marker = es.render_codex_rollout(p, None)
    with open(p, "a") as f:
        f.write(line("response_item", {"type": "message", "role": "user",
                                       "content": [{"type": "input_text", "text": "second turn"}]}))
    body, _ = es.render_codex_rollout(p, marker)
    assert body.count("### ") == 1 and "second turn" in body


def test_partial_last_line_waits(tmp_path):
    p = rollout(tmp_path)
    with open(p, "a") as f:
        f.write('{"timestamp":"t","type":"response_item","payload":{"type":"mess')
    body, marker = es.render_codex_rollout(p, None)
    assert "hi Piotr" in body
    assert int(marker.split(":")[1]) < p.stat().st_size


def test_time_budget_stops_and_resumes(tmp_path):
    p = rollout(tmp_path)
    body, marker = es.render_codex_rollout(p, None, budget_s=-1)
    assert body == "" and marker == "codex-offset:0"


def run_hook(payload: dict, home: Path, env_extra: dict | None = None):
    return subprocess.run([sys.executable, str(_HOOK)], input=json.dumps(payload).encode(),
                          capture_output=True, timeout=30,
                          env={**os.environ, "HOME": str(home), **(env_extra or {})})


def fake_ctx(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    alias = home / "tmp" / "claude" / "ctx" / "probe-alias"
    alias.mkdir(parents=True)
    (alias / ".session").write_text("x\n")
    return home, alias


def test_hook_writes_full_dump_for_codex(tmp_path):
    home, alias = fake_ctx(tmp_path)
    p = rollout(tmp_path)
    payload = {"session_id": "abc", "transcript_path": str(p), "cwd": str(alias),
               "hook_event_name": "SessionEnd", "reason": "exit"}
    r = run_hook(payload, home)
    assert r.returncode == 0
    dump = (alias / ".full_dump.md").read_text()
    assert dump.startswith("# Session: probe-alias — Full Dump")
    assert "## SessionEnd (exit)" in dump and "hello codex" in dump
    run_hook(payload, home)  # nothing new: no second section
    assert (alias / ".full_dump.md").read_text().count("## SessionEnd") == 1


def test_hook_skips_claude_unless_opted_in(tmp_path):
    home, alias = fake_ctx(tmp_path)
    t = tmp_path / "claude.jsonl"
    t.write_text(json.dumps({"type": "user", "uuid": "u1", "timestamp": "t",
                             "message": {"content": "hi claude"}}) + "\n")
    payload = {"session_id": "c", "transcript_path": str(t), "cwd": str(alias),
               "hook_event_name": "SessionEnd", "reason": "exit"}
    run_hook(payload, home)
    assert not (alias / ".full_dump.md").exists()
    run_hook(payload, home, {"AI_EXPORT_SESSION_CLAUDE": "1"})
    assert "hi claude" in (alias / ".full_dump.md").read_text()
