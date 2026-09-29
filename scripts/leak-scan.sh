#!/usr/bin/env bash
# leak-scan.sh — two-part leak gate for this PUBLIC repo.
#
# Scans ALL tracked files (`git ls-files`): gitignored locals — including the
# patterns file itself — are never in scope. Two classes of leak, both fatal:
#
#   1. secret VALUES — session cookie, csrf token, ARN with a real account id,
#      live provider token. Bare paths like ~/.secrets/openrouter are fine.
#   2. employer / internal IDENTIFIERS — read from a gitignored patterns file
#      (one extended-regex per line, `#` comments and blank lines allowed), so
#      no site-specific string ever lives in this public repo. Generic vendor
#      words are intentionally NOT flagged.
#
# The identifier patterns are NOT baked into this script — writing an employer
# name into a public repo is the very leak we are trying to prevent. Default
# file: `.leak-patterns.local` at the repo root; override with
# AI_LEAK_PATTERNS_FILE. A fresh clone has no such file and still runs the
# secret-value half of the check.
#
# Usage: scripts/leak-scan.sh   (no args)
# Prints hits as file:line:content; exits 1 on any hit, 0 otherwise.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
skip() { printf '    \033[1;33mskip\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ -d "$REPO/.git" ]] || die "not a git repo: $REPO"
cd "$REPO"

# --- scope: every tracked file ---------------------------------------------
if [[ -z "$(git ls-files)" ]]; then
  say "no tracked files — nothing to scan"
  exit 0
fi

# --- part 1: secret values (always on) --------------------------------------
SECRET_RE='_gitlab_session=[0-9a-f]{16}|x-csrf-token: ?[A-Za-z0-9._-]{20}|arn:aws:bedrock:[a-z0-9-]+:[0-9]{12}:|sk-or-v1-[A-Za-z0-9]{20}|sk-poe-[A-Za-z0-9_-]{20}|sk-ant-[A-Za-z0-9_-]{20}'

say "leak check (secret values + internal identifiers — public repo)"

secrets="$(git ls-files -z | xargs -0 grep -HInE "$SECRET_RE" 2>/dev/null || true)"

# --- part 2: internal identifiers (from gitignored patterns file) -----------
PATTERNS_FILE="${AI_LEAK_PATTERNS_FILE:-$REPO/.leak-patterns.local}"

internal=""
if [[ -f "$PATTERNS_FILE" ]]; then
  # strip comments/blank lines, join to one alternation
  alt="$(sed -E 's/#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//' "$PATTERNS_FILE" \
         | grep -v '^$' | paste -sd '|' -)"
  if [[ -n "$alt" ]]; then
    internal="$(git ls-files -z | xargs -0 grep -HInE "$alt" 2>/dev/null \
                | grep -vF "$PATTERNS_FILE" || true)"
  fi
else
  skip "no $PATTERNS_FILE — internal-identifier check SKIPPED (secret-value check still ran)"
fi

# --- verdict -----------------------------------------------------------------
if [[ -n "$secrets" || -n "$internal" ]]; then
  [[ -n "$secrets"  ]] && { printf '  -- secret values --\n'; printf '%s\n' "$secrets"; }
  [[ -n "$internal" ]] && { printf '  -- internal identifiers --\n'; printf '%s\n' "$internal"; }
  die "leak check failed (see above) — this repo is PUBLIC; sanitize before committing"
fi
printf '    ok   no secret values or internal identifiers detected\n'
exit 0
