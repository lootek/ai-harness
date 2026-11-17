# memory — shared curated memory for AI CLIs

An MCP stdio server and a small engine for a file-based memory store that several
agent CLIs (Claude Code, Codex, anything that speaks MCP) read and write. The store is
plain Markdown: one fact per file plus a `MEMORY.md` index. It's the same layout
Claude Code's auto-memory uses, so an existing Claude memory dir works as-is and
needs no import.

Stdlib only (Python ≥ 3.10). No daemon, no database.

## Why a server and not "just edit the files"

Every write goes through one code path, so the store can't drift, whichever agent wrote it:

| Concern | What the engine does |
|---|---|
| Format | `create` requires `name`, a one-line `description` and a `type` (`user`/`feedback`/`project`/`reference`); `feedback`/`project` bodies need `**Why:**` and `**How to apply:**` lines; edits that would break the frontmatter are refused |
| Index | `create`/`delete`/`rename`/`supersede` keep the `MEMORY.md` line in step; changing a description updates its index hook |
| Duplicates | `create` scores the new memory against the store (TF-IDF cosine over name, description and body). At or above `AI_MEMORY_DUP_THRESHOLD` (0.35) it refuses and returns the candidates. A same-name memory is always refused. `force=true` needs a `force_reason` |
| Contradictions | `supersede(old, new, reason)` marks the old memory `superseded_by` with a date, prepends the reason, and drops it from the index; the file stays for history and `search(include_superseded=true)` still finds it |
| Concurrent edits | one `flock` per store; atomic temp-file writes; `str_replace`/`insert`/`delete` require the `hash` from the caller's last `view`, so an edit based on a stale read is rejected |
| Safety | writes are limited to top-level `*.md` in the store (no `..`, no subdirs, no dotfiles); a secret-pattern gate refuses tokens and keys; `delete` moves to `.trash/` |

## Tools

`memory_view`, `memory_search`, `memory_create`, `memory_str_replace`, `memory_insert`,
`memory_supersede`, `memory_delete`, `memory_rename`, `memory_lint`. The command set
follows the shape of Anthropic's client-side
[memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool),
with `search`, `supersede` and `lint` added. The server sends usage instructions in its
`initialize` response.

## Install

```sh
DIR=~/path/to/memory            # the store; MEMORY.md + *.md
SRV=$PWD/mcp/memory/memory_mcp.py

# Claude Code
claude mcp add -s user memory -e AI_MEMORY_DIR=$DIR -- python3 $SRV
# then allow it: "mcp__memory__*" in ~/.claude/settings.json permissions.allow
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.memory]
command = "python3"
args = ["/abs/path/mcp/memory/memory_mcp.py"]
default_tools_approval_mode = "approve"

[mcp_servers.memory.env]
AI_MEMORY_DIR = "/abs/path/to/memory"

[mcp_servers.memory.tools.memory_delete]
approval_mode = "prompt"
```

Then tell every CLI, in its global instructions, to use the `memory_*` tools for all
memory reads and writes instead of editing the files. Claude Code's built-in
auto-memory would otherwise keep writing files directly and skip the checks.

## CLI

```sh
AI_MEMORY_DIR=$DIR python3 memory_engine.py lint      # exit 1 on problems
AI_MEMORY_DIR=$DIR python3 memory_engine.py search "query" -n 5
AI_MEMORY_DIR=$DIR python3 memory_engine.py dups      # near-duplicate pairs
```

## Tests

```sh
python3 -m pytest -q mcp/memory/tests
```
