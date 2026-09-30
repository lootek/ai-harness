#!/usr/bin/env bash
# Thin bash wrapper for OpenRouter image generation.
# Less featured than gen_image.py (no --reference, no media-type-aware extension, no streaming).
# Reads API key from $OPENROUTER_API_KEY or ~/.secrets/openrouter (bare key).
#
# Env vars:
#   MODEL          (required) OpenRouter model id, e.g. google/gemini-2.5-flash-image
#   PROMPT         (required) text prompt
#   RESOLUTION     (optional) 512 | 1K | 2K | 4K
#   ASPECT_RATIO   (optional) e.g. 1:1, 16:9, 9:16
#   QUALITY        (optional) auto | low | medium | high
#   N              (optional) number of images (default 1)
#   OUTDIR         (optional) output directory (default $PWD)
#   OUT            (optional) explicit filename (default auto-generated)
#
# Requires: curl, jq, base64.

set -euo pipefail

API_URL="https://openrouter.ai/api/v1/images"
SECRETS_PATH="$HOME/.secrets/openrouter"

if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  KEY="$OPENROUTER_API_KEY"
elif [[ -f "$SECRETS_PATH" ]]; then
  KEY="$(tr -d '[:space:]' < "$SECRETS_PATH")"
else
  echo "ERROR: no OpenRouter API key. Set \$OPENROUTER_API_KEY or write the key to $SECRETS_PATH." >&2
  echo "Get one at https://openrouter.ai/settings/keys" >&2
  exit 1
fi

if [[ -z "${MODEL:-}" || -z "${PROMPT:-}" ]]; then
  echo "Usage: MODEL=<model_id> PROMPT=\"<text>\" [RESOLUTION=1K] [ASPECT_RATIO=16:9] [QUALITY=high] [N=1] $0" >&2
  exit 1
fi

# Build JSON body with jq (drops null fields automatically).
BODY=$(jq -n --arg m "$MODEL" --arg p "$PROMPT" \
  --arg r "${RESOLUTION:-}" --arg ar "${ASPECT_RATIO:-}" --arg q "${QUALITY:-}" \
  --argjson n "${N:-1}" \
  '{model: $m, prompt: $p}
   + (if $r == "" then {} else {resolution: $r} end)
   + (if $ar == "" then {} else {aspect_ratio: $ar} end)
   + (if $q == "" then {} else {quality: $q} end)
   + (if $n == 1 then {} else {n: $n} end)')

OUTDIR="${OUTDIR:-$PWD}"
mkdir -p "$OUTDIR"
SHORT="${MODEL##*/}"
TS=$(date +%Y%m%d-%H%M%S)
OUT_FILE="${OUT:-img_${SHORT}_${TS}.png}"
OUT_PATH="$OUTDIR/$OUT_FILE"

RESP=$(curl -sS "$API_URL" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "$BODY")

# Save image (always PNG in this simplified wrapper).
echo "$RESP" | jq -r '.data[0].b64_json' | base64 -d > "$OUT_PATH"

# Report path + cost + tokens.
COST=$(echo "$RESP" | jq -r '.usage.cost // "?"')
TOKS=$(echo "$RESP" | jq -r '.usage.total_tokens // "?"')
echo "saved $(cd "$OUTDIR" && pwd)/$OUT_FILE | cost=\$$COST | tokens=$TOKS"