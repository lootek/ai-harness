#!/usr/bin/env bash
# install.sh — deploy ai-harness artifacts into ~/.ai-harness.
#
# Copies ai.py, providers.yaml, sh-aliases/ai from this repo into ~/.ai-harness
# (and ~/.zsh-aliases/ai), with timestamped backups of existing targets, and
# bootstraps the pyyaml venv the wrapper runs on. Idempotent.
#
# Safety + history hooks (--cli claude|codex|opencode|all, default all):
#   claude    full claude hook set -> ~/.claude/hooks/: engines
#             {safe_command,payload_guard}.py + history {log_commands,
#             prompt_history}.py + claude-only {export_session,
#             flush_stale_dumps}.py + session-env-check.sh
#   codex     engines + history engines -> ~/.ai-harness/hooks/, adapters
#             (safe_command, log_commands, prompt_history) ->
#             ~/.ai-harness/adapters/codex/, adapters/codex/hooks.json
#             rendered -> ~/.codex/hooks.json (merged with any existing
#             file: unknown entries preserved, ours replaced)
#   opencode  engines + adapters/opencode/ai-harness.{safe-command,history}.ts
#             -> ~/.config/opencode/plugins/
#
# Skills library (canonical skills/, one dir per skill, bundled assets inside):
#   claude    -> ~/.claude/skills/<name>/            verbatim (the marketplace
#               install in a later step supersedes; harmless until then)
#   codex     -> ~/.agents/skills/<name>/            verbatim except the
#               `allowed-tools:` frontmatter block, stripped from the copied
#               SKILL.md (codex drops it anyway; repo copy stays untouched)
#   opencode  -> ~/.config/opencode/skills/<name>/   verbatim (frontmatter
#               ignored by opencode; allowed-tools kept for reference)
#               plus agents/opencode/*.md reviewer twins ->
#               ~/.config/opencode/agents/ (codex gets the personas bundled
#               inside skills/review-board/agents/ via the skill-dir copy)
#
# Plugins (claude only): registers the `lootek` marketplace from
# github (lootek/ai-harness) and installs the 4 public bundles. While the
# repo is private (public flip is the cutover step), the github add fails
# and the script falls back to registering this checkout as a local
# directory-source marketplace. Idempotent: a lootek marketplace pointing
# elsewhere (stale local dir, or the pre-migration lootek/claude-code-harness
# source) is removed first, plugin installs pinned to a previous marketplace
# generation are uninstalled and reinstalled from the current source, and
# ~/.claude/settings.json is unlocked/relocked around installs on uchg hosts.
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
# history engines: claude runs them directly as PostToolUse/UserPromptSubmit
# hooks; codex adapters spawn them (same files, byte-identical twins).
HISTORY_ENGINES=(log_commands.py prompt_history.py)

# claude: full hook set -> ~/.claude/hooks/ (old backup/uchg discipline)
if want claude; then
  mkdir -p "$HOME/.claude/hooks"
  for f in "${ENGINES[@]}" "${HISTORY_ENGINES[@]}" \
           export_session.py flush_stale_dumps.py session-env-check.sh; do
    install_file "$SRC/hooks/$f" "$HOME/.claude/hooks/$f" 755
  done
  # safe_command.py imports payload_guard.py, so both halves of the boundary
  # need the immutable flag — locking only one leaves the other writable.
  echo "  NOTE: re-apply the lock:  chflags uchg $HOME/.claude/hooks/safe_command.py $HOME/.claude/hooks/payload_guard.py"
fi

# codex + opencode: engines -> $DST/hooks/ (single owner of the rule set;
# history engines included — the codex adapters spawn them from there)
if want codex || want opencode; then
  mkdir -p "$DST/hooks"
  for f in "${ENGINES[@]}" "${HISTORY_ENGINES[@]}"; do
    install_file "$SRC/hooks/$f" "$DST/hooks/$f" 755
  done
fi

# codex: adapters + hooks.json (render template, merge into ~/.codex/hooks.json)
if want codex; then
  mkdir -p "$DST/adapters/codex"
  for a in safe_command_codex.py log_commands_codex.py prompt_history_codex.py; do
    install_file "$SRC/adapters/codex/$a" "$DST/adapters/codex/$a" 755
  done
  CODEX_HOOKS="$HOME/.codex/hooks.json"
  mkdir -p "$HOME/.codex"
  backup_if_exists "$CODEX_HOOKS"
  RENDERED="$(mktemp)"
  sed "s|__AI_HARNESS_HOME__|$DST|g" "$SRC/adapters/codex/hooks.json" > "$RENDERED"
  python3 - "$RENDERED" "$CODEX_HOOKS" <<'PY'
# Merge our rendered entries into ~/.codex/hooks.json: unknown entries and
# events are preserved verbatim; any previous entry of OURS (matched by the
# ai-harness adapter path in its command) is replaced by the new one.
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
MARKER = "ai-harness/adapters/codex/"
def is_ours(entry):
    return isinstance(entry, dict) and any(
        MARKER in (h or {}).get("command", "")
        for h in entry.get("hooks", []) if isinstance(h, dict))
hooks = existing.setdefault("hooks", {})
if not isinstance(hooks, dict):
    hooks = {}
    existing["hooks"] = hooks
for event, entries in ours["hooks"].items():
    kept = [e for e in (hooks.get(event) or []) if not is_ours(e)]
    hooks[event] = kept + entries
with open(target, "w") as f:
    json.dump(existing, f, indent=2)
    f.write("\n")
PY
  rm -f "$RENDERED"
  echo "  -> $CODEX_HOOKS (merged)"
  echo "  NOTE: codex requires per-definition hook trust — run codex once and"
  echo "        pick '2. Trust all and continue' (hooks stay skipped until"
  echo "        then). Needed again after ANY change to this file, including"
  echo "        this one if the hook commands changed."
  echo "  NOTE: re-apply the lock:  chflags uchg $DST/hooks/safe_command.py $DST/hooks/payload_guard.py"
fi

# opencode: TS plugin shims -> ~/.config/opencode/plugins/
if want opencode; then
  mkdir -p "$HOME/.config/opencode/plugins"
  install_file "$SRC/adapters/opencode/ai-harness.safe-command.ts" \
               "$HOME/.config/opencode/plugins/ai-harness.safe-command.ts"
  install_file "$SRC/adapters/opencode/ai-harness.history.ts" \
               "$HOME/.config/opencode/plugins/ai-harness.history.ts"
  # guard: an opencode plugin that fails to load is silently OFF (the safety
  # gate included). `opencode plugin list` shows a loadable plugin by its
  # declared id and a rejected one by bare path — verify both ids are there.
  if command -v opencode >/dev/null 2>&1; then
    pl="$(opencode plugin list 2>&1)"
    for id in ai-harness.safe-command ai-harness.history; do
      if ! printf '%s\n' "$pl" | awk -v id="$id" '$1 == id {f=1} END {exit !f}'; then
        echo "  !!! opencode did NOT load plugin $id — the safety gate is OFF on opencode." >&2
        echo "      opencode $(opencode --version 2>&1): check 'opencode plugin list' and" >&2
        echo "      'grep \"failed to load plugin\" ~/.local/share/opencode/log/opencode.log'" >&2
        PLUGIN_LOAD_FAILED=1
      fi
    done
  fi
  # reviewer subagent twins (regenerate: scripts/convert-agents-opencode.py;
  # README.md stays out — opencode indexes every .md here as an agent)
  mkdir -p "$HOME/.config/opencode/agents"
  for a in "$SRC"/agents/opencode/reviewer-*.md; do
    install_file "$a" "$HOME/.config/opencode/agents/$(basename "$a")"
  done
  echo "  NOTE: re-apply the lock:  chflags uchg $DST/hooks/safe_command.py $DST/hooks/payload_guard.py"
fi

# ── skills library: canonical skills/ -> all three CLIs ─────────────────────
SKILLS=(imagine mr-monitor mr-review review-board)

install_skill_dir() {
  # backup + replace one skill dir under a given parent (e.g. ~/.claude/skills)
  local src="$1" parent="$2" name="$3"
  mkdir -p "$parent"
  backup_if_exists "$parent/$name"
  rm -rf "$parent/$name"
  cp -R "$src" "$parent/$name"
  echo "  -> $parent/$name"
}

if want claude; then
  for s in "${SKILLS[@]}"; do
    install_skill_dir "$SRC/skills/$s" "$HOME/.claude/skills" "$s"
  done
fi

if want codex; then
  for s in "${SKILLS[@]}"; do
    install_skill_dir "$SRC/skills/$s" "$HOME/.agents/skills" "$s"
    # strip the allowed-tools block (header line + its "  - item" lines) from
    # the COPY only — codex rejects/ignores it, and the repo file is untouched.
    sed -i '' -e '/^allowed-tools: *\[/d' \
              -e '/^allowed-tools:$/,/^[^ ]/{/^allowed-tools:$/d;/^  - /d;}' \
              "$HOME/.agents/skills/$s/SKILL.md"
  done
fi

if want opencode; then
  for s in "${SKILLS[@]}"; do
    install_skill_dir "$SRC/skills/$s" "$HOME/.config/opencode/skills" "$s"
  done
fi

# ── plugins: lootek marketplace + the 4 public bundles (claude only) ────────
# Marketplace source, in preference order: github lootek/ai-harness (works
# once the repo is public — the cutover step flips it), else this checkout as
# a directory-source marketplace (the repo root holds
# .claude-plugin/marketplace.json; plugins live under plugins/<name>). Once
# either source is registered the section is a no-op; switching a registered
# local checkout over to github at the public flip is a cutover action
# (remove + add), not something every install run should attempt — a
# marketplace remove also uninstalls its plugins.
#
# `claude plugin install` records the plugin in enabledPlugins inside
# ~/.claude/settings.json; on hosts that lock that file with uchg the CLI's
# atomic rename fails with EPERM and installs silently no-op — clear the flag
# around this section and re-apply it after (same discipline as hook files).
PUBLIC_PLUGINS=(imagine mr-monitor mr-review review-board)
if want claude; then
  if command -v claude >/dev/null 2>&1; then
    SETTINGS="$HOME/.claude/settings.json"
    SETTINGS_LOCKED=0
    if [ -f "$SETTINGS" ] && has_uchg "$SETTINGS"; then
      SETTINGS_LOCKED=1
      chflags nouchg "$SETTINGS"
    fi
    echo "Registering lootek marketplace (github: lootek/ai-harness, local fallback)"
    MKLIST="$(claude plugin marketplace list 2>/dev/null || true)"
    if printf '%s\n' "$MKLIST" | grep -qF 'Source: GitHub (lootek/ai-harness)' || \
       printf '%s\n' "$MKLIST" | grep -qF "Source: Directory ($SRC)"; then
      : # already registered from this repo — nothing to do
    else
      # drop any lootek marketplace pointing elsewhere (the pre-migration
      # github source lootek/claude-code-harness, or a stale local dir) —
      # note this uninstalls plugins still pinned to that marketplace; the
      # install loop below re-installs them from the current source
      claude plugin marketplace remove lootek >/dev/null 2>&1 || true
      if ! claude plugin marketplace add lootek/ai-harness >/dev/null 2>&1; then
        # repo still private: register this checkout instead
        claude plugin marketplace add "$SRC"
      fi
    fi
    for p in "${PUBLIC_PLUGINS[@]}"; do
      if ! claude plugin install "$p@lootek" >/dev/null 2>&1 && \
         ! claude plugin update "$p@lootek" >/dev/null 2>&1; then
        # install pinned to a previous marketplace generation — drop just the
        # plugin install and re-install from the current source
        claude plugin uninstall "$p@lootek" >/dev/null 2>&1 || true
        claude plugin install "$p@lootek" >/dev/null 2>&1 || true
      fi
      if claude plugin list 2>/dev/null | grep -qF "$p@lootek"; then
        echo "  -> $p@lootek"
      else
        echo "  !! $p@lootek NOT installed — run: claude plugin install $p@lootek"
      fi
    done
    if [ "$SETTINGS_LOCKED" = 1 ]; then
      chflags uchg "$SETTINGS"
      echo "  NOTE: re-applied the lock: chflags uchg $SETTINGS"
    fi
  else
    echo "  (claude CLI not on PATH — skipping marketplace register/install; run manually)"
  fi
fi

if [ -n "${PLUGIN_LOAD_FAILED:-}" ]; then
  echo "FAILED: opencode plugin(s) not loading (see above)." >&2
  exit 1
fi
echo "done. Restart claude/codex/opencode if hooks or plugins changed."
