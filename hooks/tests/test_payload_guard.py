"""Tests for payload_guard.py — the content guard for indirectly-executed
payloads.

    python3 -m pytest hooks/tests/test_payload_guard.py -v

Everything here is synthetic. The fixtures under tests/fixtures/ were written
for this suite with placeholder hosts and ids; none is a script the guard was
tuned against, so they are a holdout rather than a restatement of the tuning
set. Fixtures containing destructive commands are inert at line 1 — the guard
reads them as text, so that costs nothing.

The suite asserts three things:
  1. a payload that mutates something outside this machine ASKS,
  2. reads, local-only work, data tables and prose stay silent,
  3. the documented gaps really are gaps (marked xfail, so if one ever starts
     working the suite says so instead of hiding it).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_MODULE = _HERE.parent / "payload_guard.py"
_FIXTURES = _HERE / "fixtures"

_spec = importlib.util.spec_from_file_location("payload_guard", _MODULE)
payload_guard = importlib.util.module_from_spec(_spec)
sys.modules["payload_guard"] = payload_guard
_spec.loader.exec_module(payload_guard)

EXPECTED: dict[str, str] = json.loads((_FIXTURES / "expected.json").read_text())


def decide_file(name: str) -> str:
    return payload_guard.decide(payload_guard.scan_file(str(_FIXTURES / name)))[0]


def decide_command(command: str) -> str:
    return payload_guard.decide(
        payload_guard.scan_command(command, cwd=str(_FIXTURES)))[0]


# ── fixture files ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(n for n, v in EXPECTED.items() if v == "ask"))
def test_fixture_asks(name: str):
    """A payload that changes state outside this machine must prompt."""
    decision = decide_file(name)
    assert decision == "ask", f"{name}: expected ask, got {decision}"


@pytest.mark.parametrize("name", sorted(n for n, v in EXPECTED.items() if v == "allow"))
def test_fixture_allows(name: str):
    """Reads, local-only work, data tables and prose must stay silent."""
    hits = payload_guard.scan_file(str(_FIXTURES / name))
    assert not hits, f"{name}: expected allow, got ask ({hits[0]})"


def test_every_fixture_is_labelled():
    on_disk = {p.name for p in _FIXTURES.iterdir() if p.suffix in (".sh", ".py")}
    assert on_disk == set(EXPECTED), (
        f"unlabelled: {on_disk - set(EXPECTED)} | missing: {set(EXPECTED) - on_disk}"
    )


# ── how the payload is reached ─────────────────────────────────────────────
INDIRECTION_ASK = [
    ("interpreter + file", "python3 glab_post_mr_note.py"),
    ("bash + file", "bash vault_write_secret.sh"),
    ("sh + file", "sh dd_wipe_disk.sh"),
    ("exec bit", "./kubectl_delete_namespace.sh"),
    ("source", "source vault_kv_put.sh"),
    ("dot-source", ". vault_kv_put.sh"),
    ("cd then run", "cd /tmp && python3 slack_post_message.py"),
    ("absolute path", str(_FIXTURES / "terraform_destroy.sh")),
]


@pytest.mark.parametrize("label,command", INDIRECTION_ASK, ids=[c[0] for c in INDIRECTION_ASK])
def test_indirection_is_followed(label: str, command: str):
    if command.startswith("/"):
        command = f"bash {command}"
    assert decide_command(command) == "ask", f"{label}: payload not followed"


INLINE_CASES = [
    # (label, command, expected)
    ("heredoc into python3 -",
     "python3 - <<'EOF'\nimport subprocess\n"
     "subprocess.run(['vault','write','secret/example/x','k=v'])\nEOF", "ask"),
    ("heredoc into bash",
     "bash <<'EOF'\naws s3 cp ./out s3://example-bucket/out\nEOF", "ask"),
    ("python3 -c inline",
     "python3 -c \"import subprocess; subprocess.run(['kubectl','delete','ns','example'])\"", "ask"),
    ("python3 -c harmless", "python3 -c 'print(1 + 1)'", "allow"),
    ("interpreter, no payload", "python3 -m pytest -q", "allow"),
    ("reads a report script", "python3 read_only_report.py", "allow"),
    ("plain command, not an interpreter", "ls -la /tmp", "allow"),
    ("missing file", "python3 does_not_exist.py", "allow"),
]


@pytest.mark.parametrize("label,command,expected", INLINE_CASES, ids=[c[0] for c in INLINE_CASES])
def test_inline_and_negative(label: str, command: str, expected: str):
    assert decide_command(command) == expected, label


# ── the reason has to be actionable ───────────────────────────────────────
def test_reason_names_file_line_and_excerpt():
    decision, reason = payload_guard.decide(
        payload_guard.scan_file(str(_FIXTURES / "glab_post_mr_note.py")))
    assert decision == "ask"
    assert "glab_post_mr_note.py:" in reason      # which file, which line
    assert "glab" in reason and "api" in reason    # what was seen
    assert "mutating" in reason                    # why it matters
    # The excerpt is a fixed 110-char window, so the flag that triggered the
    # match may fall outside it — the reason names the rule instead.


def test_never_denies():
    """ASK only, by design: a string in a file is weaker evidence than a
    command on a line, and a false deny breaks real work."""
    decisions = {decide_file(n) for n in EXPECTED}
    assert decisions <= {"ask", "allow"}


# ── documented gaps, asserted as gaps ─────────────────────────────────────
GAPS = [
    ("method assembled at runtime",
     "python3 -c \"import subprocess; m='PO'+'ST'; "
     "subprocess.run(['glab','api','projects/0/merge_requests/1/notes','-X',m])\""),
    ("make target indirection", "make deploy"),
    ("npm script indirection", "npm run publish"),
]


@pytest.mark.parametrize("label,command", GAPS, ids=[c[0] for c in GAPS])
@pytest.mark.xfail(reason="known gap: documented in payload_guard's module docstring",
                   strict=True)
def test_known_gaps(label: str, command: str):
    assert decide_command(command) == "ask"
