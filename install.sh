#!/usr/bin/env bash
# install.sh — deploy ai-harness artifacts into ~/.ai-harness.
#
# Copies ai.py, providers.yaml, sh-aliases/ai from this repo into ~/.ai-harness
# (and ~/.zsh-aliases/ai), with timestamped backups of existing targets, and
# bootstraps the pyyaml venv the wrapper runs on. Idempotent.
#
# Safety hooks (--cli claude|codex|opencode|all, default all):
#   claude    hooks/{safe_command,payload_guard}.py -> ~/.claude/hooks/
#   codex     engines -> ~/.ai-harness/hooks/, adapter -> ~/.ai-harness/adapters/codex/,
#             adapters/codex/hooks.json rendered -> ~/.codex/hooks.json
#             (merged with any existing file: unknown entries preserved, ours replaced)
#   opencode  engines + adapters/opencode/ai-harness.safe-command.ts
#             -> ~/.config/opencode/plugins/
#
# Files protected with the macOS `uchg` (user-immutable) flag are handled with
# a tight clear -> copy -> re-apply pair so they're never left writable. Files
# without the flag are copied plainly (the script stays generic).

set -euo pipefail

CLI="all"
while [ $# -gt 0 ]; do
  case "$1" in
    --cli)    CLI="${2:?--cli needs a value}"; shift 2 ;;
    --cli=*)  CLI="${1#--cli=}"; shift ;;
    *)        echo "unknown argument: $1 (usage: install.sh [--cli claude|codex|opencode|all])" >&2; exit 2 ;;
  esac
done
case "$CLI" in
  claude|codex|opencode|all) ;;
  *) echo "--cli must be claude, codex, opencode or all (got: $CLI)" >&2; exit 2 ;;
esac

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="$HOME/.ai-harness"
ALIAS_DST="$HOME/.zsh-aliases"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="$DST/backups/$TS"

want() { [ "$CLI" = "all" ] || [ "$CLI" = "$1" ]; }

mkdir -p "$DST" "$ALIAS_DST" "$BACKUP"

backup_if_exists() {
  local path="$1"
  if [ -e "$path" ]; then
    local rel="${path#$HOME/}"
    local dest="$BACKUP/$rel"
    mkdir -p "$(dirname "$dest")"
    cp -a "$path" "$dest"
  fi
}

has_uchg() { ls -lO "$1" 2>/dev/null | awk '{print $5}' | grep -qx uchg; }

# copy a file, clearing+re-applying uchg if the destination has it
install_file() {
  local src="$1" dst="$2" mode="${3:-}"
  backup_if_exists "$dst"
  if has_uchg "$dst"; then
    chflags nouchg "$dst"
    cp "$src" "$dst"
    chflags uchg "$dst"
  else
    cp "$src" "$dst"
  fi
  [ -n "$mode" ] && chmod "$mode" "$dst"
  echo "  -> $dst"
}

echo "Installing ai-harness picker from $SRC"
echo "Backups (if any) -> $BACKUP"

# ai/air wrapper + provider catalog
install_file "$SRC/ai.py"          "$DST/ai.py" 755
install_file "$SRC/providers.yaml" "$DST/providers.yaml" 600

# shell alias (ai / air / ccprov)
# Replacing the old ccc/ccr flavor from claude-code-harness is the point of
# this repo — a backup is kept above. A machine-specific non-picker file is
# still left alone (same guard discipline as the old install, marker widened
# to cover both the old claude-only and the new multi-CLI picker flavors).
if [ ! -s "$ALIAS_DST/ai" ] || grep -q 'fzf-driven' "$ALIAS_DST/ai"; then
  install_file "$SRC/sh-aliases/ai" "$ALIAS_DST/ai"
else
  echo "  SKIP $ALIAS_DST/ai — existing file is not a picker flavor; left untouched"
fi

# ai/air needs pyyaml in an isolated venv (system python on macOS is PEP-668)
VENV="$DST/.venv"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import yaml' 2>/dev/null; then
  echo "Bootstrapping ai venv at $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q pyyaml
fi

# ── safety hooks: safe_command + payload_guard, owned once here, wired per CLI ──
# The engine pair is identical everywhere; only the wiring differs:
#   claude    runs safe_command.py directly as a PreToolUse hook
#   codex     runs adapters/codex/safe_command_codex.py (ASK demoted to deny:
#             codex exec treats "ask" as a failed hook and runs the command)
#   opencode  plugin spawns safe_command.py per bash call (ASK keeps its label)
# Shared audit trail: ~/.claude/hooks/safe_command_audit.jsonl
# ($AI_SAFE_COMMAND_AUDIT overrides; entries carry each CLI's session id).

ENGINES=(safe_command.py payload_guard.py)

# claude: engines -> ~/.claude/hooks/ (old backup/uchg discipline)
if want claude; then
  mkdir -p "$HOME/.claude/hooks"
  for f in "${ENGINES[@]}"; do
    install_file "$SRC/hooks/$f" "$HOME/.claude/hooks/$f" 755
  done
  # safe_command.py imports payload_guard.py, so both halves of the boundary
  # need the immutable flag — locking only one leaves the other writable.
  echo "  NOTE: re-apply the lock:  chflags uchg $HOME/.claude/hooks/safe_command.py $HOME/.claude/hooks/payload_guard.py"
fi

# codex + opencode: engines -> $DST/hooks/ (single owner of the rule set)
if want codex || want opencode; then
  mkdir -p "$DST/hooks"
  for f in "${ENGINES[@]}"; do
    install_file "$SRC/hooks/$f" "$DST/hooks/$f" 755
  done
fi

# codex: adapter + hooks.json (render template, merge into ~/.codex/hooks.json)
if want codex; then
  mkdir -p "$DST/adapters/codex"
  install_file "$SRC/adapters/codex/safe_command_codex.py" \
               "$DST/adapters/codex/safe_command_codex.py" 755
  CODEX_HOOKS="$HOME/.codex/hooks.json"
  mkdir -p "$HOME/.codex"
  backup_if_exists "$CODEX_HOOKS"
  RENDERED="$(mktemp)"
  sed "s|__AI_HARNESS_HOME__|$DST|g" "$SRC/adapters/codex/hooks.json" > "$RENDERED"
  python3 - "$RENDERED" "$CODEX_HOOKS" <<'PY'
# Merge our rendered entry into ~/.codex/hooks.json: unknown entries and
# events are preserved verbatim; any previous entry of OURS (matched by the
# adapter path in its command) is replaced by the new one.
import json, sys
rendered, target = sys.argv[1], sys.argv[2]
with open(rendered) as f:
    ours = json.load(f)
try:
    with open(target) as f:
        existing = json.load(f)
    if not isinstance(existing, dict):
        raise ValueError("not an object")
except (FileNotFoundError, ValueError, json.JSONDecodeError):
    existing = {}
MARKER = "ai-harness/adapters/codex/safe_command_codex.py"
def is_ours(entry):
    return isinstance(entry, dict) and any(
        MARKER in (h or {}).get("command", "")
        for h in entry.get("hooks", []) if isinstance(h, dict))
hooks = existing.setdefault("hooks", {})
if not isinstance(hooks, dict):
    hooks = {}
    existing["hooks"] = hooks
pre = [e for e in (hooks.get("PreToolUse") or []) if not is_ours(e)]
pre += ours["hooks"]["PreToolUse"]
hooks["PreToolUse"] = pre
with open(target, "w") as f:
    json.dump(existing, f, indent=2)
    f.write("\n")
PY
  rm -f "$RENDERED"
  echo "  -> $CODEX_HOOKS (merged)"
  echo "  NOTE: codex requires per-definition hook trust — run codex once and"
  echo "        pick '2. Trust all and continue' (hooks stay skipped until then)."
  echo "  NOTE: re-apply the lock:  chflags uchg $DST/hooks/safe_command.py $DST/hooks/payload_guard.py"
fi

# opencode: TS plugin shim -> ~/.config/opencode/plugins/
if want opencode; then
  mkdir -p "$HOME/.config/opencode/plugins"
  install_file "$SRC/adapters/opencode/ai-harness.safe-command.ts" \
               "$HOME/.config/opencode/plugins/ai-harness.safe-command.ts"
  echo "  NOTE: re-apply the lock:  chflags uchg $DST/hooks/safe_command.py $DST/hooks/payload_guard.py"
fi

echo "done. Restart claude/codex/opencode if hooks or plugins changed."
