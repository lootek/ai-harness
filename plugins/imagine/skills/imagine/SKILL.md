---
name: imagine
description: "Generate images via OpenRouter. Trigger phrases: 'generate image', 'make an image', 'imagine', 'draw', 'render', 'list image models', 'image options'. Supports Grok Imagine, Nano Banana (Pro/2/Lite), FLUX.2 family, GPT-Image, Seedream, Recraft. Reads API key from ~/.secrets/openrouter. Saves images to the current working dir."
---

You help the user generate images via OpenRouter's Image API and list the available image models.

# Quick reference

Two modes:

1. **List mode** — when the user asks what's available ("list image models", "image options", "what can I use", "show me image models", "imagine list", "what image models", "or-image list"). Run `list_models.py` and pipe stdout back verbatim. Don't paraphrase the table.

2. **Generate mode** — when the user asks for an image with a model and prompt ("generate an image of X with Y", "imagine grok rendering Z", "make an image of ... using nano banana pro"). Resolve the model name, run `gen_image.py`, report the saved path + cost.

Scripts live in the `scripts/` subdirectory of this skill's base directory. The harness passes the base directory when the skill is invoked; substitute it for `<SKILL_DIR>` in the commands below (e.g. `<SKILL_DIR>/scripts/gen_image.py`).

# Friendly-name → model_id map

Resolve common names to OpenRouter model slugs before calling the script. If the user's model string already looks like `<owner>/<model>` (contains a slash), pass it through unchanged.

| Friendly name | Model id |
|---|---|
| `grok`, `grok-imagine` | `x-ai/grok-imagine-image-quality` |
| `nano-banana-pro`, `nbp`, `nano banana pro` | `google/gemini-3-pro-image` |
| `nano-banana-2`, `nb2`, `nano banana 2` | `google/gemini-3.1-flash-image` |
| `nano-banana-2-lite`, `nb2-lite` | `google/gemini-3.1-flash-lite-image` |
| `nano-banana`, `nb`, `nano banana` | `google/gemini-2.5-flash-image` |
| `flux-pro`, `flux.2-pro` | `black-forest-labs/flux.2-pro` |
| `flux-max`, `flux.2-max` | `black-forest-labs/flux.2-max` |
| `flux-flex`, `flux.2-flex` | `black-forest-labs/flux.2-flex` |
| `flux-klein`, `flux-fast`, `flux.2-klein-4b` | `black-forest-labs/flux.2-klein-4b` |
| `gpt-image`, `gpt-image-2` | `openai/gpt-image-2` |
| `gpt-image-1` | `openai/gpt-image-1` |
| `gpt-image-mini`, `gpt-image-1-mini` | `openai/gpt-image-1-mini` |
| `gpt-5-image` | `openai/gpt-5-image` |
| `gpt-5-image-mini` | `openai/gpt-5-image-mini` |
| `seedream`, `seedream-4.5` | `bytedance-seed/seedream-4.5` |
| `recraft-pro`, `recraft-v4.1-pro` | `recraft/recraft-v4.1-pro` |
| `recraft`, `recraft-v4.1` | `recraft/recraft-v4.1` |
| `recraft-v3` | `recraft/recraft-v3` |
| `mai`, `mai-image`, `mai-image-2.5` | `microsoft/mai-image-2.5` |
| `riverflow-fast` | `sourceful/riverflow-v2.5-fast` |
| `riverflow-pro` | `sourceful/riverflow-v2.5-pro` |

The script itself also resolves these via `friendly_names.py`, so `--model grok` works directly. The map above is for you to read intent and confirm before running.

# List mode

```bash
python3 <SKILL_DIR>/scripts/list_models.py [filter] [--detail <model_id>] [--refresh] [--json]
```

- No args → compact table of all 38+ models with pricing + supported params.
- `flux` (positional) → filter rows by substring on id or name.
- `--detail x-ai/grok-imagine-image-quality` → full provider list, every pricing row, every supported parameter with allowed values, and allowed passthrough parameters.
- `--refresh` → re-fetch live from `openrouter.ai/api/v1/images/models` + per-model `/endpoints` and update the bundled snapshot before printing. Use when the user asks for "current" or "latest" prices, or mentions a model not in the snapshot.
- `--json` → machine-readable output (don't use for user-facing replies).

Run the script and pipe stdout to the user verbatim. Do not summarize the table; the user wants to see the rows.

# Generate mode

```bash
python3 <SKILL_DIR>/scripts/gen_image.py \
  --model <model_id_or_friendly_name> \
  --prompt "<prompt text>" \
  [--resolution 512|1K|2K|4K] \
  [--aspect-ratio 1:1|16:9|9:16|4:3|3:4|auto|...] \
  [--quality auto|low|medium|high] \
  [--output-format png|jpeg|webp|svg] \
  [--background auto|transparent|opaque] \
  [--n 1-10] \
  [--reference <url_or_path>] [--reference ...] \
  [--provider-only <slug>] [--no-fallbacks] \
  [--out <filename>] [--outdir <dir>]
```

Defaults if the user doesn't specify:
- `--resolution`: don't pass (let provider pick its default, usually 1K).
- `--aspect-ratio`: don't pass.
- `--quality`: don't pass (only OpenAI models honor it).
- `--n`: 1.
- `--outdir`: current working dir (`$PWD`).
- `--out`: auto-generated as `img_<short-model>_<YYYYMMDD-HHMMSS>.<ext>`.

The script outputs one line per image:
```
saved /abs/path/to/img_grok-imagine-image-quality_20260717-141500.png | cost=$0.05 | tokens=1234
```

Always include that line in your reply to the user, plus a one-line description of what was generated. Don't paraphrase the path.

There's also a thin bash wrapper for users who want curl-only without Python deps:
```bash
MODEL=google/gemini-2.5-flash-image PROMPT="a ceramic mug" \
  RESOLUTION=1K ASPECT_RATIO=1:1 \
  <SKILL_DIR>/scripts/gen_image.sh
```
The bash version is less featured (no `--reference`, no media-type-aware extension, always PNG). Prefer the Python script unless the user explicitly asks for the bash one.

# Resolution and cost notes (mention only if relevant)

- **Gemini image models** (Nano Banana family) are billed per **output token**. A 2K image is roughly 5× the cost of 1K. Check `usage.cost` in the script output for the exact figure.
- **FLUX models** are billed per **megapixel**. `resolution: "2K"` ≈ 4 MP, so FLUX.2 Pro at 2K ≈ $0.12.
- **Grok Imagine** is billed per **image** plus per input reference image. $0.05–0.07/output image + $0.01/input reference.
- **GPT-Image** is billed per **output token**; `quality: high` costs more tokens than `low`.

# What to do when the user's request is ambiguous

- If model is unspecified, ask: "Which model? Common ones: grok, nano-banana-pro (nbp), nano-banana-2 (nb2), flux-pro, gpt-image. Run `list` to see all 38."
- If prompt is missing, ask for it. Don't make up a prompt.
- If resolution/aspect_ratio aren't specified, don't ask — just run with defaults. Only ask if the user clearly wants a specific size ("I need a 16:9 banner") but didn't say so explicitly.
- If the user asks for an image edit / image-to-image, use `--reference <path_or_url>` (Python script only). For multiple references, repeat the flag.

# Secrets

The scripts read the OpenRouter API key from `$OPENROUTER_API_KEY` first, then fall back to `~/.secrets/openrouter` (bare key, no shell-export wrapper). If neither is set, they exit with a clear error pointing to https://openrouter.ai/settings/keys. Don't print the key.

# Out of scope / future

- Poe image-gen integration (Poe's `/v1/images` endpoint, point-cost conversion) — not wired in v0.1.
- Streaming partial-image rendering (`--stream` flag exists but writes only the final image).
- Provider routing optimization beyond `--provider-only` and `--no-fallbacks`.
- Async batch generation (not in OpenRouter's image API today).