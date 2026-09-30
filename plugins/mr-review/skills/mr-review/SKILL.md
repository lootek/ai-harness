---
name: mr-review
description: "Use when reviewing a GitLab MR and posting urgent inline comments — drafts each comment, shows it inline before asking, posts only on explicit user approval, anchors via the GitLab Discussions API. Trigger phrases: 'review MR', 'mr-review', 'inline comments on <MR>', 'find urgent issues in MR'."
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
---

## Running on other CLIs

On opencode: `AskUserQuestion` → `ask`. On codex: there is no interactive-prompt tool — print the options and STOP; act only on the user's next reply.

# MR Review (with approved inline comments)

Review GitLab MR **$ARGUMENTS** for urgent issues and post each finding as a true inline comment, but only after the user explicitly approves it.

## Workflow

### 1. Set up context

- Resolve repo + MR ID from `$ARGUMENTS` (URL or `<repo> <id>`).
- `cd` once into the local clone (default `~/projects/<group>/<repo>`); if not present, ask before cloning.
- `git fetch && glab mr checkout <id>` so you're on the MR head locally.
- Capture MR metadata: `glab api projects/<urlencoded-repo>/merge_requests/<id>` → save `diff_refs.{base_sha,head_sha,start_sha}` and `project_id`. These are required to anchor inline comments.

### 2. Read the diff and the changed files

- `glab mr diff <id>` for the unified diff overview.
- For each changed file, read it from the local checkout (`Read` tool). **Use line numbers from the actual file** — never from the diff body, which has its own line numbering.
- If the MR head moves while you're working, re-fetch (`git pull --ff-only`) and refresh `diff_refs` from the API. Comments anchored to a stale `head_sha` will still post but may not show as inline once the MR is updated.

### 3. Identify urgent findings only

- Focus on **bugs, correctness gaps, security risks, data-loss paths, IAM over-grants, broken cross-account/cross-region patterns**.
- Skip suggestions, style nits, and "nice-to-haves" unless explicitly asked.
- Pre-existing reviewer comments (human or bot) — read them and **do not duplicate**. Note when a previous concern has already been resolved by a later commit.

### 4. Draft inline comments locally first

Save to `<session-dir>/MR_<id>_INLINE_COMMENTS.md` as a numbered list. Each entry has:
- File path and exact line number(s) (verified against the current head)
- Severity (high / medium / low)
- The comment body itself

### 5. Approval loop — one comment at a time

For each draft comment, in this exact order:

1. **Show the full comment body inline in chat**, prefixed with the file:line header. Render the markdown the user will see — don't summarize.
2. **Then call `AskUserQuestion`** with options: `Post as-is` / `Edit first` / `Skip`. (On codex there is no prompt tool — print the three options and stop; the next user reply decides.)
3. If `Edit first`: ask what to change, redraft, show again, ask again.
4. If `Post as-is`: post via the GitLab Discussions API (see step 6).
5. If `Skip`: move on without posting.

Never batch-post. Never assume implicit approval. The user said "yes" once for one comment — that does not extend.

### 6. Post as a true inline (DiffNote)

`glab mr note` posts a flat thread comment, not inline. For inline, use the Discussions API:

```bash
glab api --method POST -H "Content-Type: application/json" \
  --input <payload.json> \
  "projects/<project_id>/merge_requests/<iid>/discussions"
```

Payload shape:

```json
{
  "body": "[Claude Opus 4.7 code review]\n\n<body>",
  "position": {
    "base_sha": "<from diff_refs>",
    "head_sha": "<from diff_refs>",
    "start_sha": "<from diff_refs>",
    "position_type": "text",
    "new_path": "<file path relative to repo root>",
    "old_path": "<same as new_path for modified files>",
    "new_line": <line number in the new file>
  }
}
```

For an added file, `old_path` is still required but the API tolerates it equal to `new_path`. For a deleted line, use `old_line` instead of `new_line`. For multi-line, add `line_range` (rarely needed for review comments).

After posting, confirm: response should include a non-null `position` and `notes[0].type == "DiffNote"`. If the response is `{"id": "...", "individual_note": true}` with no `position`, it landed as a flat comment — the position payload was rejected.

### 7. Comment prefix

Every comment body starts with `[Claude Opus 4.7 code review]` on its own line, followed by a blank line, then the actual content.

(Or whichever model is active. The point is to disclose authorship so reviewers can weight the input appropriately.)

## Rules

- **Always work from the local clone**, not the diff alone — the diff has different line numbers and you'll mis-anchor.
- **Verify line numbers** before drafting each comment: re-`Read` the file with line numbers visible.
- **Show before asking**: the user must see the rendered comment in chat *above* the approval prompt. Don't put the body only inside the `AskUserQuestion` preview — they may not expand it.
- **One approval per comment**. Never extend a previous "post" to subsequent items.
- **Disclose model authorship** in the prefix.
- **Re-check resolved findings**: if the author pushed a new commit during the review, walk through the draft list and mark anything already addressed. Skip those, don't post.
- **Stay focused on urgent**. Do not auto-expand into stylistic feedback.
- **No `cd` chaining**: cd once into the repo and stay there for the duration. Use `git -C <path>` only for one-off ops.
- **GPG-signed commits** if any commits get made (none should, this skill reviews — it doesn't fix).

## Output

A summary table at the end:

| # | File:line | Discussion/Note ID | Status |
|---|---|---|---|
| 1 | path:line | discussion_id / note_id | Posted / Skipped / Resolved-by-later-commit / Edited |

Plus links to each discussion on the MR.

## When NOT to use

- Pure code style cleanups (use a different reviewer pattern).
- Bulk approvals or rubber-stamping.
- Posting comments without per-comment approval — that's not what this skill does. If the user wants batch posting, use `glab mr note` directly.
- Fixing the issues yourself — that's `mr-monitor`. This skill only reviews and comments.
