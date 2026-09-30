---
name: mr-monitor
description: "Use when monitoring a GitLab MR pipeline and review threads, fixing issues until all green and resolved. Trigger phrases: 'monitor MR', 'watch pipeline', 'fix MR', 'mr-monitor'."
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - Agent
---

# MR Monitor & Fix

Monitor MR **$ARGUMENTS** until pipeline passes and all review discussions are resolved.

First, cd once and stay in the repo dir while working on it, avoid chaininig `cd ... && ...`.

## Loop

1. **Pipeline status**: `glab -R <repo> ci status` — if failed, read job logs, identify root cause, fix, commit, push
2. **Review threads**: `glab -R <repo> mr view <id> --comments` — for each unresolved discussion:
   - If actionable code feedback: fix the code, commit, push, reply noting the fix
   - If question/clarification: reply with context
   - If already addressed: note resolution
3. **Wait 5 minutes**, repeat from step 1
4. **Exit** when: pipeline green AND all discussions resolved

## Rules

- Run from the repo directory: first default to cloned locally within working dir, or if missing then search within `~/projects/`
- Commits: no Co-Authored-By, terse message prefixed with Jira ID from branch name
- Use `git -C <path>` for git commands, subshell `(cd <path> && glab ...)` for glab
- Never force-push or amend — always new commits
- If stuck after 2 fix attempts on the same issue, stop and report
- Write progress to `<session-dir>/mr-monitor.md` so the user can check async
