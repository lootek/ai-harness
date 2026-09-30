# opencode reviewer twins

Subagent-format copies of the 18 review-board personas (`plugins/review-board/agents/`),
for opencode's `task` tool. Bodies are byte-identical to the claude originals; only the
frontmatter is converted: `description` kept, `mode: subagent`, and the claude `tools`
list becomes a `permission:` allow block — Read→read, Grep→grep, Glob→glob, Edit/Write→edit
(opencode's `write` tool is governed by the `edit` permission), Bash→bash, WebFetch→webfetch,
WebSearch→websearch; unknown tools are dropped. `model` is deliberately omitted, so every
twin inherits the opencode default model (change the default in opencode config, not here).

Regenerate after editing any persona: `python3 scripts/convert-agents-opencode.py`
(idempotent — second run is a zero diff). install.sh copies these to
`~/.config/opencode/agents/` (filename = agent name, e.g. `task` target `reviewer-golang`).
