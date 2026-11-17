"""Unit tests for the codex envelope adapter (adapters/codex/safe_command_codex.py).

The adapter maps the engine's Claude-Code-shaped verdicts onto codex's
hookSpecificOutput envelope. The one behavioural change lives here, so it is
tested directly: codex treats an "ask" verdict as a failed hook and RUNS the
command (verified live, codex-cli 0.157.1 and 0.169.0), so ASK is rewritten to
the safe_command_ask gate that codex's exec policy prompts on, or demoted to
deny when the gate/rule is missing. apply_patch targets that are uchg-locked or
guard files are denied. Everything else — deny passthrough, silent default
allow, malformed-stdin fail-closed, engine-fault fail-closed — must mirror the
engine's own semantics.

The engine is faked (monkeypatched _load_engine): these tests exercise the
adapter's mapping logic only, not the rule set (that's test_safe_command.py).
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_ADAPTER = (Path(__file__).resolve().parents[2]
            / "adapters" / "codex" / "safe_command_codex.py")
_spec = importlib.util.spec_from_file_location("safe_command_codex", _ADAPTER)
adapter = importlib.util.module_from_spec(_spec)
sys.modules["safe_command_codex"] = adapter
_spec.loader.exec_module(adapter)


class FakeEngine:
    def __init__(self, decision: str, reason: str = "engine reason"):
        self.decision = decision
        self.reason = reason
        self.audits: list[tuple] = []

    def evaluate(self, command: str) -> tuple[str, str]:
        return self.decision, self.reason

    def _audit(self, decision, reason, command, session_id) -> None:
        self.audits.append((decision, reason, command, session_id))


def run_adapter(monkeypatch, payload, engine):
    """Feed payload to the adapter, return (envelope-or-None, exit_code)."""
    monkeypatch.setattr(adapter, "_load_engine", lambda: engine)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc:
        adapter.main()
    out = sys.stdout.getvalue()
    return (json.loads(out) if out.strip() else None), exc.value.code


def stdin(command: str, tool: str = "Bash") -> str:
    return json.dumps({
        "session_id": "ses_test", "turn_id": "t", "transcript_path": "",
        "cwd": "/tmp", "hook_event_name": "PreToolUse", "model": "m",
        "permission_mode": "bypassPermissions", "tool_name": tool,
        "tool_input": {"command": command}, "tool_use_id": "call_x",
    })


def envelope_of(out):
    return out["hookSpecificOutput"]


def test_deny_passthrough(monkeypatch, capsys):
    eng = FakeEngine("deny", "dd with zero/random source")
    out, code = run_adapter(monkeypatch, stdin("dd if=/dev/zero of=/dev/sda"),
                            eng)
    env = envelope_of(out)
    assert code == 0
    assert env["permissionDecision"] == "deny"
    assert "dd with zero/random source" in env["permissionDecisionReason"]
    assert "ASK-demotion" not in env["permissionDecisionReason"]
    assert eng.audits == [("deny", "dd with zero/random source",
                           "dd if=/dev/zero of=/dev/sda", "ses_test")]


@pytest.fixture()
def gate_home(tmp_path, monkeypatch):
    """An executable gate + a codex rules dir that prompts on it."""
    gate = tmp_path / "hooks" / "safe_command_ask"
    gate.parent.mkdir()
    gate.write_text("#!/bin/sh\n")
    gate.chmod(0o755)
    rules = tmp_path / "codex" / "rules"
    rules.mkdir(parents=True)
    (rules / "safe_command.rules").write_text(
        f'prefix_rule(pattern=["{gate}"], decision="prompt")\n')
    monkeypatch.setattr(adapter, "_GATE", gate)
    monkeypatch.setattr(adapter, "_CODEX_HOME", tmp_path / "codex")
    return gate


def test_ask_rewrites_to_prompt_gate(monkeypatch, capsys, gate_home):
    eng = FakeEngine("ask", "git rebase — confirm target")
    cmd = "git rebase main && echo 'done'"
    out, code = run_adapter(monkeypatch, stdin(cmd), eng)
    env = envelope_of(out)
    assert code == 0
    # allow + updatedInput: codex then evaluates the gate call against the
    # prompt rule and shows its own approval prompt.
    assert env["permissionDecision"] == "allow"
    new = env["updatedInput"]["command"]
    assert new.startswith(f"{gate_home} 'safe_command ASK: git rebase")
    argv = shlex.split(new)
    assert argv[0] == str(gate_home)
    assert argv[2] == "git rebase main && echo \u2019done\u2019"
    assert base64.b64decode(argv[3]).decode() == cmd
    # Exactly three single-quoted words, no '\\'' concatenation (the codex
    # parser gives up on that and the prompt rule would not match).
    assert "'\\''" not in new and new.count("'") == 4
    assert eng.audits[0][0] == "ask"


def test_ask_reason_quotes_are_neutralised(monkeypatch, capsys, gate_home):
    eng = FakeEngine("ask", "it's\nrisky")
    out, _ = run_adapter(monkeypatch, stdin("x"), eng)
    new = envelope_of(out)["updatedInput"]["command"]
    assert shlex.split(new)[1] == "safe_command ASK: it\u2019s risky"


def test_ask_without_rule_stays_deny(monkeypatch, capsys, gate_home, tmp_path):
    (tmp_path / "codex" / "rules" / "safe_command.rules").write_text(
        f'prefix_rule(pattern=["{gate_home}"], decision="allow")\n')
    eng = FakeEngine("ask", "git rebase — confirm target")
    out, code = run_adapter(monkeypatch, stdin("git rebase main"), eng)
    env = envelope_of(out)
    assert code == 0
    assert env["permissionDecision"] == "deny"  # a bare ask would fail open
    reason = env["permissionDecisionReason"]
    assert "ASK-demotion (codex, prompt gate unavailable): git rebase" in reason
    assert reason.endswith("! git rebase main")
    assert eng.audits[0][0] == "deny"


def test_ask_without_gate_stays_deny(monkeypatch, capsys, gate_home):
    gate_home.unlink()
    eng = FakeEngine("ask", "r")
    out, _ = run_adapter(monkeypatch, stdin("chflags nouchg x"), eng)
    assert envelope_of(out)["permissionDecision"] == "deny"


def test_allow_explicit_is_audited_and_silent(monkeypatch, capsys):
    # A bare "allow" is unsupported in codex 0.169 (logged as a failed hook).
    eng = FakeEngine("allow-explicit", "pre-approved command")
    out, code = run_adapter(monkeypatch, stdin("glab mr list"), eng)
    assert out is None
    assert code == 0
    assert eng.audits[0][0] == "allow"


def patch_stdin(patch: str, cwd: str = "/tmp") -> str:
    d = json.loads(stdin(patch, tool="apply_patch"))
    d["cwd"] = cwd
    return json.dumps(d)


def test_apply_patch_plain_file_allowed(monkeypatch, capsys, tmp_path):
    eng = FakeEngine("deny", "never consulted")
    p = "*** Begin Patch\n*** Add File: new.txt\n+hi\n*** End Patch"
    out, code = run_adapter(monkeypatch, patch_stdin(p, str(tmp_path)), eng)
    assert out is None and code == 0 and eng.audits == []


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="needs BSD file flags")
def test_apply_patch_uchg_file_denied(monkeypatch, capsys, tmp_path):
    f = tmp_path / "locked.txt"
    f.write_text("hi\n")
    os.chflags(f, stat.UF_IMMUTABLE)
    try:
        eng = FakeEngine("allow", "")
        p = "*** Begin Patch\n*** Update File: locked.txt\n@@\n-hi\n+bye\n*** End Patch"
        out, code = run_adapter(monkeypatch, patch_stdin(p, str(tmp_path)), eng)
        env = envelope_of(out)
        assert env["permissionDecision"] == "deny"
        assert "uchg-locked" in env["permissionDecisionReason"]
        assert "chflags nouchg" in env["permissionDecisionReason"]
    finally:
        os.chflags(f, 0)


def test_apply_patch_guard_files_denied(monkeypatch, capsys, tmp_path, gate_home):
    eng = FakeEngine("allow", "")
    rules = tmp_path / "codex" / "rules" / "x.rules"
    for target in (rules, tmp_path / "codex" / "hooks.json", gate_home):
        p = f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch"
        out, _ = run_adapter(monkeypatch, patch_stdin(p), eng)
        env = envelope_of(out)
        assert env["permissionDecision"] == "deny", target
        assert "guard file" in env["permissionDecisionReason"]
        capsys.readouterr()  # run_adapter reads the accumulated stdout
    # A move onto a guard file counts too.
    p = ("*** Begin Patch\n*** Update File: /tmp/a.txt\n"
         f"*** Move to: {tmp_path / 'codex' / 'hooks.json'}\n*** End Patch")
    out, _ = run_adapter(monkeypatch, patch_stdin(p), eng)
    assert envelope_of(out)["permissionDecision"] == "deny"


def test_apply_patch_memory_store_denied(monkeypatch, capsys, tmp_path, gate_home):
    monkeypatch.delenv("AI_MEMORY_DIR", raising=False)
    mem = tmp_path / "mem"
    mem.mkdir()
    (tmp_path / "codex").mkdir(exist_ok=True)
    (tmp_path / "codex" / "config.toml").write_text(
        f'[mcp_servers.memory.env]\nAI_MEMORY_DIR = "{mem}"\n')
    eng = FakeEngine("allow", "")
    for target in (mem / "MEMORY.md", mem / "feedback_x.md"):
        p = f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch"
        out, _ = run_adapter(monkeypatch, patch_stdin(p), eng)
        env = envelope_of(out)
        assert env["permissionDecision"] == "deny", target
        assert "memory_" in env["permissionDecisionReason"]
        capsys.readouterr()
    p = f"*** Begin Patch\n*** Add File: {tmp_path / 'other.md'}\n+x\n*** End Patch"
    out, code = run_adapter(monkeypatch, patch_stdin(p), eng)
    assert out is None and code == 0


def test_apply_patch_no_memory_server_no_guard(monkeypatch, capsys, tmp_path, gate_home):
    monkeypatch.delenv("AI_MEMORY_DIR", raising=False)
    p = f"*** Begin Patch\n*** Add File: {tmp_path / 'mem' / 'MEMORY.md'}\n+x\n*** End Patch"
    out, code = run_adapter(monkeypatch, patch_stdin(p), FakeEngine("allow", ""))
    assert out is None and code == 0


# ── the gate itself ──────────────────────────────────────────────────────────

_GATE_SRC = _ADAPTER.with_name("safe_command_ask")


def run_gate(*argv: str):
    return subprocess.run([sys.executable, str(_GATE_SRC), *argv],
                          capture_output=True, text=True,
                          env={**os.environ, "SHELL": "/bin/sh"})


def test_gate_runs_the_approved_command(tmp_path):
    cmd = "printf '%s' it\\'s; echo"
    b64 = base64.b64encode(cmd.encode()).decode()
    r = run_gate("reason", adapter.display_of(cmd), b64)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "it's\n"


def test_gate_refuses_display_mismatch():
    b64 = base64.b64encode(b"echo evil").decode()
    r = run_gate("reason", "echo harmless", b64)
    assert r.returncode == 126 and r.stdout == ""
    assert "differs" in r.stderr


def test_gate_refuses_bad_payload():
    assert run_gate("reason", "x", "not base64!").returncode == 126
    assert run_gate("only-two", "args").returncode == 126


def test_install_ships_gate_rule_template():
    tpl = (_ADAPTER.with_name("safe_command.rules")).read_text()
    assert '"__AI_HARNESS_HOME__/adapters/codex/safe_command_ask"' in tpl
    assert 'decision="prompt"' in tpl


def test_plain_allow_is_silent(monkeypatch, capsys):
    eng = FakeEngine("allow", "")
    out, code = run_adapter(monkeypatch, stdin("echo hi"), eng)
    assert out is None
    assert code == 0
    assert eng.audits == []  # engine audits nothing on the silent default


def test_non_bash_tool_ignored(monkeypatch, capsys):
    eng = FakeEngine("deny", "should never be consulted")
    out, code = run_adapter(monkeypatch, stdin("x", tool="Read"), eng)
    assert out is None
    assert code == 0
    assert eng.audits == []


def test_malformed_stdin_fails_closed(monkeypatch, capsys):
    eng = FakeEngine("allow", "")
    out, code = run_adapter(monkeypatch, "NOT JSON {{", eng)
    env = envelope_of(out)
    assert code == 0
    assert env["permissionDecision"] == "deny"
    assert "malformed hook payload" in env["permissionDecisionReason"]


def test_engine_exception_fails_closed(monkeypatch, capsys):
    class BrokenEngine:
        def evaluate(self, command):
            raise RuntimeError("boom")
        def _audit(self, *a):
            pass
    monkeypatch.setattr(adapter, "_load_engine", lambda: BrokenEngine())
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin("echo hi")))
    with pytest.raises(SystemExit) as exc:
        adapter.main()
    env = envelope_of(json.loads(sys.stdout.getvalue()))
    assert exc.value.code == 0
    assert env["permissionDecision"] == "deny"
    assert "engine error (RuntimeError)" in env["permissionDecisionReason"]


def test_missing_engine_fails_closed(monkeypatch, capsys):
    # No fake: point the adapter at a home with no engine file. (_ENGINE is
    # resolved at import time, so patch the attribute, not the env.)
    monkeypatch.setattr(adapter, "_ENGINE",
                        Path("/nonexistent-ai-harness/hooks/safe_command.py"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin("echo hi")))
    with pytest.raises(SystemExit) as exc:
        adapter.main()
    env = envelope_of(json.loads(sys.stdout.getvalue()))
    assert exc.value.code == 0
    assert env["permissionDecision"] == "deny"
    assert "engine error" in env["permissionDecisionReason"]
