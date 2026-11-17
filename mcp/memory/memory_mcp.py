#!/usr/bin/env python3
"""memory_mcp - MCP stdio server over memory_engine (stdlib only).

Register the same server in every CLI so they all share one curated store:

  claude mcp add -s user memory -e AI_MEMORY_DIR=<dir> -- python3 <path>/memory_mcp.py
  # ~/.codex/config.toml
  [mcp_servers.memory]
  command = "python3"
  args = ["<path>/memory_mcp.py"]
  env = { AI_MEMORY_DIR = "<dir>" }

Tools follow the shape of Anthropic's memory tool (view/create/str_replace/insert/
delete/rename) plus search, supersede and lint.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_engine as me  # noqa: E402

SERVER = {"name": "memory", "version": "0.1.0"}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """Curated long-term memory shared by every AI CLI on this machine: one fact per file,
MEMORY.md is the index. Use these tools for ALL reads and writes of the store - never edit its files directly.
- Recall: memory_search with the topic's keywords, then memory_view the hits. Memories are dated leads: re-verify before acting.
- Add: memory_create. If it answers "possible duplicate", memory_view the top candidate and decide:
  same fact -> memory_str_replace it; the new fact contradicts it -> memory_create with force=true and
  force_reason, then memory_supersede the old one; genuinely different -> memory_create with force=true and force_reason.
- Edit: memory_view first, then memory_str_replace/memory_insert with the hash it returned. A hash mismatch means
  someone else changed the file: view again and redo the edit.
- Never store secrets. Convert relative dates to absolute ones."""

S = {"type": "string"}
TOOLS = [
    {"name": "memory_view",
     "description": "Without file: the MEMORY.md index. With file: that memory with line numbers. Always returns the content hash needed for edits.",
     "inputSchema": {"type": "object", "properties": {"file": {**S, "description": "e.g. feedback_foo.md; omit for the index"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "memory_search",
     "description": "Rank memories by similarity to a query (name and description weigh most). Returns file, name, description, type, score.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": S, "limit": {"type": "integer", "default": 10},
         "include_superseded": {"type": "boolean", "default": False}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "memory_create",
     "description": "Create one memory and its index line. Refuses a likely duplicate and returns the candidates instead (see server instructions). feedback/project bodies need '**Why:**' and '**How to apply:**' lines; link related memories as [[name]].",
     "inputSchema": {"type": "object", "required": ["name", "description", "type", "body"], "properties": {
         "name": {**S, "description": "short kebab-case slug"},
         "description": {**S, "description": "one line, used for recall"},
         "type": {"type": "string", "enum": list(me.TYPES)},
         "body": {**S, "description": "markdown body without frontmatter"},
         "title": {**S, "description": "index line title (default: name)"},
         "hook": {**S, "description": "index line hook (default: description)"},
         "file": {**S, "description": "override file name; must start with '<type>_'"},
         "force": {"type": "boolean", "default": False},
         "force_reason": {**S, "description": "required with force: why this is not a duplicate"}}}},
    {"name": "memory_str_replace",
     "description": "Replace one exact, unique occurrence of old with new in a memory (or in MEMORY.md). Needs expected_hash from memory_view. Updates `modified`, and the index hook when the description changes.",
     "inputSchema": {"type": "object", "required": ["file", "old", "new", "expected_hash"], "properties": {
         "file": S, "old": S, "new": S, "expected_hash": S}}},
    {"name": "memory_insert",
     "description": "Insert text after a given line number (as shown by memory_view; 0-based count of lines kept above). Needs expected_hash.",
     "inputSchema": {"type": "object", "required": ["file", "after_line", "text", "expected_hash"], "properties": {
         "file": S, "after_line": {"type": "integer"}, "text": S, "expected_hash": S}}},
    {"name": "memory_supersede",
     "description": "Retire an outdated memory in favour of a newer one: marks it superseded_by + date, prepends a note with the reason, drops it from the index. The file stays for history.",
     "inputSchema": {"type": "object", "required": ["old_file", "new_file", "reason"], "properties": {
         "old_file": S, "new_file": S, "reason": S}}},
    {"name": "memory_delete",
     "description": "Move a memory to .trash/ and drop its index line. Prefer memory_supersede when a fact changed; delete only what was wrong or junk.",
     "inputSchema": {"type": "object", "required": ["file", "expected_hash"], "properties": {"file": S, "expected_hash": S}},
     "annotations": {"destructiveHint": True}},
    {"name": "memory_rename",
     "description": "Rename a memory file and its index link.",
     "inputSchema": {"type": "object", "required": ["old_file", "new_file"], "properties": {"old_file": S, "new_file": S}}},
    {"name": "memory_lint",
     "description": "Check the store: files missing from the index, dangling index lines, bad frontmatter, duplicate names, dangling [[links]], secret hits. Optionally list near-duplicate pairs.",
     "inputSchema": {"type": "object", "properties": {"near_duplicates": {"type": "boolean", "default": False}}},
     "annotations": {"readOnlyHint": True}},
]


def call(store: me.Store, name: str, a: dict):
    if name == "memory_view":
        return store.view(a.get("file"))
    if name == "memory_search":
        return store.search(a["query"], int(a.get("limit", 10)), bool(a.get("include_superseded", False)))
    if name == "memory_create":
        return store.create(name=a["name"], description=a["description"], type=a["type"], body=a["body"],
                            title=a.get("title"), hook=a.get("hook"), file=a.get("file"),
                            force=bool(a.get("force", False)), force_reason=a.get("force_reason", ""))
    if name == "memory_str_replace":
        return store.str_replace(file=a["file"], old=a["old"], new=a["new"], expected_hash=a.get("expected_hash", ""))
    if name == "memory_insert":
        return store.insert(file=a["file"], after_line=int(a["after_line"]), text=a["text"],
                            expected_hash=a.get("expected_hash", ""))
    if name == "memory_supersede":
        return store.supersede(old_file=a["old_file"], new_file=a["new_file"], reason=a["reason"])
    if name == "memory_delete":
        return store.delete(file=a["file"], expected_hash=a.get("expected_hash", ""))
    if name == "memory_rename":
        return store.rename(old_file=a["old_file"], new_file=a["new_file"])
    if name == "memory_lint":
        r = store.lint()
        if a.get("near_duplicates"):
            r["near_duplicates"] = store.near_duplicates()
        return r
    raise me.MemoryError_(f"unknown tool {name}")


def text_result(obj, is_error=False):
    body = obj if isinstance(obj, str) else json.dumps(obj, indent=1, ensure_ascii=False)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def handle(store_factory, msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER, "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            return text_result(call(store_factory(), p.get("name", ""), p.get("arguments") or {}))
        except me.MemoryError_ as e:
            return text_result(f"error: {e}", True)
        except KeyError as e:
            return text_result(f"error: missing argument {e}", True)
        except Exception as e:  # never kill the server on a tool bug
            traceback.print_exc(file=sys.stderr)
            return text_result(f"internal error: {type(e).__name__}: {e}", True)
    if method in ("resources/list", "prompts/list"):
        return {method.split("/")[0]: []}
    raise LookupError(method)


def main():
    store_factory = me.store_from_env
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            res = handle(store_factory, msg)
            if res is None:
                continue
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": res}
        except LookupError as e:
            out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": f"method not found: {e}"}}
        except me.MemoryError_ as e:
            out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": str(e)}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
