"""Tests for the memory hooks: the Claude file-tool guard and the Codex
SessionStart index injection. Both find the store through the server's
registration (or $AI_MEMORY_DIR)."""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def run(script, payload, env):
    return subprocess.run([sys.executable, str(HOOKS / script)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=20, env={**os.environ, **env})


def test_guard_denies_file_tools_in_store(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    env = {"AI_MEMORY_DIR": str(mem)}
    for tool, path, cwd in (("Write", str(mem / "x.md"), "/"),
                            ("Edit", "MEMORY.md", str(mem)),
                            ("NotebookEdit", str(mem / "n.ipynb"), "/")):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        r = run("memory_guard.py", {"tool_name": tool, "tool_input": {key: path}, "cwd": cwd}, env)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny" and "memory_" in out["permissionDecisionReason"]


def test_guard_allows_elsewhere_and_other_tools(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    env = {"AI_MEMORY_DIR": str(mem)}
    for tool, path in (("Write", str(tmp_path / "memx" / "a.md")), ("Read", str(mem / "MEMORY.md"))):
        r = run("memory_guard.py", {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": "/"}, env)
        assert r.returncode == 0 and r.stdout == ""


def test_guard_reads_claude_registration(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"memory": {"env": {"AI_MEMORY_DIR": str(mem)}}}}))
    env = {"HOME": str(tmp_path), "AI_MEMORY_DIR": ""}
    r = run("memory_guard.py", {"tool_name": "Write", "tool_input": {"file_path": str(mem / "a.md")}}, env)
    assert '"deny"' in r.stdout


def test_index_context_from_codex_registration(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("- [A](a.md) — first\n- [B](b.md) — second\n")
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text(f'[mcp_servers.memory.env]\nAI_MEMORY_DIR = "{mem}"\n')
    r = run("memory_index_context.py", {}, {"CODEX_HOME": str(codex), "AI_MEMORY_DIR": ""})
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "- [B](b.md) — second" in out["additionalContext"]


def test_index_context_truncates_and_tolerates_missing(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("".join(f"- [M{i}](m{i}.md) — h\n" for i in range(250)))
    out = json.loads(run("memory_index_context.py", {}, {"AI_MEMORY_DIR": str(mem)}).stdout)
    assert "truncated at 200 of 250" in out["hookSpecificOutput"]["additionalContext"]
    r = run("memory_index_context.py", {}, {"AI_MEMORY_DIR": str(tmp_path / "nope")})
    assert r.returncode == 0 and r.stdout == ""
