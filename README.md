# ai-harness

Multi-CLI AI coding harness wrapper: one `ai`/`air` entry point that picks
CLI → provider → model for claude/codex/opencode, wraps every launch in
safety hooks, and carries a portable set of skills, plugins, and agents
shared across the CLIs.

## Layout

Planned tree (being migrated in):

    ai.py               entry point: CLI → provider → model picker
    providers.yaml      provider/model catalog
    sh-aliases/         shell aliases (ai, air)
    hooks/              safety hooks shared across CLIs
    adapters/codex/     codex adapter
    adapters/opencode/  opencode adapter
    skills/             portable skills
    plugins/            plugins
    agents/             subagent definitions
    scripts/            repo tooling (leak-scan.sh, install, populate)

## Status

Migration in progress from lootek/claude-code-harness (2026-09-29).

## Install

Coming at cutover.
