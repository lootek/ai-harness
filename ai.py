#!/usr/bin/env python3
"""ai/air — fzf-driven multi-CLI (claude/codex/opencode) provider+model wrapper.

Reads providers.yaml (co-located; falls back to ~/.ai-harness/providers.yaml),
lets you pick CLI → provider → model (fzf), exports the right env per CLI, and
execs the CLI binary:

    claude    ANTHROPIC_BASE_URL/AUTH_TOKEN/API_KEY assembly, slot routing,
              CLAUDE_CODE_MAX_OUTPUT_TOKENS (ported wholesale from the old
              claude-code-harness cc.py — semantics preserved)
    codex     <env_key>=<token> + a generated $CODEX_HOME/ai-<provider>.config.toml
              selected via `codex --profile ai-<provider>`
    opencode  <env_key>=<token> (absent for OAuth providers) + optional
              OPENCODE_CONFIG_CONTENT from the block's `config:` dict

Usage (source sh-aliases/ai from your shell rc, then):
    ai [cli-args...]        fresh session
    air [cli-args...]       resume most-recent session
    ai --cli codex --provider nvidia --model openai/gpt-oss-20b exec "hi"

Flags (consumed by this script, not forwarded):
    --resume           resume the most recent session; --continue is an alias
    --cli NAME         claude | codex | opencode (skip the cli fzf)
    --provider NAME    skip the provider fzf
    --model ID         skip the model fzf
    --refresh-models   standalone pre-step: force a clean-env `claude -p`
                        call against the real api.anthropic.com first, to
                        refresh claude code's own model-catalog cache before
                        the picker runs (see apply_claude_env / providers.yaml
                        for why that cache goes stale). Does not consume
                        --cli/--provider/--model; the normal flow continues
                        after it either way.
    --                 everything after is passed to the CLI verbatim
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("pyyaml missing — run: ~/.ai-harness/.venv/bin/pip install pyyaml")

# providers.yaml lives next to this script (repo root / ~/.ai-harness).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_CANDIDATES = [
    os.path.join(SCRIPT_DIR, "providers.yaml"),
    os.path.expanduser("~/.ai-harness/providers.yaml"),
]
CFG = next((p for p in CFG_CANDIDATES if os.path.isfile(p)), CFG_CANDIDATES[0])
SUPPORTED = ("claude", "codex", "opencode")
# All three CLIs are homebrew: claude and codex are casks, opencode a formula.
BREW = {
    "claude": "brew install --cask claude-code@latest",
    "codex": "brew install --cask codex",
    "opencode": "brew install opencode",
}


def load():
    with open(CFG) as f:
        return yaml.safe_load(f)["providers"]


def resolve_token(v):
    if not v:
        return ""
    if v.startswith("~") or v.startswith("/"):
        path = os.path.expanduser(v)
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            sys.exit(f"token file not found: {path}")
    return v


def fzf_pick(lines, prompt):
    if not lines:
        return ""
    r = subprocess.run(
        ["fzf", "--prompt", prompt, "--height=40%"],
        input="\n".join(sorted(set(lines))) + "\n",
        text=True,
        capture_output=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def list_model_ids(cfg):
    cmd = cfg.get("listcmd")
    if not cmd:
        return []
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def require_binary(cli):
    if not shutil.which(cli):
        sys.exit(f"{cli} CLI not found on PATH — {BREW[cli]}")


# ── claude path (ported wholesale from claude-code-harness tools/cc.py) ────

# claude code fills several internal slots (background title generation, the
# subagent model, and the opus/sonnet/haiku aliases) with *Anthropic* model
# names. On a BYO provider those names don't resolve, so the calls fail. A
# provider can either pin the slots itself via `env:` (z.ai does), or set
# route_slots_to_model: true to aim every slot at whatever model was picked —
# the right default when the catalogue is huge and no single model is special
# (the HF router). Explicit `env:` entries always win.
SLOT_VARS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def refresh_anthropic_catalog():
    # --refresh-models pre-step. Source #2 in providers.yaml (the model-catalog
    # cache) only self-refreshes off a call that actually reaches
    # api.anthropic.com; if `claude` is mostly invoked through this wrapper
    # with a BYO provider's ANTHROPIC_BASE_URL set, that never happens and the
    # cache can sit stale for days. Force one real hit here with a clean env —
    # strip every ANTHROPIC_* var so the call can't accidentally land on a
    # gateway (which wouldn't refresh anything and would burn a BYO token on a
    # throwaway prompt) and instead uses native `claude login` credentials.
    # Best-effort only: never aborts the picker, just warns on failure.
    env = dict(os.environ)
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        env.pop(k, None)
    try:
        r = subprocess.run(
            ["claude", "-p", "hi", "--output-format", "json"],
            env=env, capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        sys.stderr.write("⚠ --refresh-models: claude CLI not found on PATH — skipping\n")
        return
    except subprocess.TimeoutExpired:
        sys.stderr.write("⚠ --refresh-models: claude -p timed out after 60s — skipping\n")
        return
    if r.returncode == 0:
        sys.stderr.write("→ refreshed anthropic model catalog\n")
    else:
        tail = "\n   ".join((r.stderr or r.stdout or "").strip().splitlines()[-3:])
        sys.stderr.write(
            f"⚠ --refresh-models: claude exited {r.returncode}, catalog may still be stale"
            + (f"\n   {tail}" if tail else "") + "\n"
        )


def route_slots(cfg, model):
    if not cfg.get("route_slots_to_model"):
        return
    declared = cfg.get("env") or {}
    for var in SLOT_VARS:
        if var not in declared:
            os.environ[var] = model


def apply_claude_env(cfg, providers):
    # Clear ANTHROPIC_* + CLAUDE_CODE_MAX_OUTPUT_TOKENS so switching providers
    # doesn't leak env from a previous launch.
    #
    # claude code 2.1.212 honors the [1m] suffix for any provider (NPc()'s ub()
    # branch), not just api.anthropic.com. A model id like "glm-5.2[1m]"
    # gets a 1M context window; the suffix is stripped before the API call.
    # The suffix passes through this wrapper verbatim — see providers.yaml for
    # which models emit [1m] variants.
    # The clear covers every key any provider's claude block can set, not just
    # ANTHROPIC_*: otherwise a CLAUDE_CODE_* var declared by one provider
    # (z.ai's DISABLE_NONESSENTIAL_TRAFFIC, or a slot var) survives into the
    # next launch and silently points at a model the new provider doesn't serve.
    volatile = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", *SLOT_VARS}
    for pcfg in providers.values():
        volatile.update((((pcfg.get("clis") or {}).get("claude") or {}).get("env") or {}).keys())
    for k in list(os.environ):
        if k.startswith("ANTHROPIC_") or k in volatile:
            del os.environ[k]
    if not cfg.get("native"):
        os.environ["ANTHROPIC_BASE_URL"] = cfg.get("base_url", "")
        os.environ["ANTHROPIC_AUTH_TOKEN"] = resolve_token(cfg.get("token", ""))
        # Most gateways authenticate off ANTHROPIC_AUTH_TOKEN alone and want
        # ANTHROPIC_API_KEY empty. Some (the HF router) document setting both,
        # so a provider may declare api_key — same literal-or-path resolution
        # as token. "auth_token" is a shorthand for "reuse that value".
        api_key = cfg.get("api_key", "")
        if api_key == "auth_token":
            api_key = os.environ["ANTHROPIC_AUTH_TOKEN"]
        else:
            api_key = resolve_token(api_key)
        os.environ["ANTHROPIC_API_KEY"] = api_key
    mot = cfg.get("max_output_tokens")
    if mot:
        os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(mot)
    # Provider-declared extra env (BYO providers like z.ai that need
    # ANTHROPIC_DEFAULT_*_MODEL for claude code's background haiku calls,
    # API_TIMEOUT_MS, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, etc.).
    # Values are literal strings — no file-path resolution (only token
    # does that). Exported after the ANTHROPIC_* clear above, so keys
    # starting with ANTHROPIC_ survive. ANTHROPIC_MODEL is set later in
    # main() from the fzf pick, overriding anything here.
    for k, v in (cfg.get("env") or {}).items():
        os.environ[k] = str(v)


# ── codex / opencode shared helpers ─────────────────────────────────────────


def export_env_key(cfg, providers, cli):
    # codex/opencode read the API key from the env var named by env_key.
    # Sibling providers' keys from an earlier launch are cleared first so a
    # stale token never lingers in the env (and in ccprov output).
    keep = cfg.get("env_key")
    for pcfg in providers.values():
        ek = ((pcfg.get("clis") or {}).get(cli) or {}).get("env_key")
        if ek and ek != keep:
            os.environ.pop(ek, None)
    if keep:
        os.environ[keep] = resolve_token(cfg.get("token", ""))
    # The key must be exported BEFORE the model fzf: `opencode models <pid>`
    # only discovers a provider once its key is in the env.


def toml_str(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return f'"{toml_str(v)}"'


def write_codex_profile(provider, cfg):
    # $CODEX_HOME/ai-<provider>.config.toml, layered by `codex --profile
    # ai-<provider>`. The header carries a sha256 of the cli block; the file
    # is rewritten only when that hash changes, so hand edits survive an
    # unchanged block (and are wiped the moment it changes, as the header says).
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    os.makedirs(home, exist_ok=True)
    digest = hashlib.sha256(
        yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False).encode()
    ).hexdigest()[:12]
    path = os.path.join(home, f"ai-{provider}.config.toml")
    header = f"# Generated by ai-harness ai.py — {digest}; hand edits will be overwritten"
    try:
        with open(path) as f:
            if f.readline().strip() == header:
                return
    except FileNotFoundError:
        pass
    with open(path, "w") as f:
        f.write(header + "\n")
        f.write(f'model_provider = "ai-{provider}"\n')
        # Extra profile keys (dotted paths, scalars) — e.g. nvidia needs
        # web_search/features.multi_agent off because its /v1/responses
        # rejects those codex tool types. Emitted before any [table] header
        # so they stay at the config root.
        for k, v in sorted((cfg.get("toml") or {}).items()):
            f.write(f"{k} = {toml_value(v)}\n")
        f.write(f"[model_providers.ai-{provider}]\n")
        f.write(f'name = "{toml_str(provider)}"\n')
        f.write(f'base_url = "{toml_str(cfg.get("base_url", ""))}"\n')
        f.write(f'env_key = "{toml_str(cfg.get("env_key", ""))}"\n')


def transform_model(model, expr):
    # model_transform (optional): a sed -E expression applied to the picked id
    # before use — for providers whose catalogue ids differ from their API ids.
    if not expr:
        return model
    r = subprocess.run(["sed", "-E", expr], input=model + "\n", text=True, capture_output=True)
    return r.stdout.strip() if r.returncode == 0 else model


def prefixed_model(cfg, model):
    # opencode wants <provider_id>/<model>. Ids from `opencode models <pid>`
    # already carry the prefix; a foreign catalogue (or a manual --model) may
    # not — prepend iff it isn't there exactly once already.
    pid = cfg.get("provider_id") or ""
    if model and pid and not model.startswith(pid + "/"):
        return f"{pid}/{model}"
    return model


def apply_opencode_env(cfg, providers):
    export_env_key(cfg, providers, "opencode")
    # OPENCODE_CONFIG_CONTENT from a previous launch (zai's coding baseURL
    # override) would silently re-apply to this one — clear it unless re-set
    # right after. No env_key (anthropic OAuth via `opencode auth login`)
    # exports nothing and relies on opencode's stored credentials.
    os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
    if cfg.get("config"):
        os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg["config"])


def parse_args(argv):
    resume = False
    cli = None
    provider = None
    model = None
    refresh_models = False
    rest = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t in ("--resume", "--continue"):
            resume = True
            i += 1
        elif t == "--refresh-models":
            refresh_models = True
            i += 1
        elif t == "--cli" and i + 1 < len(argv):
            cli = argv[i + 1]
            i += 2
        elif t == "--provider" and i + 1 < len(argv):
            provider = argv[i + 1]
            i += 2
        elif t == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
        elif t == "--":
            rest = argv[i + 1:]
            break
        else:
            rest = argv[i:]
            break
    return resume, cli, provider, model, refresh_models, rest


def main():
    resume, cli, provider, model, refresh_models, rest = parse_args(sys.argv[1:])
    if refresh_models:
        refresh_anthropic_catalog()
    providers = load()
    if cli and cli not in SUPPORTED:
        sys.exit(f"unknown cli: {cli} (supported: {', '.join(SUPPORTED)})")
    if not cli:
        # clis present across providers — effectively the SUPPORTED three,
        # minus anything not yet configured; sorted for a stable fzf order.
        clis = sorted({c for p in providers.values() for c in (p.get("clis") or {})})
        cli = fzf_pick(clis, "cli> ")
        if not cli:
            sys.exit(130)
    require_binary(cli)
    cands = sorted(n for n, p in providers.items() if cli in (p.get("clis") or {}))
    if not cands:
        sys.exit(f"no provider serves cli '{cli}' in {CFG}")
    if not provider:
        provider = fzf_pick(cands, f"{cli}> ")
        if not provider:
            sys.exit(130)
    if provider not in providers:
        sys.exit(f"unknown provider: {provider}")
    clis = providers[provider].get("clis") or {}
    if cli not in clis:
        sys.exit(f"provider '{provider}' has no {cli} block")
    cfg = dict(clis[cli])
    # cli-level token wins over the provider-level one (schema: both allowed).
    if "token" not in cfg and providers[provider].get("token") is not None:
        cfg["token"] = providers[provider]["token"]

    if cli == "claude":
        apply_claude_env(cfg, providers)
        if not model:
            model = fzf_pick(list_model_ids(cfg), f"{provider}> ")
            if not model:
                sys.exit(130)
        os.environ["ANTHROPIC_MODEL"] = model
        route_slots(cfg, model)
        args = ["--model", model, *(["--continue"] if resume else []), *rest]
    elif cli == "codex":
        export_env_key(cfg, providers, "codex")
        if not model:
            model = fzf_pick(list_model_ids(cfg), f"{provider}> ")
            if not model:
                sys.exit(130)
        model = transform_model(model, cfg.get("model_transform"))
        write_codex_profile(provider, cfg)
        # resume maps to codex's own subcommand, inserted as the first
        # positional after the flags (`codex --profile p -m m resume --last`).
        args = ["--profile", f"ai-{provider}", "-m", model,
                *(["resume", "--last"] if resume else []), *rest]
    else:  # opencode
        apply_opencode_env(cfg, providers)
        if not model:
            model = fzf_pick(list_model_ids(cfg), f"{provider}> ")
        # No --model = opencode's configured default; an empty fzf pick is a
        # valid "just launch" there (claude/codex exit 130 instead).
        model = prefixed_model(cfg, model)
        args = [*(["--model", model] if model else []),
                *(["--continue"] if resume else []), *rest]
    sys.stderr.write(f"→ cli={cli} provider={provider} model={model}\n")
    os.execvpe(cli, [cli, *args], os.environ)


if __name__ == "__main__":
    main()
