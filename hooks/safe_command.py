#!/usr/bin/env python3
"""
Claude Code PreToolUse hook: gates destructive / exfil-prone shell commands.

Version:         2.6.0
Last reviewed:   2026-09-21
Threat model:    LLM with ambient Bedrock/Vault/AWS/GitLab credentials,
                 broad Bash(bash:*), Bash(python3:*), Bash(aws:*), Bash(*CLI:*)
                 wildcards, and Edit/Write(~/**) — this hook is the last
                 line of defense against credential exfil, destructive
                 filesystem ops, macOS persistence, and mutating API calls.
Out of scope:    Raw-network interception (pf, MITM proxy).
                 Burst-rate detection and canary tripwires.
Post-deploy:     chflags uchg ~/.claude/hooks/safe_command.py
                 chflags uchg ~/.claude/hooks/payload_guard.py
                 (BOTH: this file imports the guard, so locking only this one
                 leaves the other half of the boundary writable)
                 (unlock with: chflags nouchg <path>)

Evaluator:
  1. Normalize raw command (collapse \\<LF>, NBSP → space).
  2. Segment on ; && || | newline (and ampersand-background).
  3. For each segment, shlex.split (posix) the raw text. On ValueError,
     fall back to a whitespace split so a single unparseable segment
     cannot short-circuit the others.
  4. Apply DENY rules (catastrophic). Any match → deny.
  5. Apply ASK rules (legitimate-but-risky). First match → ask, unless 6
     clears it.
  6. Explicit ALLOW for a tight pre-approved verb set, but only when every
     other segment of the command is neutral (see ALLOW_PREDICATES).
  7. Otherwise allow silently.

Fail-closed: any uncaught exception → deny("hook internal error").
Audit log:   $AI_SAFE_COMMAND_AUDIT if set (absolute path), else
              ~/.claude/hooks/safe_command_audit.jsonl  (append-only JSONL).
              The default keeps the pre-ai-harness location so the existing
              claude history keeps appending while the codex/opencode
              adapters write the same shared trail.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

# Env-overridable audit trail: AI_SAFE_COMMAND_AUDIT (absolute path) lets the
# codex/opencode adapters — and tests — share or redirect the log without
# touching the default. The default stays the historical ~/.claude location so
# existing claude history keeps appending and every CLI writes one trail.
AUDIT_LOG = Path(os.environ.get("AI_SAFE_COMMAND_AUDIT")
                 or (Path.home() / ".claude" / "hooks" / "safe_command_audit.jsonl"))

# ── Raw-text patterns (evaluated on the normalized full command) ──────────
# These are the patterns that are inherently about raw shell syntax
# (redirects, here-strings, fork-bomb literals) rather than argv structure.

RAW_DENY: list[tuple[re.Pattern, str]] = [
    (re.compile(r">\s*/dev/(r?disk|sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d"
                r"|md\d|md/|loop\d|dm-\d|mapper/)", re.MULTILINE),
     "write to raw disk device — no safe alternative"),
    (re.compile(r"(?:^|[\s;&|])(tee\s+(?:-a\s+)?)?>\s*/etc/(passwd|shadow|sudoers)\b"),
     "overwrite sensitive /etc file — no safe alternative"),
    (re.compile(r"\btee\s+(?:-a\s+)?/etc/(passwd|shadow|sudoers)\b"),
     "tee into sensitive /etc file — no safe alternative"),
    (re.compile(r">>\s*~?/\.?(zshrc|bashrc|zprofile|profile|zshenv|bash_profile)\b"),
     "append to shell rc (persistence) — edit manually and confirm"),
    (re.compile(r">>\s*~?/\.?ssh/(authorized_keys|config)\b"),
     "append to ~/.ssh config (persistence) — edit manually and confirm"),
    (re.compile(r">\s*~?/\.?(bash_history|zsh_history)\b"),
     "wipe shell history (T1070.003) — no safe alternative"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     "fork bomb literal"),
    (re.compile(r"\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     "fork bomb literal (no-space variant)"),
    (re.compile(r"\bHISTFILESIZE\s*=\s*0\b"),
     "HISTFILESIZE=0 (history disable) — no safe alternative"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
     "SQL DROP — no safe alternative"),
    (re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
     "SQL TRUNCATE TABLE — no safe alternative"),
    (re.compile(r"\bbash\s*<\(\s*(curl|wget)\b"),
     "bash <(curl|wget …) — download payload then execute; save & review first"),
    (re.compile(r"\bsource\s*<\(\s*(curl|wget)\b"),
     "source <(curl|wget …) — download payload then execute; save & review first"),
    (re.compile(r"\beval\s+\"?\$\(\s*(curl|wget)\b"),
     "eval $(curl|wget …) — download payload then execute; save & review first"),
    (re.compile(r"\bbase64\s+(-d|--decode)\b.*\|\s*(ba|z)?sh\b"),
     "base64 -d | sh — obfuscated payload execution"),
    (re.compile(r"\|\s*(ba|z)?sh\b"),
     "pipe to shell — save & review first"),
    (re.compile(r"\|\s*(python[23]?|ruby|perl|node|php)\b"),
     "pipe to interpreter — save & review first"),
]

# Sensitive paths for credential exfil (files containing secrets)
SENSITIVE_PATH_RE = re.compile(
    r"(~|\$HOME|/Users/[^/\s]+)/"
    r"(\.aws(/|\b)|\.vault-token\b|\.secrets(/|\b)|\.config/ghost/auth\b|"
    r"\.ssh/id_(?!.*\.pub)|\.netrc\b|\.config/gh\b|\.gnupg(/|\b))|"
    r"\.(pem|key|p12|pfx)\b"
)

# Sensitive directories (broader — used for chmod/chown blast-radius checks)
SENSITIVE_DIR_RE = re.compile(
    r"(~|\$HOME|/Users/[^/\s]+)/"
    r"(\.aws\b|\.vault-token\b|\.secrets\b|\.ssh\b|\.config/ghost/auth\b|"
    r"\.config/gh\b|\.gnupg\b|\.netrc\b|\.claude/hooks\b)"
)

READ_OR_COPY_CMDS = {
    "cat", "less", "more", "head", "tail", "bat",
    "base64", "cp", "mv", "tar", "rsync", "scp", "zip", "gzip", "xz",
}

# Vault: inverted allow list of read-only verb chains. Anything not in this
# set (when argv[0] == "vault") falls into ASK. Catastrophic vault ops (delete,
# kv delete, lease revoke -prefix) are handled by the argv-based DENY layer.
VAULT_READONLY_CHAINS: set[tuple[str, ...]] = {
    ("read",), ("list",), ("status",), ("version",), ("debug",), ("print",),
    ("token", "lookup"), ("token", "capabilities"),
    ("policy", "read"), ("policy", "list"),
    ("kv", "get"), ("kv", "list"), ("kv", "metadata", "get"),
    ("audit", "list"), ("auth", "list"), ("secrets", "list"),
    ("operator", "raft", "list-peers"),
    ("operator", "diagnose"), ("operator", "members"), ("operator", "usage"),
    ("lease", "lookup"),
    ("plugin", "list"), ("plugin", "info"),
    ("namespace", "list"),
    ("pki", "health-check"), ("pki", "list-intermediates"),
    ("login",), ("logout",),  # interactive auth, not mutation of state
    ("help",), ("-h",), ("--help",), ("--version",),
}

# Azure/GCP mutating-verb tokens
AZ_MUTATING_VERBS = {
    "create", "update", "set", "delete", "deploy", "add", "remove",
    "start", "stop", "restart", "scale", "revoke", "rotate",
    "regenerate", "reset", "upload", "import", "invoke", "purge",
    "move", "enable", "disable", "reimage", "redeploy",
}
GCLOUD_MUTATING_VERBS = {
    "create", "update", "delete", "deploy", "apply", "replace", "patch",
    "start", "stop", "restart", "add-iam-policy-binding",
    "remove-iam-policy-binding", "set-iam-policy", "remove", "add",
    "enable", "disable", "import", "upload", "move",
}


# ────────────────────────────────────────────────────────────────────────────
#  I/O helpers
# ────────────────────────────────────────────────────────────────────────────

def _audit(decision: str, reason: str, command: str, session_id: str) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "session_id": session_id,
            "decision": decision,
            "reason": reason,
            "command_snippet": command[:300],
            "pid": os.getpid(),
        }
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        # Best-effort: audit failure must not block the hook decision.
        pass


def _emit(decision: str, reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": f"safe_command: {reason}",
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def deny(reason: str, command: str = "", session_id: str = "") -> None:
    _audit("deny", reason, command, session_id)
    _emit("deny", reason)


def ask(reason: str, command: str = "", session_id: str = "") -> None:
    _audit("ask", reason, command, session_id)
    _emit("ask", reason)


# ────────────────────────────────────────────────────────────────────────────
#  Normalization & segmentation
# ────────────────────────────────────────────────────────────────────────────

_LINE_CONT_RE = re.compile(r"\\\n\s*")


def normalize(command: str) -> str:
    if not command:
        return ""
    # NBSP → space
    cmd = command.replace(" ", " ")
    # backslash-newline continuations
    cmd = _LINE_CONT_RE.sub(" ", cmd)
    return cmd


_SEGMENT_CHARS = {";", "|", "&", "\n"}


def split_segments(command: str) -> list[str]:
    """Split on ; && || | & newline honoring single/double quotes + backslash."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        c = command[i]
        if quote:
            if c == "\\" and i + 1 < n:
                buf.append(c); buf.append(command[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            buf.append(c); i += 1; continue
        if c in ("'", '"'):
            quote = c; buf.append(c); i += 1; continue
        if c == "\\" and i + 1 < n:
            buf.append(c); buf.append(command[i + 1]); i += 2; continue
        if c in _SEGMENT_CHARS:
            seg = "".join(buf).strip()
            if seg:
                out.append(seg)
            buf = []
            while i < n and command[i] in _SEGMENT_CHARS:
                i += 1
            continue
        buf.append(c); i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def tokenize(segment: str) -> list[str]:
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        # Fallback: whitespace split, keep something to match against.
        toks = segment.split()
    return toks


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def strip_env_prefix(argv: list[str]) -> list[str]:
    """Strip `env [KEY=VAL ...]` or bare `KEY=VAL ...` prefix so that
    `env VAR=x rm -rf /` evaluates as `rm -rf /`."""
    if not argv:
        return argv
    i = 0
    if _basename(argv[0]) == "env":
        i = 1
        while i < len(argv) and (
            _ENV_ASSIGN_RE.match(argv[i]) or argv[i] in ("-i", "-u", "--ignore-environment")
        ):
            # -u/-i may take an arg (-u VAR); handle simply
            if argv[i] == "-u" and i + 1 < len(argv):
                i += 2
            else:
                i += 1
    else:
        while i < len(argv) and _ENV_ASSIGN_RE.match(argv[i]):
            i += 1
    return argv[i:] if i < len(argv) else argv


_SUDO_CMDS = {"sudo", "doas"}
_SUDO_OPTS_WITH_ARG = {"-u", "-g", "-p", "-C", "-U", "-T", "-R", "-h"}


def strip_sudo_prefix(argv: list[str]) -> list[str]:
    """`sudo -n dd of=/dev/sda` → `dd of=/dev/sda`, so the underlying command
    is still inspected. (A bare local `sudo` is separately denied upstream.)"""
    if not argv or _basename(argv[0]) not in _SUDO_CMDS:
        return argv
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _SUDO_OPTS_WITH_ARG:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    return argv[i:] if i < len(argv) else argv


# ssh flags that consume the following token, so the host isn't misidentified.
_SSH_OPTS_WITH_ARG = {
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}


def extract_ssh_remote(argv: list[str]) -> str | None:
    """Return the remote command from `ssh [opts] host cmd...`, else None.

    Without this, every destructive command sent to a remote box is invisible
    to the hook, because argv[0] is `ssh`.
    """
    if not argv or _basename(argv[0]) not in ("ssh", "rsh"):
        return None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _SSH_OPTS_WITH_ARG:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break  # argv[i] is the [user@]host
    i += 1  # step past the host
    if i >= len(argv):
        return None  # interactive login, no remote command
    return " ".join(argv[i:])


def _basename(tok: str) -> str:
    """Resolve /bin/rm, /usr/bin/rm, "rm", 'rm', \\rm → rm."""
    t = tok.lstrip("\\")
    if t.startswith(("'", '"')) and len(t) >= 2 and t[-1] == t[0]:
        t = t[1:-1]
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    return t


# ────────────────────────────────────────────────────────────────────────────
#  Argv predicates — DENY layer
# ────────────────────────────────────────────────────────────────────────────

def _has_flag(argv: list[str], *flags: str) -> bool:
    return any(a in flags for a in argv)


def _flag_value(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _touches_sensitive(argv: list[str]) -> bool:
    for a in argv:
        if SENSITIVE_PATH_RE.search(a):
            return True
    return False


def check_rm(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd != "rm":
        return None
    tail = argv[1:]
    if any(a in ("-rf", "-fr", "-Rf", "-fR") for a in tail):
        return "rm -rf — no safe alternative in this workspace"
    flags = "".join(a for a in tail if a.startswith("-") and not a.startswith("--"))
    if ("r" in flags or "R" in flags) and "f" in flags:
        return "rm with combined -r/-R and -f — no safe alternative"
    if any(a == "--recursive" for a in tail) and any(a == "--force" for a in tail):
        return "rm --recursive --force — no safe alternative"
    if any(a in ("--recursive", "-r", "-R") for a in tail) and any(
        a in ("--force", "-f") for a in tail
    ):
        return "rm recursive + force — no safe alternative"
    return None


def check_find_delete(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "find":
        if "-delete" in argv:
            return "find -delete — preview with -print first"
        # find ... -exec rm ... or -execdir rm ...
        for i, a in enumerate(argv):
            if a in ("-exec", "-execdir") and i + 1 < len(argv):
                if _basename(argv[i + 1]) == "rm":
                    return "find -exec rm — no safe alternative"
    return None


def check_xargs_rm(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "xargs":
        for a in argv[1:]:
            if _basename(a) == "rm":
                return "xargs rm — no safe alternative"
    return None


# Block-device namespaces that must never be a write target. The original
# pattern only knew sd*/hd*/disk*, which silently allowed SD cards (mmcblk*),
# RAID arrays (md*), NVMe, LVM and loop devices.
_BLOCK_DEV_RE = re.compile(
    r"^/dev/("
    r"r?disk\d+.*"                    # macOS: disk0, disk2s1, rdisk0
    r"|(sd|hd|vd|xvd)[a-z]+\d*"       # sda, sda1, vdb, xvda1
    r"|nvme\d+n\d+(p\d+)?"            # nvme0n1, nvme0n1p2
    r"|mmcblk\d+(p\d+)?"              # mmcblk0, mmcblk0p2  ← SD cards
    r"|md\d+(p\d+)?|md/.+"            # md125, md125p1      ← RAID arrays
    r"|loop\d+(p\d+)?"
    r"|dm-\d+|mapper/.+"              # LVM / device-mapper
    r"|sr\d+|fd\d+"
    r")$"
)

# /dev targets that are legitimate write destinations.
_SAFE_DEV_WRITE = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}

# Whole-command destructive disk utilities (no read-only mode worth allowing).
_DISK_DESTRUCTIVE_CMDS = {
    "wipefs", "shred", "blkdiscard", "zerofree", "mkswap", "dban", "nwipe",
}

# Partition editors: destructive by default, but each has a read-only form
# worth keeping usable (fdisk -l and friends are everyday diagnostics).
_PART_TOOL_READONLY_FLAGS = {
    "fdisk": {"-l", "--list", "-x"},
    "sfdisk": {"-l", "--list", "-d", "--dump", "-s", "--show-size",
               "-V", "--verify", "-J", "--json"},
    "sgdisk": {"-p", "--print", "-i", "--info", "-v", "--verify"},
    "gdisk": {"-l"},
    "cgdisk": set(),
    "cfdisk": set(),
    "parted": {"-l", "--list", "print"},
    "partx": {"-s", "--show", "-l", "--list"},
}


def _dev_write_target(path: str) -> str | None:
    """Return a reason if `path` is an unsafe device write target."""
    p = path.strip().strip("'\"")
    if not p.startswith("/dev/"):
        return None
    if p in _SAFE_DEV_WRITE or p.startswith("/dev/fd/"):
        return None
    if _BLOCK_DEV_RE.match(p):
        return f"block device {p}"
    # Unknown /dev/* target — fail closed rather than guess.
    return f"device {p}"


def check_disk_tools(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])

    # mkfs.ext4, mkfs.xfs etc all count
    if cmd == "mkfs" or cmd.startswith("mkfs.") or cmd in _DISK_DESTRUCTIVE_CMDS:
        return f"{cmd} — destructive disk operation; no safe alternative"

    if cmd == "dd":
        for a in argv[1:]:
            if re.match(r"if=/dev/(zero|urandom|random)\b", a):
                return "dd with zero/random source — no safe alternative"
            if a.startswith("of="):
                target = _dev_write_target(a[3:])
                if target:
                    return f"dd targeting {target} — no safe alternative"

    if cmd in _PART_TOOL_READONLY_FLAGS:
        ro = _PART_TOOL_READONLY_FLAGS[cmd]
        if not any(a in ro for a in argv[1:]):
            return f"{cmd} — partition table edit; no safe alternative"

    if cmd == "mdadm":
        destructive = {
            "--zero-superblock", "--create", "-C", "--stop", "-S",
            "--fail", "-f", "--remove", "-r", "--kill-subarray", "--force",
        }
        for a in argv[1:]:
            if a in destructive:
                return f"mdadm {a} — RAID-destructive; no safe alternative"

    if cmd == "badblocks" and _has_flag(argv, "-w", "--write-mode"):
        return "badblocks -w — destructive write test; no safe alternative"

    if cmd == "hdparm":
        for a in argv[1:]:
            if a.startswith("--security-erase") or a in (
                "--trim-sector-ranges", "--make-bad-sector", "--dco-restore"
            ):
                return f"hdparm {a} — destructive; no safe alternative"

    if cmd == "nvme" and len(argv) > 1 and argv[1] in ("format", "sanitize"):
        return f"nvme {argv[1]} — destructive; no safe alternative"

    if cmd == "cryptsetup":
        for a in argv[1:]:
            if a in ("luksFormat", "erase", "luksErase"):
                return f"cryptsetup {a} — destroys LUKS data; no safe alternative"

    if cmd in ("pvcreate", "pvremove", "vgremove", "lvremove", "vgreduce"):
        return f"{cmd} — LVM-destructive; no safe alternative"

    return None


def check_mv_cp_to_nullish(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd in ("mv", "cp"):
        for a in argv[1:]:
            if a == "/dev/null" or a.rstrip("/").endswith("/.Trash"):
                return f"{cmd} to /dev/null or Trash — no safe alternative"
    return None


def check_truncate(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "truncate":
        size = _flag_value(argv, "-s") or _flag_value(argv, "--size")
        if size in ("0", "+0", "-0"):
            if _touches_sensitive(argv):
                return "truncate -s 0 on sensitive path — no safe alternative"
    return None


def check_chmod_chown(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd not in ("chmod", "chown"):
        return None
    danger_targets = {"/", "~", "$HOME"}
    for a in argv[1:]:
        if a in danger_targets:
            return f"{cmd} on filesystem root or $HOME — no safe alternative"
        if cmd == "chmod" and (
            SENSITIVE_PATH_RE.search(a) or SENSITIVE_DIR_RE.search(a)
        ) and any(x in argv for x in ("777", "-R", "--recursive")):
            return "chmod on sensitive path — no safe alternative"
    return None


def check_mkdir_root(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "mkdir":
        if "-p" in argv and "/" in argv[argv.index("-p") + 1:]:
            return "mkdir -p / — no safe alternative"
    return None


def check_security_cli(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "security":
        if len(argv) > 1:
            sub = argv[1]
            if re.match(r"(find|dump|export|delete)(-|$)", sub):
                return (
                    "macOS security CLI (keychain access) — "
                    "unlock items manually via Keychain Access.app"
                )
    return None


def check_cred_read(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd in READ_OR_COPY_CMDS and _touches_sensitive(argv):
        return (
            f"{cmd} on credential/secret path — "
            "read manually if strictly needed"
        )
    return None


def check_token_print(argv: list[str]) -> str | None:
    if not argv:
        return None
    raw = " ".join(argv)
    if re.search(r"\bgh\s+auth\s+token\b", raw):
        return "gh auth token (prints PAT) — scope the token at the API call instead"
    if re.search(r"\bglab\s+config\s+get\s+token\b", raw):
        return "glab config get token (prints PAT) — scope at the API call instead"
    if argv[0] == "vault" and len(argv) >= 3 and argv[1] == "token" and argv[2] == "lookup":
        # Look-up is fine; it's explicitly read-only.
        return None
    if argv[0] == "vault" and len(argv) >= 3 and argv[1] == "read" and any(
        "secret" in a.lower() or "creds" in a.lower() for a in argv[2:]
    ):
        return "vault read secret/creds — exfil risk; request explicit approval"
    return None


def check_curl_exfil(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "curl":
        return None
    for i, a in enumerate(argv[1:], start=1):
        if a in ("-d", "--data", "--data-binary", "--data-raw", "--data-urlencode"):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt.startswith("@"):
                return "curl POST with @file payload — exfil vector, no safe alternative"
        if a in ("-F", "--form"):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if "@" in nxt:
                after_at = nxt.split("@", 1)[1]
                if SENSITIVE_PATH_RE.search(after_at) or after_at.startswith(
                    ("/etc/", "/var/")
                ):
                    return "curl multipart upload of sensitive file — exfil vector"
        if a in ("-T", "--upload-file") or a.startswith("--upload-file="):
            nxt = argv[i + 1] if a in ("-T", "--upload-file") and i + 1 < len(argv) else (
                a.split("=", 1)[1] if "=" in a else ""
            )
            if SENSITIVE_PATH_RE.search(nxt) or nxt.startswith(("/etc/", "/var/")):
                return "curl upload of sensitive file — exfil vector"
    return None


def check_netcat(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd in ("nc", "ncat", "socat"):
        for a in argv[1:]:
            if re.match(r"^\d{1,5}$", a):
                return f"{cmd} with numeric port — raw-socket exfil channel"
            # socat TCP:host:port, TCP4:host:port, UDP:host:port
            if re.match(r"^(TCP4?|UDP4?|SSL|OPENSSL):[^:]+:\d+$", a, re.IGNORECASE):
                return f"{cmd} TCP/UDP endpoint — raw-socket exfil channel"
    return None


def check_scp_external(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "scp":
        for a in argv[1:]:
            m = re.match(r".+@([^:]+):", a)
            if m:
                host = m.group(1)
                if host not in ("localhost", "127.0.0.1", "::1"):
                    return f"scp to external host {host} — exfil channel"
    return None


def check_dig_exfil(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "dig":
        for a in argv[1:]:
            if "$(" in a:
                return "dig with command-substitution in name — DNS exfil vector"
    return None


def check_shell_c(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd in ("bash", "sh", "zsh") and "-c" in argv:
        idx = argv.index("-c")
        payload = argv[idx + 1] if idx + 1 < len(argv) else ""
        bad = re.search(r"\b(rm\s+-|cat\s+/etc|base64|curl|wget|nc\b)", payload)
        if bad:
            return f"{cmd} -c with destructive/exfil payload — run explicitly"
    return None


def check_interpreter_code(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd not in ("python", "python2", "python3", "perl", "ruby", "node"):
        return None
    for flag in ("-c", "-e"):
        if flag in argv:
            idx = argv.index(flag)
            payload = argv[idx + 1] if idx + 1 < len(argv) else ""
            if re.search(
                r"(os\.system|subprocess|__import__|exec\(|child_process|"
                r"socket\.|use\s+Socket|require\s+['\"]socket)",
                payload,
            ):
                return f"{cmd} {flag} with syscall/socket payload — run explicitly"
    return None


def check_persistence(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd == "sudo":
        return "sudo — run manually outside the hook session"
    if cmd == "launchctl" and len(argv) > 1 and argv[1] in (
        "load", "bootstrap", "unload", "disable",
    ):
        return f"launchctl {argv[1]} (persistence) — no safe alternative"
    if cmd == "diskutil" and len(argv) > 1 and argv[1] in (
        "eraseDisk", "zeroDisk", "secureErase", "reformat",
    ):
        return f"diskutil {argv[1]} — no safe alternative"
    if cmd == "osascript":
        return "osascript — run manually and review"
    if cmd == "defaults" and len(argv) > 1 and argv[1] == "write":
        return "defaults write (system preferences persistence) — run manually"
    if cmd == "spctl" and "--master-disable" in argv:
        return "spctl --master-disable — disables Gatekeeper"
    if cmd == "csrutil" and "disable" in argv:
        return "csrutil disable — disables SIP"
    if cmd == "nvram" and "-c" in argv:
        return "nvram -c — clears NVRAM"
    if cmd == "ssh-copy-id":
        return "ssh-copy-id — copies a key to a remote; run manually"
    if cmd == "crontab" and any(a in ("-e", "-l", "-r") for a in argv[1:]):
        return "crontab edit/list/remove — persistence; run manually"
    if cmd == "ssh-keygen":
        return "ssh-keygen — key material change; run manually"
    if cmd == "brew" and len(argv) > 1 and argv[1] in ("install", "reinstall") or (
        cmd == "brew" and len(argv) > 2 and argv[1] == "services" and argv[2] == "start"
    ):
        return "brew install/services start — persistence risk; install manually"
    if cmd == "history" and "-c" in argv:
        return "history -c — clears shell history"
    if cmd == "unset" and "HISTFILE" in argv:
        return "unset HISTFILE — disables history"
    if cmd == "visudo":
        return "visudo — sudoers edit; run manually"
    return None


def check_git_destructive(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "git":
        return None
    sub = argv[1] if len(argv) > 1 else ""
    tail = argv[2:]
    if sub == "push":
        if any(a in ("--force", "-f") for a in tail):
            return "git push --force — rewrites upstream; no safe alternative"
        if any(a == "--force-with-lease" or a.startswith("--force-with-lease=") for a in tail):
            return "git push --force-with-lease — still rewrites history; no safe alternative"
        if "--mirror" in tail:
            return "git push --mirror — rewrites all refs; no safe alternative"
    if sub == "update-ref" and "-d" in tail:
        return "git update-ref -d — drops refs; no safe alternative"
    if sub == "reflog" and tail and tail[0] == "expire":
        return "git reflog expire — destroys recovery history"
    if sub == "filter-branch":
        return "git filter-branch — rewrites history"
    if sub == "remote" and tail and tail[0] == "set-url":
        return "git remote set-url — can redirect pushes; run manually"
    if sub == "stash" and tail and tail[0] in ("drop", "clear"):
        return f"git stash {tail[0]} — destroys stashed work"
    if sub == "checkout" and "--" in tail:
        idx = tail.index("--")
        if idx + 1 < len(tail) and tail[idx + 1] == ".":
            return "git checkout -- . — discards all working-tree changes"
    return None


def check_shutdown(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd in ("shutdown", "reboot", "halt", "poweroff"):
        return f"{cmd} — no safe alternative"
    if cmd == "init" and len(argv) > 1 and argv[1] in ("0", "6"):
        return f"init {argv[1]} — no safe alternative"
    if cmd == "kill":
        sig = None
        tail = argv[1:]
        for a in tail:
            if a in ("-9", "-SIGKILL", "-KILL"):
                sig = a
            if sig and a == "-1":
                return "kill SIGKILL -1 — kills all user processes"
    if cmd == "pkill":
        if any(a in ("-9", "-SIGKILL", "-KILL") for a in argv[1:]):
            return "pkill with SIGKILL — no safe alternative (use -TERM)"
    if cmd == "ulimit" and "-n" in argv and "unlimited" in argv:
        return "ulimit -n unlimited — resource exhaustion risk"
    return None


def check_systemctl(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "systemctl":
        if any(a in ("disable", "mask") for a in argv[1:]):
            return "systemctl disable/mask — service persistence; run manually"
    return None


def check_infra_deny(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    raw = " ".join(argv)
    if cmd == "terraform":
        if "destroy" in argv[1:2]:
            return "terraform destroy — no safe alternative"
        if "apply" in argv[1:2] and "-auto-approve" in argv[2:]:
            return "terraform apply -auto-approve — review the plan first"
    if cmd == "kubectl" and "delete" in argv[1:2]:
        tail = argv[2:]
        if any(a in ("namespace", "ns", "-A", "--all", "--all-namespaces") for a in tail):
            return "kubectl delete namespace/-A/--all — no safe alternative"
    if cmd == "aws":
        if len(argv) >= 3 and argv[1] == "iam" and re.match(
            r"(delete|detach|remove)-", argv[2]
        ):
            return f"aws iam {argv[2]} — no safe alternative"
        if len(argv) >= 3 and argv[1] == "iam" and argv[2] == "put-role-policy":
            return "aws iam put-role-policy — policy change; run manually"
        if re.search(r"\baws\s+s3\s+rm\b.*--recursive", raw):
            return "aws s3 rm --recursive — no safe alternative"
        if len(argv) >= 3 and argv[1] == "s3api" and argv[2] == "delete-bucket":
            return "aws s3api delete-bucket — no safe alternative"
        if len(argv) >= 3 and argv[1] == "ec2" and argv[2] == "terminate-instances":
            return "aws ec2 terminate-instances — no safe alternative"
    if cmd == "vault":
        sub = argv[1:]
        if sub[:1] == ["delete"]:
            return "vault delete — no safe alternative"
        if sub[:2] == ["kv", "delete"] or sub[:2] == ["kv", "destroy"]:
            return f"vault kv {sub[1]} — no safe alternative"
        if sub[:2] == ["lease", "revoke"] and "-prefix" in sub:
            return "vault lease revoke -prefix — mass-revocation, no safe alternative"
    if cmd == "helm" and "uninstall" in argv[1:2]:
        return "helm uninstall — no safe alternative"
    if cmd == "docker":
        if argv[1:2] == ["system"] and len(argv) > 2 and argv[2] == "prune":
            tail_flags = argv[3:]
            has_af = any(a in ("-a", "-f", "--all", "--force") for a in tail_flags)
            # combined short flags like -af, -fa
            has_combined = any(
                a.startswith("-") and not a.startswith("--") and len(a) > 2
                and set(a[1:]) & {"a", "f"} for a in tail_flags
            )
            if has_af or has_combined:
                return "docker system prune -a/-f — no safe alternative"
        if argv[1:2] == ["run"]:
            if "--privileged" in argv[2:]:
                return "docker run --privileged — container escape risk"
            for i, a in enumerate(argv[2:], start=2):
                if a == "--cap-add" and i + 1 < len(argv) and argv[i + 1] == "SYS_ADMIN":
                    return "docker run --cap-add SYS_ADMIN — container escape risk"
                if a.startswith("--cap-add=") and "SYS_ADMIN" in a:
                    return "docker run --cap-add=SYS_ADMIN — container escape risk"
        if argv[1:2] == ["exec"] and "--privileged" in argv[2:]:
            return "docker exec --privileged — container escape risk"
    if cmd == "nsenter":
        return "nsenter — namespace escape; run manually"
    return None


def _gh_api_method(argv: list[str]) -> str | None:
    """Method for `gh api` / `glab api`. gh defaults to GET, or POST when a
    field flag is present; -X/--method wins."""
    explicit = None
    has_field = False
    for i, a in enumerate(argv):
        if a in ("-X", "--method"):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            explicit = nxt.upper()
        elif a.startswith("--method="):
            explicit = a.split("=", 1)[1].upper()
        elif a in ("-f", "--field", "-F", "--raw-field", "--input"):
            has_field = True
        elif a.startswith(("-f=", "--field=", "-F=", "--raw-field=", "--input=")):
            has_field = True
    if explicit:
        return explicit
    return "POST" if has_field else "GET"


# `gh api` / `glab api` flags that consume the following token, so it is not
# the endpoint.
_API_VALUE_FLAGS = {
    "-X", "--method", "-f", "--field", "-F", "--raw-field", "--input",
    "-H", "--header", "--hostname",
}

# Merge-request comments and review threads: post a note, edit a note, resolve
# a discussion. Same blast radius as `glab mr create`, already pre-approved.
_MR_NOTE_ENDPOINT_RE = re.compile(
    r"^/?(api/v4/)?projects/[^/]+/merge_requests/\d+/"
    r"(notes(/\d+)?|discussions(/[^/]+(/notes(/\d+)?)?)?)/?$"
)

# The raw-API equivalents of `glab mr create` and `glab mr update`, which are
# already pre-approved as subcommands. Doing the same thing through `glab api`
# used to prompt while the subcommand did not — an asymmetry with no security
# meaning, since the blast radius is identical.
#   POST projects/X/merge_requests        → create
#   PUT  projects/X/merge_requests/<iid>  → update (title, description, assignee)
# Both are anchored with `$` so ACTION subpaths never match: /merge, /approve,
# /rebase, /close stay in the ASK layer, matching `glab mr merge` and friends.
_MR_CREATE_ENDPOINT_RE = re.compile(
    r"^/?(api/v4/)?projects/[^/]+/merge_requests/?$"
)
_MR_UPDATE_ENDPOINT_RE = re.compile(
    r"^/?(api/v4/)?projects/[^/]+/merge_requests/\d+/?$"
)


def _api_endpoint(tail: list[str]) -> str:
    """First positional arg of `gh api` / `glab api` — the endpoint path."""
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok.startswith("-"):
            i += 2 if tok in _API_VALUE_FLAGS else 1
            continue
        return tok
    return ""


def _is_mr_write_endpoint(tail: list[str], method: str | None) -> bool:
    """True for the MR writes that `glab mr create/update` already pre-approve:
    creating an MR, updating one, and posting/editing notes and discussions."""
    if method not in ("POST", "PUT"):
        return False
    endpoint = _api_endpoint(tail).split("?", 1)[0]
    if _MR_NOTE_ENDPOINT_RE.match(endpoint):
        return True
    if method == "POST":
        return bool(_MR_CREATE_ENDPOINT_RE.match(endpoint))
    return bool(_MR_UPDATE_ENDPOINT_RE.match(endpoint))


def check_gh_glab_deny(argv: list[str]) -> str | None:
    """Catastrophic gh/glab ops: repo delete, and flipping a repo to public."""
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd not in ("gh", "glab"):
        return None
    tail = argv[1:]
    if tail[:2] == ["repo", "delete"]:
        return f"{cmd} repo delete — irreversible; run manually and confirm"
    if tail[:2] == ["repo", "edit"]:
        vis = _flag_value(tail, "--visibility")
        if vis is None and "--visibility" in tail:
            # value in a later position already handled by _flag_value; also
            # catch bare `--visibility public` split oddly
            idx = tail.index("--visibility")
            vis = tail[idx + 1] if idx + 1 < len(tail) else None
        if vis == "public":
            return f"{cmd} repo edit --visibility public — exposes a private repo; run manually"
    return None


DENY_PREDICATES = [
    check_gh_glab_deny,
    check_rm,
    check_find_delete,
    check_xargs_rm,
    check_disk_tools,
    check_mv_cp_to_nullish,
    check_truncate,
    check_chmod_chown,
    check_mkdir_root,
    check_security_cli,
    check_cred_read,
    check_token_print,
    check_curl_exfil,
    check_netcat,
    check_scp_external,
    check_dig_exfil,
    check_shell_c,
    check_interpreter_code,
    check_persistence,
    check_git_destructive,
    check_shutdown,
    check_systemctl,
    check_infra_deny,
]


# ────────────────────────────────────────────────────────────────────────────
#  Argv predicates — ASK layer
# ────────────────────────────────────────────────────────────────────────────

def ask_git(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "git":
        return None
    sub = argv[1] if len(argv) > 1 else ""
    tail = argv[2:]
    if sub == "rebase":
        return "git rebase — confirm target is not shared; prefer merge if unsure"
    if sub == "reset" and "--hard" in tail:
        return "git reset --hard — stash unstaged work first"
    if sub == "clean":
        flags = "".join(a.lstrip("-") for a in tail if a.startswith("-"))
        if "f" in flags and ("d" in flags or "x" in flags):
            return "git clean -fd — preview with `git clean -ndx` first"
    return None


def ask_kill(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "killall":
        return "killall — prefer `pkill -TERM <name>` or pid-targeted kill"
    return None


def ask_chflags(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "chflags":
        return f"chflags {' '.join(argv[1:])[:80]} — file-flag change (e.g. uchg lock); confirm"
    return None


def ask_install(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd == "pip" or cmd == "pip3" or cmd == "pipx":
        if len(argv) > 1 and argv[1] == "install":
            return f"{cmd} install — pin version; verify registry"
    if cmd == "npm" and len(argv) > 1 and argv[1] in ("install", "i"):
        return "npm install — prefer `npm ci`; verify registry"
    if cmd == "go" and len(argv) > 2 and argv[1] == "install":
        if any("@latest" in a for a in argv[2:]):
            return "go install …@latest — pin a version"
    if cmd in ("gem", "cargo") and len(argv) > 1 and argv[1] == "install":
        return f"{cmd} install — pin version; verify registry"
    return None


def _http_method_from_curl(argv: list[str]) -> str | None:
    explicit = None   # method forced by -X/--request
    implied = None     # method inferred from a body/upload flag
    force_get = False  # -G/--get moves any data into the query string → GET
    for i, a in enumerate(argv[1:], start=1):
        if a in ("-G", "--get"):
            force_get = True
        elif a == "-X" or a == "--request":
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt.upper() in ("POST", "PUT", "PATCH", "DELETE"):
                explicit = nxt.upper()
        elif a.startswith("--request="):
            v = a.split("=", 1)[1].upper()
            if v in ("POST", "PUT", "PATCH", "DELETE"):
                explicit = v
        elif a in ("-d", "--data", "--data-raw", "--data-binary",
                   "--data-urlencode", "-F", "--form") and implied is None:
            implied = "POST"
        elif (a in ("-T", "--upload-file") or a.startswith("--upload-file=")) \
                and implied is None:
            implied = "PUT"
    # An explicit -X always wins (e.g. `-X POST -G` really is a POST).
    if explicit:
        return explicit
    # `-G` with only body flags is a GET: curl appends the data to the URL.
    if force_get:
        return None
    return implied


def _find_url(argv: list[str]) -> str:
    for a in argv[1:]:
        if re.match(r"https?://", a):
            return a
    return "?"


# Read-only API endpoints that are safe to POST to without a prompt
# (search/jql, wiki search, etc. — the POST body carries a query, not a
# mutation). No endpoints ship built-in so nothing internal leaks by default.
# Two sources, unioned: the CC_CURL_POST_ALLOW env var and a gitignored file
# beside this hook — `.curl-post-allow.local`, overridable with
# CC_CURL_POST_ALLOW_FILE. One URL prefix per line, `#` starts a comment.
# Same arrangement as populate.sh's .leak-patterns.local: site-specific values
# stay out of this repo, and out of settings.json — which local credential
# tooling may rewrite wholesale, taking anything parked there with it.
# Matched as a prefix against the curl URL.
CURL_POST_ALLOW_FILE = Path(
    os.environ.get("CC_CURL_POST_ALLOW_FILE")
    or Path(__file__).resolve().parent / ".curl-post-allow.local"
)


def _load_curl_post_allow() -> tuple[str, ...]:
    blobs = [os.environ.get("CC_CURL_POST_ALLOW", "")]
    try:
        blobs.append(CURL_POST_ALLOW_FILE.read_text())
    except OSError:
        pass  # no local file — env var only, or nothing at all
    prefixes: list[str] = []
    for blob in blobs:
        for line in blob.splitlines():
            # Only a leading `#` is a comment: a URL may legitimately carry a
            # fragment, and truncating it would silently widen the prefix.
            line = line.strip()
            if not line or line.startswith("#") or line in prefixes:
                continue
            prefixes.append(line)
    return tuple(prefixes)


CURL_POST_ALLOW_PREFIXES: tuple[str, ...] = _load_curl_post_allow()


# The local dashboard's refresh endpoint, on whatever port it is bound to. The
# allow list above is prefix-matched, which cannot express a port wildcard, so
# this one is a pattern. Both halves are required — the host must be loopback
# AND the path must be /api/refresh — so it cannot drift into "any POST to
# localhost" or "any /api/refresh anywhere".
LOCAL_REFRESH_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?/api/refresh(/|\?|$)", re.I)


def ask_curl(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "curl":
        return None
    method = _http_method_from_curl(argv)
    if method:
        url = _find_url(argv)
        if any(url.startswith(p) for p in CURL_POST_ALLOW_PREFIXES):
            return None  # harmless read-only query endpoint
        if LOCAL_REFRESH_RE.match(url):
            return None  # local dashboard refresh; nothing leaves the machine
        return f"curl {method} {url} — confirm target + payload"
    return None


def ask_httpie(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd not in ("http", "https", "xh", "xhs"):
        return None
    # httpie: first non-flag token may be METHOD; next is URL.
    positionals = [a for a in argv[1:] if not a.startswith("-")]
    if not positionals:
        return None
    first = positionals[0].upper()
    if first in ("POST", "PUT", "PATCH", "DELETE"):
        url = positionals[1] if len(positionals) > 1 else "?"
        return f"httpie {first} {url} — confirm target + payload"
    return None


def ask_wget(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "wget":
        return None
    for a in argv[1:]:
        m = re.match(r"--method=(POST|PUT|PATCH|DELETE)$", a)
        if m:
            return f"wget --method={m.group(1)} — confirm target + payload"
        if a in ("--post-data", "--post-file", "--body-data", "--body-file"):
            return "wget with body payload — confirm target + payload"
        if a.startswith(("--post-data=", "--post-file=",
                         "--body-data=", "--body-file=")):
            return "wget with body payload — confirm target + payload"
    return None


def ask_aws_upload(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "aws":
        return None
    if len(argv) < 4:
        return None
    if argv[1] == "s3" and argv[2] in ("cp", "sync"):
        # positionals after the verb
        pos = [a for a in argv[3:] if not a.startswith("-")]
        if len(pos) >= 2:
            src, dst = pos[0], pos[1]
            if not src.startswith("s3://") and dst.startswith("s3://"):
                return f"aws s3 {argv[2]} upload to {dst} — confirm target"
    if argv[1] == "s3api" and argv[2] == "put-object":
        return "aws s3api put-object — confirm bucket + key"
    return None


def ask_kubectl(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "kubectl":
        return None
    sub = argv[1] if len(argv) > 1 else ""
    if sub in ("apply", "create", "patch", "replace", "scale"):
        return f"kubectl {sub} — confirm context + namespace"
    if sub == "rollout" and len(argv) > 2 and argv[2] in (
        "restart", "pause", "resume", "undo"
    ):
        return f"kubectl rollout {argv[2]} — confirm context + workload"
    return None


def ask_helm(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "helm" and len(argv) > 1:
        if argv[1] in ("install", "upgrade"):
            return f"helm {argv[1]} — confirm release + values"
    return None


def ask_terraform(argv: list[str]) -> str | None:
    if argv and _basename(argv[0]) == "terraform" and len(argv) > 1:
        if argv[1] == "apply" and "-auto-approve" not in argv[2:]:
            return "terraform apply — confirm the plan"
    return None


def ask_az(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "az":
        return None
    for a in argv[1:]:
        if a in AZ_MUTATING_VERBS:
            return f"az {' '.join(argv[1:])[:80]} — confirm subscription + target"
    return None


def ask_gcloud(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd == "gcloud":
        for a in argv[1:]:
            if a in GCLOUD_MUTATING_VERBS:
                return f"gcloud {' '.join(argv[1:])[:80]} — confirm project + target"
    if cmd == "gsutil":
        sub = argv[1] if len(argv) > 1 else ""
        if sub == "rm":
            return "gsutil rm — confirm bucket + path"
        if sub in ("cp", "rsync"):
            pos = [a for a in argv[2:] if not a.startswith("-")]
            if len(pos) >= 2:
                src, dst = pos[0], pos[1]
                if not src.startswith("gs://") and dst.startswith("gs://"):
                    return f"gsutil {sub} upload to {dst} — confirm target"
    return None


def ask_vault(argv: list[str]) -> str | None:
    if not argv or _basename(argv[0]) != "vault":
        return None
    sub = tuple(a for a in argv[1:] if not a.startswith("-"))
    if not sub:
        return None
    for prefix_len in (3, 2, 1):
        if sub[:prefix_len] in VAULT_READONLY_CHAINS:
            return None
    return f"vault {' '.join(sub[:3])[:80]} — non-read-only verb; confirm"


def ask_gh_glab(argv: list[str]) -> str | None:
    """gh/glab mutations not already denied: non-repo-delete/visibility
    deletes, secret/variable writes, and mutating `api` calls."""
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd not in ("gh", "glab"):
        return None
    tail = argv[1:]
    if not tail:
        return None
    grp = tail[0]
    sub = tail[1] if len(tail) > 1 else ""

    # api calls: prompt on any mutating method.
    if grp == "api":
        method = _gh_api_method(tail[1:])
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            if cmd == "glab" and _is_mr_write_endpoint(tail[1:], method):
                return None  # MR create/update/notes — pre-approved below
            return f"{cmd} api {method} — mutating API call; confirm endpoint"
        return None

    # secret / variable writes.
    if grp in ("secret", "variable") and sub in ("set", "delete", "remove"):
        return f"{cmd} {grp} {sub} — writes/removes a secret; confirm target"

    # generic mutating verbs across gh/glab noun-verb groups
    # (repo delete + repo edit --visibility public are DENIED upstream).
    mutating = {
        "delete", "remove", "create", "edit", "rename", "transfer",
        "archive", "unarchive", "set", "add", "sync", "merge", "close",
        "lock", "unlock", "protect", "unprotect", "disable", "enable",
    }
    if sub in mutating:
        return f"{cmd} {grp} {sub} — mutating operation; confirm target"
    # single-token mutating verbs (e.g. `glab mr merge`, `gh pr merge`)
    if grp in ("merge", "delete", "close", "rebase") and cmd in ("gh", "glab"):
        return f"{cmd} {grp} — mutating operation; confirm target"
    return None


# acli grammar is `acli <product> <noun…> <verb> [flags]`. These nouns share a
# name with a mutating verb but only group subcommands — `comment list` is a
# read, `comment create` is a write — so the verb that follows them decides.
ACLI_NOUN_GROUPS = {"comment", "link", "attachment", "watcher"}
ACLI_READONLY_VERBS = {
    "list", "view", "search", "get", "show", "status",
    "visibility", "type", "list-watchers",
}


def ask_acli(argv: list[str]) -> str | None:
    """Atlassian acli (jira/confluence): prompt on mutating subcommands."""
    if not argv or _basename(argv[0]) != "acli":
        return None
    mutating = {
        "create", "update", "edit", "delete", "remove", "assign",
        "transition", "add", "set", "move", "clone",
        "archive", "publish", "upload", "import",
    }
    reason = f"acli {' '.join(argv[1:])[:80]} — mutating Atlassian op; confirm"
    positional = [a for a in argv[1:] if not a.startswith("-")]
    for i, a in enumerate(positional):
        if a in ACLI_NOUN_GROUPS:
            nxt = positional[i + 1] if i + 1 < len(positional) else ""
            if nxt in ACLI_READONLY_VERBS:
                continue
            return reason  # bare group, or a verb we do not know to be a read
        if a in mutating:
            return reason
    return None


# ────────────────────────────────────────────────────────────────────────────
#  Argv predicates — explicit ALLOW layer
#
#  An explicit "allow" emitted by this PreToolUse hook is authoritative: it
#  short-circuits both this hook's own ASK layer AND the downstream auto-mode
#  classifier (which otherwise prompts on outward-facing ops like `git push`
#  and `glab mr create`). It runs AFTER the DENY layer, so destructive forms
#  (git push --force/--force-with-lease/--mirror, etc.) are already blocked and
#  never reach here. Keep this list tight — only genuinely pre-approved verbs.
# ────────────────────────────────────────────────────────────────────────────

def allow_git_glab_mr(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _basename(argv[0])
    if cmd == "git":
        # Destructive push forms are caught by check_git_destructive (DENY)
        # before we get here, so any git push reaching this point is safe.
        if len(argv) > 1 and argv[1] == "push":
            return "git push (non-destructive) — pre-approved"
    if cmd == "glab":
        tail = argv[1:]
        # `mr note` is the CLI spelling of the pre-approved API call
        # (POST …/merge_requests/<iid>/notes, see allow_glab_mr_api). Without it
        # the two spellings of one action behaved differently: the API form was
        # authoritative-allow, while the CLI form fell through to the silent
        # default and still met the auto-mode classifier.
        if tail[:2] in (["mr", "create"], ["mr", "update"], ["mr", "note"]):
            return f"glab mr {tail[1]} — pre-approved"
    return None


def allow_glab_mr_api(argv: list[str]) -> str | None:
    """`glab api` doing what `glab mr create/update` already do: create an MR,
    update one, post or edit a note, resolve a thread. DELETE, action subpaths
    (/merge, /approve, /rebase) and every other endpoint stay in the ASK layer."""
    if not argv or _basename(argv[0]) != "glab":
        return None
    tail = argv[1:]
    if tail[:1] != ["api"]:
        return None
    method = _gh_api_method(tail[1:])
    if not _is_mr_write_endpoint(tail[1:], method):
        return None
    return f"glab api {method} merge-request write — pre-approved"


ALLOW_PREDICATES = [
    allow_git_glab_mr,
    allow_glab_mr_api,
]


# Commands with no blast radius of their own: navigation, output, read-only
# text filters. They never earn an explicit allow by themselves — they only
# avoid vetoing one that a pre-approved verb in the same command has earned.
NEUTRAL_CMDS = {
    "cd", "pushd", "popd", "pwd", "ls", "echo", "printf", "true", "false",
    "date", "jq", "yq", "grep", "egrep", "fgrep", "rg", "cut", "tr", "sort",
    "uniq", "wc", "head", "tail", "column",
}

_REDIR_RE = re.compile(r"^(\d*|&)>>?(.*)$")


def _redirects_to_file(argv: list[str]) -> bool:
    """True when a segment writes output anywhere but /dev/null. Unknown `>`
    shapes count as a write — the safe direction, since a false positive only
    costs the segment its neutral status."""
    for i, tok in enumerate(argv):
        m = _REDIR_RE.match(tok)
        if not m:
            if ">" in tok:
                return True
            continue
        target = m.group(2) or (argv[i + 1] if i + 1 < len(argv) else "")
        # `2>&1` survives segmentation as a trailing `2>` plus a bare `1`.
        if target in ("", "/dev/null") or target.lstrip("&").isdigit():
            continue
        return True
    return False


def _is_neutral_segment(argv: list[str]) -> bool:
    if not argv:
        return True
    if len(argv) == 1 and (argv[0].isdigit() or _REDIR_RE.match(argv[0])):
        return True
    if _redirects_to_file(argv):
        return False
    cmd = _basename(argv[0])
    if cmd in NEUTRAL_CMDS:
        return True
    if cmd in ("gh", "glab"):
        return ask_gh_glab(argv) is None
    return False


# ────────────────────────────────────────────────────────────────────────────
#  Payload guard — content of indirectly-executed scripts
#
#  This file only ever sees a command line, so `python3 post.py` is opaque to
#  every rule above. payload_guard.py reads the payload instead. It lives beside
#  this file and MUST carry the same chflags uchg lock: it is half of the same
#  boundary, so leaving it writable would make the lock on this file pointless.
#
#  Loaded by absolute path rather than `import payload_guard`, so the hook does
#  not depend on sys.path or the cwd it happens to be invoked from.
# ────────────────────────────────────────────────────────────────────────────

_PAYLOAD_GUARD = Path(__file__).resolve().parent / "payload_guard.py"
_INDIRECT_RE = re.compile(
    r"\b(python[23]?|perl|ruby|node|osascript|bash|sh|zsh|ksh|dash)\b|"
    r"(^|[;&|]\s*)(source|\.)\s+\S|(^|[;&|]\s*)\./\S")


def _payload_ask(command: str) -> str | None:
    """ASK when a script or inline body about to run mutates something outside
    this machine. Returns None when there is no payload to read.

    If the guard cannot be loaded but the command *is* an indirect execution,
    ask rather than allow — a missing or broken guard must not silently reopen
    the hole. Commands with no payload are unaffected, so losing the file
    cannot wedge the shell.
    """
    if not _INDIRECT_RE.search(command):
        return None
    try:
        spec = importlib.util.spec_from_file_location("payload_guard", _PAYLOAD_GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        findings = guard.scan_command(command, cwd=os.getcwd())
        if not findings:
            return None
        return guard.decide(findings)[1]
    except Exception as exc:  # noqa: BLE001
        return (f"payload guard unavailable ({type(exc).__name__}) and this "
                f"command executes a script — confirm by hand")


ASK_PREDICATES = [
    ask_git,
    ask_gh_glab,
    ask_acli,
    ask_kill,
    ask_chflags,
    ask_install,
    ask_curl,
    ask_httpie,
    ask_wget,
    ask_aws_upload,
    ask_kubectl,
    ask_helm,
    ask_terraform,
    ask_az,
    ask_gcloud,
    ask_vault,
]


# ────────────────────────────────────────────────────────────────────────────
#  Evaluator entrypoint
# ────────────────────────────────────────────────────────────────────────────

# Predicates applied to a command sent to a remote host over ssh. This is the
# destructive-filesystem subset: the full local list would also deny things
# like `sudo`, which is unavoidable (and fine) for remote administration.
REMOTE_DENY_PREDICATES = [
    check_rm,
    check_find_delete,
    check_xargs_rm,
    check_disk_tools,
    check_mv_cp_to_nullish,
    check_truncate,
    check_chmod_chown,
    check_mkdir_root,
]

_MAX_SSH_DEPTH = 3


def evaluate_remote(command: str, depth: int = 1) -> str | None:
    """Evaluate a command destined for a remote host. Returns a deny reason."""
    if depth > _MAX_SSH_DEPTH or not command:
        return None
    norm = normalize(command)

    for pat, reason in RAW_DENY:
        if pat.search(norm) and "/dev/" in pat.pattern:
            return reason

    for segment in split_segments(norm):
        argv = tokenize(segment)
        if not argv:
            continue
        for av in {
            tuple(argv),
            tuple(strip_env_prefix(argv)),
            tuple(strip_sudo_prefix(strip_env_prefix(argv))),
        }:
            av_list = list(av)
            for predicate in REMOTE_DENY_PREDICATES:
                reason = predicate(av_list)
                if reason:
                    return reason
            nested = extract_ssh_remote(av_list)
            if nested:
                reason = evaluate_remote(nested, depth + 1)
                if reason:
                    return reason
    return None


def evaluate(command: str) -> tuple[str, str]:
    """Return (decision, reason).
    decision in {'allow', 'allow-explicit', 'deny', 'ask'}.

    Precedence, evaluated over ALL segments of the command:
      1. Any DENY match anywhere  → deny (catastrophic; wins outright).
      2. Any ASK match anywhere   → ask  (unless step 3 explicitly clears it).
      3. Explicit ALLOW: emitted only when at least one segment is pre-approved
         and every other segment is neutral (navigation, output, read-only
         filters — see _is_neutral_segment). That stops a compound like
         `glab mr create && curl …` from riding the allow past the ASK that its
         second segment would otherwise raise, while still clearing the
         `cd repo && <pre-approved> | jq` shape that real commands take.
      4. Otherwise                → allow (silent default; no prompt anyway).

    The distinction between step 3 (explicit allow) and step 4 (implicit
    allow) matters downstream: an explicit hook allow also suppresses the
    auto-mode classifier prompt, whereas the silent default leaves that
    prompt in place. So we only emit explicit-allow for the tight,
    intentionally pre-approved verb set in ALLOW_PREDICATES.
    """
    norm = normalize(command)

    # Raw-text DENY patterns first (redirects, pipe-to-shell, fork bombs).
    for pat, reason in RAW_DENY:
        if pat.search(norm):
            return "deny", reason

    ask_hit: str | None = None
    explicit_hit = False
    all_segments_ok = True
    saw_segment = False

    for segment in split_segments(norm):
        argv = tokenize(segment)
        if not argv:
            continue
        saw_segment = True
        argvs = [argv]
        stripped = strip_env_prefix(argv)
        if stripped is not argv and stripped != argv:
            argvs.append(stripped)
        unsudoed = strip_sudo_prefix(stripped)
        if unsudoed != stripped:
            argvs.append(unsudoed)

        # DENY wins immediately, anywhere in the command.
        for av in argvs:
            # Commands shipped to a remote host: inspect the payload, not `ssh`.
            remote = extract_ssh_remote(av)
            if remote:
                reason = evaluate_remote(remote)
                if reason:
                    return "deny", f"remote command via ssh: {reason}"

            for predicate in DENY_PREDICATES:
                reason = predicate(av)
                if reason:
                    return "deny", reason

        # Record the first ASK, but keep scanning so a later DENY still wins.
        if ask_hit is None:
            for av in argvs:
                for predicate in ASK_PREDICATES:
                    reason = predicate(av)
                    if reason:
                        ask_hit = reason
                        break
                if ask_hit is not None:
                    break

        # Track explicit-allow: one pre-approved segment, the rest neutral.
        seg_allowed = False
        for av in argvs:
            for predicate in ALLOW_PREDICATES:
                if predicate(av):
                    seg_allowed = True
                    break
            if seg_allowed:
                break
        if seg_allowed:
            explicit_hit = True
        elif not any(_is_neutral_segment(av) for av in argvs):
            all_segments_ok = False

    if saw_segment and explicit_hit and all_segments_ok:
        return "allow-explicit", "pre-approved command"

    if ask_hit is None:
        ask_hit = _payload_ask(norm)

    if ask_hit is not None:
        return "ask", ask_hit

    return "allow", ""


def main() -> None:
    session_id = ""
    command = ""
    try:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError):
            deny("malformed hook payload", command="", session_id=session_id)
            return

        session_id = payload.get("session_id", "") or ""
        command = payload.get("tool_input", {}).get("command", "") or ""
        if not command:
            sys.exit(0)

        decision, reason = evaluate(command)
        if decision == "deny":
            deny(reason, command=command, session_id=session_id)
        elif decision == "ask":
            ask(reason, command=command, session_id=session_id)
        elif decision == "allow-explicit":
            # Authoritative allow: suppresses this hook's ASK layer *and* the
            # downstream auto-mode classifier prompt for the pre-approved set.
            _audit("allow", reason, command, session_id)
            _emit("allow", reason)
        else:
            sys.exit(0)

    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        deny(f"hook internal error: {type(exc).__name__}",
             command=command, session_id=session_id)


if __name__ == "__main__":
    main()
