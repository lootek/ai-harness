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

Interactive sessions: a command that opens something codex could type into
later with write_stdin (a shell with no command, ssh with no remote command,
`docker/kubectl exec -i`, su, sudo -i, tmux/screen, script, expect) is an ASK,
because the typed lines reach no hook. REPLs are not covered.

apply_patch: denied when a target file carries the uchg/schg flag (use the
chflags unlock -> write -> relock chain in ONE shell command; chflags itself
prompts), or is one of the guard's own files (exec-policy rules, hooks.json,
this adapter, the gate, the engine pair), or lies in the shared memory store
(written only through the `memory` MCP server; its AI_MEMORY_DIR is read from
the server's [mcp_servers.memory.env] in config.toml).

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
import re
import shlex
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


def memory_dir() -> Path | None:
    """The shared memory store, from the memory MCP server's registration."""
    d = os.environ.get("AI_MEMORY_DIR")
    if not d:
        try:
            import tomllib
            cfg = tomllib.loads((_CODEX_HOME / "config.toml").read_text())
            d = cfg["mcp_servers"]["memory"]["env"]["AI_MEMORY_DIR"]
        except (ImportError, OSError, ValueError, KeyError, TypeError):
            return None
    return _real(Path(os.path.expanduser(d)))


def check_patch(patch: str, cwd: str) -> str | None:
    """Reason to deny the patch, or None."""
    files, dirs = protected_paths()
    mem = memory_dir()
    for p in patch_paths(patch, cwd):
        rp = _real(p)
        if rp in files or any(rp == d or d in rp.parents for d in dirs):
            return (f"apply_patch on guard file {rp} — the harness guards itself; "
                    "edit it by hand")
        if mem is not None and (rp == mem or mem in rp.parents):
            return (f"{rp.name} is in the shared memory store: write it with the memory_* "
                    "tools (memory_create / memory_str_replace / memory_insert / "
                    "memory_supersede; memory_view first for the hash). If they are "
                    "unavailable in this session, say so and give the user the change instead.")
        try:
            flags = getattr(os.lstat(rp), "st_flags", 0)
        except OSError:
            continue  # new file
        if flags & _IMMUTABLE:
            return (f"apply_patch on uchg-locked {rp} — unlock, write and relock in ONE "
                    f"shell command (chflags prompts): chflags nouchg '{rp}' && "
                    f"<write> && chflags uchg '{rp}'")
    return None


# ── interactive sessions (write_stdin) ───────────────────────────────────────
#
# codex can start a TTY session (exec_command) and then type into it with
# write_stdin. Typed lines trigger no hook at all, so a shell started that way
# runs everything unchecked and unlogged (verified on 0.169.0: `bash --norc -i`
# was logged, the echo typed into it was not). Starting such a session is
# therefore an ASK. REPLs (python, node, ...) are not covered.

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash", "mksh"}
_SHELL_OPTS_WITH_ARG = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
_SSH_OPTS_WITH_ARG = {"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L",
                      "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"}
_SSH_NO_SESSION = {"-N", "-V", "-G", "-O", "-Q", "-W"}
_WRAPPERS = {"exec": {"-a"}, "command": set(), "nohup": set(), "time": set(),
             "nice": {"-n"}, "env": {"-u", "-C", "-S"}, "caffeinate": {"-t", "-w"},
             "timeout": {"-s", "-k", "--signal", "--kill-after"},
             "sudo": {"-u", "-g", "-p", "-C", "-U", "-T", "-R", "-h", "-D"},
             "doas": {"-u", "-C"}}
_CONTAINER_CLIS = {"docker", "podman", "nerdctl", "kubectl", "oc"}
_CONTAINER_SUBCMDS = {"exec", "run", "attach", "debug", "start"}
_SEPARATORS = {";", "&", "&&", "|", "||", "|&", ";;", "(", ")"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_MAX_DEPTH = 4


def _segments(command: str) -> list[list[str]] | None:
    """argv per simple command, split on shell operators and newlines."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        toks = list(lex)
    except ValueError:
        return None
    out, cur = [], []
    for t in toks:
        if t in _SEPARATORS or set(t) <= set(";&|()\n"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _base(tok: str) -> str:
    return tok.lstrip("\\").rsplit("/", 1)[-1]


def _unwrap(argv: list[str]) -> tuple[list[str], str | None]:
    """Strip env assignments and exec wrappers. Returns (argv, reason) where a
    reason means the wrapper itself opens an interactive shell (sudo -i, su)."""
    for _ in range(_MAX_DEPTH):
        i = 0
        while i < len(argv) and _ENV_ASSIGN.match(argv[i]):
            i += 1
        argv = argv[i:]
        if not argv:
            return argv, None
        head = _base(argv[0])
        if head == "su":
            if not any(a in ("-c", "--command") or a.startswith("--command=")
                       for a in argv[1:]):
                return argv, "su opens an interactive shell"
            return argv, None
        if head not in _WRAPPERS:
            return argv, None
        takes = _WRAPPERS[head]
        positionals = 1 if head == "timeout" else 0
        i = 1
        while i < len(argv):
            a = argv[i]
            if a in takes:
                i += 2
                continue
            if a.startswith("-") or (head == "env" and _ENV_ASSIGN.match(a)):
                if head == "sudo" and a in ("-i", "-s", "--login", "--shell"):
                    if i + 1 >= len(argv):
                        return argv, f"sudo {a} opens an interactive root shell"
                i += 1
                continue
            if positionals:
                positionals -= 1
                i += 1
                continue
            break
        if i >= len(argv):
            return [], None  # bare wrapper (`sudo -v`, `env`): nothing to open
        argv = argv[i:]
    return argv, None


def _shell_interactive(argv: list[str]) -> tuple[bool, str | None]:
    """(opens a typeable shell?, -c script if any)."""
    i, forced = 1, False
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a in _SHELL_OPTS_WITH_ARG:
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        if a.startswith(("-", "+")) and len(a) > 1:
            flags = a[1:]
            if "c" in flags:
                return False, argv[i + 1] if i + 1 < len(argv) else None
            if "i" in flags or "s" in flags:
                forced = True
            i += 1
            continue
        break
    has_script = i < len(argv)
    return forced or not has_script, None


def _ssh_interactive(argv: list[str]) -> bool:
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _SSH_NO_SESSION:
            return False
        if a in _SSH_OPTS_WITH_ARG:
            i += 2
            continue
        if a.startswith("-"):
            if any(f in a[1:] for f in "NVGQ"):
                return False
            i += 1
            continue
        break
    return i + 1 >= len(argv)  # a host and nothing after it


def _container_interactive(argv: list[str]) -> bool:
    sub = next((a for a in argv[1:] if not a.startswith("-")), "")
    if sub not in _CONTAINER_SUBCMDS:
        return False
    for a in argv[1:]:
        if a == "--":
            break
        if a in ("--interactive", "--stdin", "--attach") or a.startswith(
                ("--interactive=true", "--stdin=true")):
            return True
        if a.startswith("-") and not a.startswith("--") and "i" in a[1:]:
            return True
    return False


def interactive_reason(command: str, depth: int = 0) -> str | None:
    """Why this command opens a session codex could type into, or None."""
    if depth > _MAX_DEPTH or not command:
        return None
    segs = _segments(command)
    if segs is None:
        return None
    for argv in segs:
        argv, why = _unwrap(argv)
        if why:
            return why
        if not argv:
            continue
        head = _base(argv[0])
        if head in _SHELLS:
            interactive, script = _shell_interactive(argv)
            if interactive:
                return f"{head} without a command opens an interactive shell"
            if script:
                nested = interactive_reason(script, depth + 1)
                if nested:
                    return nested
        elif head in ("ssh", "mosh", "autossh") and _ssh_interactive(argv):
            return f"{head} without a remote command opens an interactive remote shell"
        elif head in _CONTAINER_CLIS and _container_interactive(argv):
            return f"{head} with stdin attached opens an interactive session"
        elif head == "script" and not any(a.startswith("-c") or a == "--command"
                                          for a in argv[1:]):
            if len([a for a in argv[1:] if not a.startswith("-")]) <= 1:
                return "script without a command opens an interactive shell"
        elif head == "tmux":
            sub = [a for a in argv[1:] if not a.startswith("-")]
            if any(a in ("send-keys", "send", "paste-buffer", "pasteb") for a in sub):
                return "tmux send-keys types into a session no hook sees"
            if not sub or sub[0] in ("new", "new-session", "attach", "attach-session", "a"):
                return "tmux starts an interactive terminal session"
        elif head in ("screen", "byobu", "zellij"):
            if "stuff" in argv[1:]:
                return f"{head} -X stuff types into a session no hook sees"
            if not any(a in ("-ls", "-list", "ls", "list-sessions") for a in argv[1:]):
                return f"{head} starts an interactive terminal session"
        elif head in ("expect", "unbuffer"):
            return f"{head} drives an interactive program no hook sees"
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

        # A typeable session (write_stdin) escapes every later hook: ASK.
        if decision != "deny":
            why = interactive_reason(command)
            if why:
                why = f"{why}; lines typed into it via write_stdin bypass every hook"
                reason = f"{reason}; {why}" if decision == "ask" and reason else why
                decision = "ask"

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
