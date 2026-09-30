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
    hooks/              safety engine (safe_command + payload_guard) + tests
    adapters/codex/     codex hook wiring + ASK→deny envelope adapter
    adapters/opencode/  opencode plugin shim for the engine
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
Picker (ai/air) is live; safety hooks are live on claude/codex/opencode.
Skills/plugins/agents deploy comes at cutover.

## Safety

Every CLI in this harness gates shell commands through the same engine:
`hooks/safe_command.py` (command-line rules: destructive fs ops, credential
exfil, macOS persistence, mutating API calls — DENY outright for the
catastrophic set, ASK for legitimate-but-risky) plus `hooks/payload_guard.py`
(content of indirectly-executed scripts: `python3 x.py`, `bash x.sh`,
heredocs — ASK only). The engine is owned once here and wired per CLI:

    claude    ~/.claude/hooks/safe_command.py        (PreToolUse hook)
    codex     ~/.codex/hooks.json -> ~/.ai-harness/adapters/codex/safe_command_codex.py
    opencode  ~/.config/opencode/plugins/ai-harness.safe-command.ts
              (spawns ~/.ai-harness/hooks/safe_command.py per bash call)

Both adapters fail closed: malformed hook payload or an engine fault denies
the command. On codex, engine ASK verdicts are demoted to deny — codex exec
treats an "ask" as a failed hook and runs the command anyway (verified live,
codex-cli 0.157.1), so the demotion is what keeps the gate closed; the reason
keeps an `ASK-demotion (codex):` prefix so the judgment-call origin stays
visible. On opencode, ASK keeps its own label (`[ai-harness] ASK: …`).

One shared audit trail: `~/.claude/hooks/safe_command_audit.jsonl`
(append-only JSONL; `$AI_SAFE_COMMAND_AUDIT` overrides the path — absolute).
Every deny/ask/allow-explicit is logged with the invoking CLI's session id.

codex hook trust: codex requires a one-time per-definition trust. After
install or any change to the wiring, run `codex` once and pick
"2. Trust all and continue" — until then the hook is silently skipped and
codex runs without this gate.

Overriding a false positive: run the command by hand in your own shell (the
gate only sees CLI-issued commands), or refine the rule in
`hooks/safe_command.py` and rerun `install.sh`. On macOS, the deployed
engine copies are locked with `chflags uchg`; unlock with
`chflags nouchg <path>` before editing and re-apply afterwards.

## Install

    bash install.sh [--cli claude|codex|opencode|all]

Deploys `ai.py` + `providers.yaml` to `~/.ai-harness/`, the `ai`/`air`
aliases to `~/.zsh-aliases/ai`, bootstraps a pyyaml venv at
`~/.ai-harness/.venv`, and (`--cli`, default `all`) deploys the safety hooks:
engines to `~/.claude/hooks/` + `~/.ai-harness/hooks/`, the codex adapter and
rendered `~/.codex/hooks.json` (merged with an existing file: unknown entries
preserved, ours replaced), and the opencode plugin to
`~/.config/opencode/plugins/`.

Dev tests: `cd hooks && ~/.ai-harness/.venv/bin/python -m pytest -q tests/`
(pytest is dev-only — installed into the venv by hand, not via install.sh).
