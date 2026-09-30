#!/usr/bin/env bash
# sync-skills.sh — keep the plugin bundles' skill copies in lockstep with the
# canonical skills/ library.
#
# skills/<name>/ is the single source of truth (edited here; cross-CLI dialect
# notes included). Each plugins/<p>/skills/<name>/ copy must be an EXACT mirror
# — claude marketplace bundles stay standalone, so nothing is stripped.
#
# Default mode CHECKS ONLY and fails (exit 1) if any plugin copy has drifted:
#   scripts/sync-skills.sh            # verify, report drift, exit non-zero on any
#   scripts/sync-skills.sh --fix      # re-copy skills/<name>/ -> plugins/<p>/skills/<name>/
#
# Called from populate.sh after capture so drift can't sneak in unnoticed.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX=false
[ "${1:-}" = "--fix" ] && FIX=true

# skill:plugin pairs (1:1 today; keep in sync when adding either side).
# Plain pairs, not an associative array — macOS still ships bash 3.2.
SKILLS="imagine:imagine mr-monitor:mr-monitor mr-review:mr-review review-board:review-board"

drift=0
for pair in $SKILLS; do
  name="${pair%%:*}"
  plugin="${pair##*:}"
  src="$REPO/skills/$name"
  dst="$REPO/plugins/$plugin/skills/$name"
  if [ ! -d "$src" ]; then
    echo "ERROR: canonical skills/$name missing" >&2
    drift=1
    continue
  fi
  if $FIX; then
    mkdir -p "$(dirname "$dst")"
    rm -rf "$dst"
    cp -R "$src" "$dst"
    echo "synced  skills/$name -> plugins/$plugin/skills/$name"
  elif ! diff -rq "$src" "$dst" >/dev/null 2>&1; then
    echo "DRIFT: plugins/$plugin/skills/$name differs from skills/$name" >&2
    diff -rq "$src" "$dst" >&2 || true
    drift=1
  else
    echo "ok      plugins/$plugin/skills/$name == skills/$name"
  fi
done

if [ "$drift" -ne 0 ]; then
  echo "skill drift detected — run: scripts/sync-skills.sh --fix  (then review the diff)" >&2
  exit 1
fi
$FIX || echo "all plugin skill copies match the canonical library."
