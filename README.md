# ai-harness

Multi-CLI AI coding harness wrapper: one `ai`/`air` entry point that picks
CLI → provider → model for claude/codex/opencode/cursor, wraps every launch in
safety hooks, and carries a portable set of skills, plugins, and agents
shared across the CLIs.

## Layout

    ai.py               entry point: CLI → provider → model picker
    providers.yaml      provider/model catalog (claude/codex/opencode/cursor blocks)
    sh-aliases/         shell aliases (ai, air, ccprov)
    install.sh          deploy picker artifacts to ~/.ai-harness + ~/.zsh-aliases
    populate.sh         capture live picker artifacts back into the repo
    scripts/            repo tooling (leak-scan.sh)
    hooks/              engines: safe_command + payload_guard (safety),
                        log_commands + prompt_history (history), claude-only
                        export_session/flush_stale_dumps/session-env-check
    adapters/codex/     codex hook wiring: ASK→deny envelope adapter +
                        history adapters (spawn the engines)
    adapters/opencode/  opencode plugins: engine shim + history
    skills/             portable skills
    plugins/            claude plugin bundles (lootek marketplace)
    agents/             subagent definitions

## Usage

Source the alias file (`install.sh` copies it to `~/.zsh-aliases/ai`), then:

    ai                          fzf: pick CLI → provider → model, launch fresh
    air                         same, resuming the most recent session
    ai --provider zai -p "hi"   pick provider/model, pass -p to claude
    ai --cli codex --provider nvidia --model openai/gpt-oss-20b exec "say ok"
    ai --cli opencode --provider xai --model grok-4.7 run "say ok"
    ai --cli opencode --provider xai            interactive TUI (model via config)
    ai --cli cursor --provider cursor           pick a model from your Cursor account
    ai --refresh-models --provider anthropic   force-refresh claude's model
                                                catalog cache, then pick
    ccprov                      print the active provider env

`--cli`, `--provider`, `--model` skip the corresponding fzf step; everything
after `--` (or any non-picker flag) goes to the CLI verbatim. `--refresh-models`
is a standalone pre-step (doesn't consume the others) that forces a clean-env
`claude -p` call against the real api.anthropic.com before the picker runs —
use it when the anthropic provider's model list looks stale from running
mostly through BYO providers.

opencode 2.x launch shapes (the picker builds these; flags go after the
subcommand because the top-level `opencode` has no `--model`):

    ai … run "msg"   ->  opencode run --standalone --model <prov/model> "msg"
    ai … mini        ->  opencode mini --standalone --model <prov/model>
    ai …             ->  opencode --standalone            (TUI; model injected as
                         {"model": …} into OPENCODE_CONFIG_CONTENT, merged
                         with the provider's `config`)
    air …            ->  same, plus --continue
    other subcommands (models, auth, session …) pass through untouched.

`--standalone` is required: opencode 2 runs a shared background service
(`opencode serve --service`) that never sees the launching process's env, so
without it the exported provider key reaches only the client and the run
fails with `Model unavailable`. The model picker no longer shells out to
`opencode models <provider>` (v2 takes no provider argument and lists only
providers the service already knows): it filters `opencode models` by
`<provider_id>/` and falls back to opencode's cached models.dev catalogue
(`~/.cache/opencode/models.json`). ai/air refuse to launch opencode when
`opencode plugin list` does not show both ai-harness plugin ids
(`AI_ALLOW_UNGATED=1` overrides).

`cursor` (Cursor's terminal agent, binary `agent`) is a launcher-only CLI:
it runs `agent [--model <id>] [--continue]`. Auth is the Cursor account
(one-time `agent login`; an optional API key in `~/.secrets/cursor` is
exported as `CURSOR_API_KEY`). The model list comes from `agent models`, i.e.
whatever your Cursor account offers (Grok models included via the
subscription). There are no BYO providers or base URLs for it, and the safety
hooks/skills installed by `install.sh` do not apply to it. Install the CLI with
`curl https://cursor.com/install -fsS | bash`.

## Provider × CLI support

✅ live-verified or documented as noted, ❌ does not work (reason below), ⚠ untested, — not wired.

| provider    | claude        | codex                  | opencode                 |
|-------------|---------------|------------------------|--------------------------|
| anthropic   | ✅ native     | —                      | ✅                       |
| ollama      | ✅            | —                      | ✅                       |
| openrouter  | ✅ (documented) | ✅                   | ✅                       |
| poe         | ✅ (documented, untested: key invalid) | ⚠ (untested: key invalid) | ✅ (catalog) |
| xai         | ❌            | ✅                     | ✅                       |
| zai         | ✅            | ✅                     | ✅ (zai-coding-plan)     |
| nvidia      | —             | ✅ (3 models)          | ✅                       |
| huggingface | ✅            | —                      | ✅                       |
| cursor      | cursor CLI only (launcher, no BYO provider) | | |

Notes:

- xai/claude ❌: xAI's `/v1/messages` rejects Claude Code's requests (role:"system"
  message after the user turn; tools without `required`). Details in providers.yaml.
  xai/codex needs the `web_search`/`multi_agent` toml extras (already in providers.yaml).
- nvidia/codex is per-model: only `nemotron-3-super-120b-a12b`,
  `nemotron-3-ultra-550b-a55b` and `gpt-oss-20b` serve `/v1/responses`. GLM, kimi
  etc. work on NVIDIA only via opencode (chat-completions wire).
- zai: the Coding Plan weekly quota can block calls (quota, not a compat issue).
- cursor: launcher-only; models come from the Cursor account (Grok via the
  subscription). Named models need a paid Cursor plan; free plans are Auto-only.

## Status

Live — migrated 2026-10-01 from lootek/claude-code-harness (archived;
README there points here). Picker (ai/air) live on claude/codex/opencode;
safety hooks live on all three; command/prompt history live on
claude/opencode (codex awaits its one-time hook trust, then a live
re-check). Skills, agents, and the `lootek` plugin marketplace install
via install.sh (github source: lootek/ai-harness).

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
              (spawns ~/.ai-harness/hooks/safe_command.py per shell call)

Both adapters fail closed: malformed hook payload or an engine fault denies
the command. On codex, engine ASK verdicts are demoted to deny — codex exec
treats an "ask" as a failed hook and runs the command anyway (verified live,
codex-cli 0.157.1), so the demotion is what keeps the gate closed; the reason
keeps an `ASK-demotion (codex):` prefix so the judgment-call origin stays
visible. On opencode, ASK keeps its own label (`[ai-harness] ASK: …`).

opencode 2.x plugin API (the machine runs 2.0.20; 1.x plugins no longer
load): a plugin is a module with `export default { id, setup(ctx) }` (the
`effect` variant is the Effect-returning twin). The gate registers two
layers: `ctx.tool.hook("execute.before")` runs the engine and throws to block
(the tool is named `shell` in v2, `bash` in v1), and
`ctx.permission.hook("evaluate")` denies any tool-originated shell call the
first layer did not explicitly allow. A plugin the host rejects is silently
inactive, so `install.sh` runs `opencode plugin list` and exits non-zero
unless both `ai-harness.*` ids are listed (a rejected module shows up as a
bare path).

One shared audit trail: `~/.claude/hooks/safe_command_audit.jsonl`
(append-only JSONL; `$AI_SAFE_COMMAND_AUDIT` overrides the path — absolute).
Every deny/ask/allow-explicit is logged with the invoking CLI's session id.

codex hook trust: codex requires a one-time per-definition trust. After
install or any change to the wiring, run `codex` once and pick
"2. Trust all and continue" — until then the hook is silently skipped and
codex runs without this gate.

## History

Every CLI also logs what it did, appending to one shared per-alias history
in the session-context tree (`~/tmp/claude/ctx/<alias>/`, alias taken from
the launch directory):

    .commands_history.md    executed Bash calls (claude also: Edit/Write)
    .prompts_history.md     submitted prompts

The formats come from the claude hooks (`hooks/log_commands.py`,
`hooks/prompt_history.py` — ported verbatim from claude-code-harness) and
every CLI writes the same lines:

    claude    ~/.claude/hooks/{log_commands,prompt_history}.py
              (PostToolUse Bash/Edit/Write + UserPromptSubmit hooks)
    codex     ~/.codex/hooks.json ->
              ~/.ai-harness/adapters/codex/{log_commands,prompt_history}_codex.py
              (spawn the same engines; PostToolUse payload field names carry
              the Claude-Code-shape assumption — live-verify at cutover,
              after the hook trust above)
    opencode  ~/.config/opencode/plugins/ai-harness.history.ts
              (`session.hook("prompt")` for prompts, `tool.hook("execute.after")`
              for shell — both payloads verified live, opencode 2.0.20)

History is best-effort by design: any adapter fault exits silently — logging
must never break a session. The claude-only set (`export_session.py`,
`flush_stale_dumps.py`, `session-env-check.sh`) is deployed for cutover
completeness and stays claude-wired via `~/.claude/settings.json`.

Overriding a false positive: run the command by hand in your own shell (the
gate only sees CLI-issued commands), or refine the rule in
`hooks/safe_command.py` and rerun `install.sh`. On macOS, the deployed
engine copies are locked with `chflags uchg`; unlock with
`chflags nouchg <path>` before editing and re-apply afterwards.

## Plugins

Four Claude Code plugin bundles, installed from this repo via the `lootek`
marketplace (`.claude-plugin/marketplace.json` at the repo root):

    imagine         image generation via OpenRouter (list models, generate)
    mr-monitor      watch a GitLab MR pipeline + review threads until green
    mr-review       review a GitLab MR, post inline comments on approval
    review-board    multi-persona review board (18 reviewer subagents)

Each bundle under `plugins/<name>/` carries `.claude-plugin/plugin.json`
plus a byte-exact mirror of its skill from `skills/<name>/` (and `agents/`
for review-board); `scripts/sync-skills.sh --check` gates the drift.

`install.sh` (`--cli claude`) registers the marketplace and installs all
four as `<name>@lootek`. It registers the github source
(`claude plugin marketplace add lootek/ai-harness`) directly; only if that
add fails (e.g. no GitHub credentials for a private clone) does it fall
back to registering the local checkout as a directory-source marketplace —
same name, same plugins.

Locked-host note: `claude plugin install` records the plugin in
`enabledPlugins` inside `~/.claude/settings.json`; install.sh clears and
re-applies the macOS `uchg` flag around the plugin section (same
discipline as the hook files).

## Install

    git clone git@github.com:lootek/ai-harness.git && cd ai-harness
    bash install.sh [--cli claude|codex|opencode|all]
    # then: restart your shells (aliases are sourced at shell start) and
    # relaunch claude/codex/opencode so they pick up the hooks

Deploys `ai.py` + `providers.yaml` to `~/.ai-harness/`, the `ai`/`air`
aliases to `~/.zsh-aliases/ai`, bootstraps a pyyaml venv at
`~/.ai-harness/.venv`, and (`--cli`, default `all`) deploys the hooks: the
full claude set (engines + history + claude-only) to `~/.claude/hooks/`,
engines + history engines to `~/.ai-harness/hooks/`, the codex adapters and
rendered `~/.codex/hooks.json` (merged with an existing file: unknown entries
preserved, ours replaced), and the opencode plugins to
`~/.config/opencode/plugins/`. With `--cli claude` (or `all`) it also
registers the `lootek` plugin marketplace and installs the four plugin
bundles (see Plugins).

codex one-time hook trust: after install (and after ANY later change to
`~/.codex/hooks.json`), run `codex` once and pick "2. Trust all and
continue" — until then codex silently skips the hooks and runs without the
safety gate and history logging.

Dev tests: `cd hooks && ~/.ai-harness/.venv/bin/python -m pytest -q tests/`
(pytest is dev-only — installed into the venv by hand, not via install.sh).
