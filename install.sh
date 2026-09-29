#!/usr/bin/env bash
# install.sh — deploy ai-harness picker artifacts into ~/.ai-harness.
#
# Copies ai.py, providers.yaml, sh-aliases/ai from this repo into ~/.ai-harness
# (and ~/.zsh-aliases/ai), with timestamped backups of existing targets, and
# bootstraps the pyyaml venv the wrapper runs on. Idempotent.
#
# Picker-only for now: hooks/plugins/agents/skills install comes at cutover —
# this script deliberately does NOT touch ~/.claude or the old
# claude-code-harness install (ccc/ccr keep working until then).
#
# Files protected with the macOS `uchg` (user-immutable) flag are handled with
# a tight clear -> copy -> re-apply pair so they're never left writable. Files
# without the flag are copied plainly (the script stays generic).

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="$HOME/.ai-harness"
ALIAS_DST="$HOME/.zsh-aliases"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="$DST/backups/$TS"

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

echo "done. hooks/plugins/agents/skills install comes at cutover (old ~/.claude install untouched)."
