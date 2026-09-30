"""Unit tests for the codex envelope adapter (adapters/codex/safe_command_codex.py).

The adapter maps the engine's Claude-Code-shaped verdicts onto codex's
hookSpecificOutput envelope. The one behavioural change lives here, so it is
tested directly: codex exec treats an "ask" verdict as a failed hook and RUNS
the command (verified live, codex-cli 0.157.1), so ASK is demoted to deny with
the original reason prefixed. Everything else — deny passthrough, explicit
allow, silent default allow, malformed-stdin fail-closed, engine-fault
fail-closed — must mirror the engine's own semantics.

The engine is faked (monkeypatched _load_engine): these tests exercise the
adapter's mapping logic only, not the rule set (that's test_safe_command.py).
"""
from __future__ import annotations

import importlib.util
import io
import json
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


def test_ask_demoted_to_deny_with_prefix(monkeypatch, capsys):
    eng = FakeEngine("ask", "git rebase — confirm target")
    out, code = run_adapter(monkeypatch, stdin("git rebase main"), eng)
    env = envelope_of(out)
    assert code == 0
    assert env["permissionDecision"] == "deny"  # NOT ask: fail-open in codex
    assert "ASK-demotion (codex): git rebase" in env["permissionDecisionReason"]
    # Audited as the verdict codex actually enforced.
    assert eng.audits[0][0] == "deny"
    assert eng.audits[0][1].startswith("ASK-demotion (codex):")


def test_allow_explicit_emits_allow(monkeypatch, capsys):
    eng = FakeEngine("allow-explicit", "pre-approved command")
    out, code = run_adapter(monkeypatch, stdin("glab mr list"), eng)
    env = envelope_of(out)
    assert code == 0
    assert env["permissionDecision"] == "allow"
    assert eng.audits[0][0] == "allow"


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
