#!/usr/bin/env python3
"""convert-agents-opencode.py — regenerate opencode subagent twins from the
claude reviewer personas.

Source of truth: plugins/review-board/agents/reviewer-*.md (claude format:
name / description / model / tools frontmatter). Output: agents/opencode/
reviewer-*.md (opencode format: description / mode / permission).

Conversion rules (see agents/opencode/README.md):
  - description text kept verbatim (re-emitted as a YAML double-quoted scalar)
  - mode: subagent (filename = agent name in opencode)
  - model is OMITTED so every twin inherits the opencode default model
  - tools list -> permission block, allow for each mapped tool:
        Read->read  Grep->grep  Glob->glob  Write->edit  Edit->edit
        WebFetch->webfetch  WebSearch->websearch  Bash->bash
    (opencode's `write` tool is governed by the `edit` permission, so claude's
    Write maps to `edit: allow`; unknown claude tools are dropped with a
    warning on stderr)
  - persona BODY is copied byte-identical (no persona references a claude-only
    tool in prose; WebFetch/WebSearch/Bash all exist in opencode lowercase)

Idempotent: running twice produces zero diff. Stdlib only.

Usage: python3 scripts/convert-agents-opencode.py [--src DIR] [--dst DIR]
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SRC = REPO / "plugins" / "review-board" / "agents"
DEFAULT_DST = REPO / "agents" / "opencode"

# claude tool -> opencode permission key (order = emission order)
TOOL_MAP = {
    "Read": "read",
    "Grep": "grep",
    "Glob": "glob",
    "Edit": "edit",
    "Write": "edit",      # opencode `write` tool is controlled by `edit`
    "Bash": "bash",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
}
PERMISSION_ORDER = ["read", "grep", "glob", "edit", "bash", "webfetch", "websearch"]


def yaml_quote(text: str) -> str:
    """Emit a YAML double-quoted scalar (JSON-style escaping is a subset)."""
    return json.dumps(text, ensure_ascii=False)


def convert(text: str, fname: str) -> str:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        sys.exit(f"error: {fname}: no frontmatter opening '---'")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        sys.exit(f"error: {fname}: no frontmatter closing '---'")

    description = None
    tools = []
    for line in lines[1:end]:
        if line.startswith("description:"):
            description = line[len("description:"):].strip()
            if len(description) >= 2 and description[0] == description[-1] == '"':
                description = description[1:-1]
        elif line.startswith("tools:"):
            tools = json.loads(line[len("tools:"):].strip())

    if description is None:
        sys.exit(f"error: {fname}: missing description")

    perms = []
    for tool in tools:
        perm = TOOL_MAP.get(tool)
        if perm is None:
            print(f"warning: {fname}: dropping unknown claude tool {tool!r}", file=sys.stderr)
            continue
        if perm not in perms:
            perms.append(perm)
    perms.sort(key=PERMISSION_ORDER.index)

    out = ["---",
           f"description: {yaml_quote(description)}",
           "mode: subagent",
           "permission:"]
    out += [f"  {p}: allow" for p in perms]
    out.append("---")
    # body byte-identical: everything after the closing --- line, separator
    # newline preserved exactly
    body = "\n".join(lines[end + 1:])
    return "\n".join(out) + "\n" + body


def main() -> None:
    args = sys.argv[1:]
    src, dst = DEFAULT_SRC, DEFAULT_DST
    for i, flag in enumerate(args):
        if flag == "--src":
            src = Path(args[i + 1])
        elif flag == "--dst":
            dst = Path(args[i + 1])
        else:
            sys.exit(f"unknown argument: {flag} (usage: convert-agents-opencode.py [--src DIR] [--dst DIR])")

    sources = sorted(src.glob("reviewer-*.md"))
    if not sources:
        sys.exit(f"error: no reviewer-*.md found under {src}")

    dst.mkdir(parents=True, exist_ok=True)
    for path in sources:
        target = dst / path.name
        target.write_text(convert(path.read_text(), path.name))
        print(f"converted  {path.relative_to(REPO)} -> {target.relative_to(REPO)}")

    # drop stale twins whose source persona no longer exists
    produced = {p.name for p in sources}
    for stale in dst.glob("reviewer-*.md"):
        if stale.name not in produced:
            stale.unlink()
            print(f"removed    {stale.relative_to(REPO)} (no source persona)")

    print(f"{len(sources)} twins written to {dst.relative_to(REPO)}")


if __name__ == "__main__":
    main()
