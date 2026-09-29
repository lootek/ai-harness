# ai-harness

Multi-CLI AI coding harness wrapper: one `ai`/`air` entry point that picks
CLI → provider → model for claude/codex/opencode, wraps every launch in
safety hooks, and carries a portable set of skills, plugins, and agents
shared across the CLIs.

## Layout

    ai.py               entry point: CLI → provider → model picker
    providers.yaml      provider/model catalog (claude/codex/opencode blocks)
    sh-aliases/         shell aliases (ai, air, ccprov)
    install.sh          deploy picker artifacts to ~/.ai-harness + ~/.zsh-aliases
    populate.sh         capture live picker artifacts back into the repo
    scripts/            repo tooling (leak-scan.sh)
    hooks/              safety hooks shared across CLIs        (planned)
    adapters/codex/     codex adapter                           (planned)
    adapters/opencode/  opencode adapter                        (planned)
    skills/             portable skills                         (planned)
    plugins/            plugins                                 (planned)
    agents/             subagent definitions                    (planned)

## Usage

Source the alias file (`install.sh` copies it to `~/.zsh-aliases/ai`), then:

    ai                          fzf: pick CLI → provider → model, launch fresh
    air                         same, resuming the most recent session
    ai --provider zai -p "hi"   pick provider/model, pass -p to claude
    ai --cli codex --provider nvidia --model openai/gpt-oss-20b exec "say ok"
    ai --cli opencode --provider zai run "say ok"
    ccprov                      print the active provider env

`--cli`, `--provider`, `--model` skip the corresponding fzf step; everything
after `--` (or any non-picker flag) goes to the CLI verbatim.

## Status

Migration in progress from lootek/claude-code-harness (2026-09-29).
Picker (ai/air) is live; hooks/plugins/agents/skills deploy comes at cutover —
until then the old `~/.claude` install keeps working untouched.

## Install

    bash install.sh

Deploys `ai.py` + `providers.yaml` to `~/.ai-harness/`, the `ai`/`air`
aliases to `~/.zsh-aliases/ai`, and bootstraps a pyyaml venv at
`~/.ai-harness/.venv`. Hooks/plugins install comes at cutover.
