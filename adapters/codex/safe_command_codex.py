#!/usr/bin/env python3
"""Codex PreToolUse adapter around the ai-harness safe_command engine.

codex's hook runtime is Claude-Code-shaped. Re-verified live on codex-cli
0.169.0 (2026-11-17, `codex exec` + app-server probes with a stdin-dumping hook):

  hooks.json (~/.codex/hooks.json) — same shape as Claude Code settings.
  Matcher names: shell calls arrive as "Bash" (also when the model drives
  exec_command from the `exec` JS tool), file edits as "apply_patch", MCP
  calls as "mcp__<server>__<tool>" (dashes in the tool name become "_").

  stdin payload: {"session_id", "turn_id", "transcript_path", "cwd",
       "hook_event_name": "PreToolUse", "model", "permission_mode",
       "tool_name", "tool_input", "tool_use_id"} (+ "agent_id"/"agent_type"
       in a subagent). Bash: tool_input = {"command": "<cmd>"}.
       apply_patch: tool_input = {"command": "*** Begin Patch ..."}.

  stdout envelope: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
      "permissionDecision": ..., "permissionDecisionReason": ...,
      "updatedInput": ...}}
      — "deny" blocks the call and surfaces the reason to the model.
      — "ask" is unsupported: codex logs "PreToolUse Failed" and RUNS the
        command (fail-open). A bare "allow" is unsupported the same way.
      — "allow" + updatedInput {"command": ...} replaces the command, and
        the replacement then goes through codex's own exec policy.

ASK → a real codex approval prompt. An engine ASK rewrites the command to

    <dir>/safe_command_ask '<reason>' '<display>' <base64 of the command>

and ~/.codex/rules/safe_command.rules carries
    prefix_rule(pattern=["<dir>/safe_command_ask"], decision="prompt")
so codex shows its normal approval prompt (accept / cancel, no "don't ask
again" for a policy prompt). Under `codex exec` (approval never) it is
rejected — fails closed. The gate re-derives the display from the decoded
command and refuses on a mismatch, so a hand-built gate call cannot show one
command and run another. Every argument is a plain single-quoted word or
base64: codex's shell parser gives up on '\\'' concatenation and then the
prefix rule does NOT match (verified: the command ran unprompted), which is
why the display swaps ' for U+2019 instead of escaping it.

If the gate or the rule is missing the ASK stays a deny (the pre-0.169
behaviour), with a `!`-ready command for the human.

apply_patch: denied when a target file carries the uchg/schg flag (use the
chflags unlock -> write -> relock chain in ONE shell command; chflags itself
prompts), or is one of the guard's own files (exec-policy rules, hooks.json,
this adapter, the gate, the engine pair).

Trust: codex requires a one-time per-definition trust ("hooks are new or
changed" in the TUI); until then the hook is silently skipped.

Engine: imported by absolute path from $AI_HARNESS_HOME/hooks/safe_command.py
($AI_HARNESS_HOME defaults to ~/.ai-harness). Audit trail: the engine's
$AI_SAFE_COMMAND_AUDIT override applies; entries carry the codex session_id.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

_HARNESS_HOME = Path(os.environ.get("AI_HARNESS_HOME")
                     or Path.home() / ".ai-harness")
_ENGINE = _HARNESS_HOME / "hooks" / "safe_command.py"
_CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
_GATE = Path(__file__).resolve().parent / "safe_command_ask"
_IMMUTABLE = getattr(stat, "UF_IMMUTABLE", 0) | getattr(stat, "SF_IMMUTABLE", 0)
_PATCH_PREFIXES = ("*** Add File: ", "*** Update File: ", "*** Delete File: ",
                   "*** Move to: ")


def _emit(decision: str, reason: str, updated: dict | None = None) -> None:
    out = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": f"safe_command: {reason}",
    }
    if updated is not None:
        out["updatedInput"] = updated
    json.dump({"hookSpecificOutput": out}, sys.stdout)
    sys.exit(0)


def _load_engine():
    spec = importlib.util.spec_from_file_location("safe_command", _ENGINE)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine


def _audit(decision: str, reason: str, command: str, session_id: str) -> None:
    # Best-effort, through the engine so AI_SAFE_COMMAND_AUDIT is honored.
    try:
        _load_engine()._audit(decision, reason, command, session_id)
    except Exception:
        pass


# ── ASK → codex approval prompt ──────────────────────────────────────────────

def display_of(command: str) -> str:
    """Prompt-safe rendering: a ' would force '\\'' quoting, which defeats the rule."""
    return command.replace("'", "’")


def reason_of(reason: str) -> str:
    return " ".join(reason.replace("'", "’").split())


def gate_command(reason: str, command: str, gate: Path | None = None) -> str:
    gate = gate or _GATE
    b64 = base64.b64encode(command.encode()).decode()
    return (f"{gate} 'safe_command ASK: {reason_of(reason)}' "
            f"'{display_of(command)}' {b64}")


def gate_ready(gate: Path | None = None, rules_dir: Path | None = None) -> bool:
    """The gate is executable and some *.rules file makes codex prompt on it."""
    gate = gate or _GATE
    rules_dir = rules_dir or _CODEX_HOME / "rules"
    if not (gate.is_file() and os.access(gate, os.X_OK)):
        return False
    needle = f'"{gate}"'
    try:
        for rf in rules_dir.glob("*.rules"):
            for line in rf.read_text().splitlines():
                s = line.split("#", 1)[0]
                if "prefix_rule" in s and needle in s and '"prompt"' in s:
                    return True
    except OSError:
        return False
    return False


# ── apply_patch ──────────────────────────────────────────────────────────────

def patch_paths(patch: str, cwd: str) -> list[Path]:
    out = []
    for line in patch.splitlines():
        for prefix in _PATCH_PREFIXES:
            if line.startswith(prefix):
                p = Path(os.path.expanduser(line[len(prefix):].strip()))
                if not p.is_absolute():
                    p = Path(cwd or os.getcwd()) / p
                out.append(p)
    return out


def _real(p: Path) -> Path:
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(p))


def protected_paths() -> tuple[list[Path], list[Path]]:
    """(exact files, directories) the patch tool must never touch."""
    files = [_CODEX_HOME / "hooks.json", Path(__file__).resolve(), _GATE,
             _ENGINE, _ENGINE.with_name("payload_guard.py")]
    dirs = [_CODEX_HOME / "rules"]
    return [_real(f) for f in files], [_real(d) for d in dirs]


def check_patch(patch: str, cwd: str) -> str | None:
    """Reason to deny the patch, or None."""
    files, dirs = protected_paths()
    for p in patch_paths(patch, cwd):
        rp = _real(p)
        if rp in files or any(rp == d or d in rp.parents for d in dirs):
            return (f"apply_patch on guard file {rp} — the harness guards itself; "
                    "edit it by hand")
        try:
            flags = getattr(os.lstat(rp), "st_flags", 0)
        except OSError:
            continue  # new file
        if flags & _IMMUTABLE:
            return (f"apply_patch on uchg-locked {rp} — unlock, write and relock in ONE "
                    f"shell command (chflags prompts): chflags nouchg '{rp}' && "
                    f"<write> && chflags uchg '{rp}'")
    return None


def main() -> None:
    session_id = ""
    command = ""
    try:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError, ValueError):
            _audit("deny", "codex adapter: malformed hook payload",
                   command, session_id)
            _emit("deny", "codex adapter: malformed hook payload")

        session_id = payload.get("session_id", "") or ""
        tool = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}

        if tool == "apply_patch":
            patch = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
            why = check_patch(str(patch or ""), payload.get("cwd") or "")
            if why:
                _audit("deny", why, "apply_patch", session_id)
                _emit("deny", why)
            sys.exit(0)

        # The engine only reads shell commands.
        if tool != "Bash":
            sys.exit(0)
        command = tool_input.get("command", "") or ""
        if not command:
            sys.exit(0)

        try:
            decision, reason = _load_engine().evaluate(command)
        except Exception as exc:  # noqa: BLE001
            _audit("deny", f"codex adapter: engine error ({type(exc).__name__})",
                   command, session_id)
            _emit("deny", f"codex adapter: engine error ({type(exc).__name__})")

        if decision == "deny":
            _audit("deny", reason, command, session_id)
            _emit("deny", reason)
        elif decision == "ask":
            if gate_ready():
                _audit("ask", f"codex gate: {reason}", command, session_id)
                _emit("allow", f"ASK via codex approval prompt: {reason}",
                      {"command": gate_command(reason, command)})
            # No gate/rule: a bare ask would fail open, so it stays a deny.
            demoted = (f"ASK-demotion (codex, prompt gate unavailable): {reason}. "
                       f"Hand this to the human to run: ! {command}")
            _audit("deny", demoted, command, session_id)
            _emit("deny", demoted)
        elif decision == "allow-explicit":
            # A bare "allow" is unsupported in codex (logged as a failed hook,
            # then runs): audit it and allow silently instead.
            _audit("allow", reason, command, session_id)
            sys.exit(0)
        else:
            # Silent default allow — same as the engine under Claude Code.
            sys.exit(0)

    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        _emit("deny", f"codex adapter internal error: {type(exc).__name__}")


if __name__ == "__main__":
    main()
