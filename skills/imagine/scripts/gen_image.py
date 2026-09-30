#!/usr/bin/env python3
"""Generate an image via OpenRouter's Image API and save it to the working dir.

Usage:
  gen_image.py --model grok --prompt "a red panda astronaut" --resolution 1K
  gen_image.py --model nbp --prompt "watercolor of a harbor" --resolution 2K --aspect-ratio 16:9
  gen_image.py --model gpt-image-2 --prompt "make this watercolor" --reference ./img.png

Output: one line per image:
  saved <abspath> | cost=$<usage.cost> | tokens=<usage.total_tokens>

API key resolution order: $OPENROUTER_API_KEY env var, then ~/.secrets/openrouter (bare key).
"""
import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from friendly_names import resolve  # noqa: E402

API_URL = "https://openrouter.ai/api/v1/images"
SECRETS_PATH = os.path.expanduser("~/.secrets/openrouter")


def load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(SECRETS_PATH, "r") as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    sys.exit(f"ERROR: no OpenRouter API key. Set $OPENROUTER_API_KEY or write the key to {SECRETS_PATH}. "
             f"Get one at https://openrouter.ai/settings/keys.")


def encode_reference(path: str) -> dict:
    """Encode a local file path or pass through a URL as an input_reference."""
    if path.startswith("http://") or path.startswith("https://"):
        return {"type": "image_url", "image_url": {"url": path}}
    p = Path(path).expanduser()
    if not p.exists():
        sys.exit(f"ERROR: reference file not found: {path}")
    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def short_model_name(model_id: str) -> str:
    """Short slug for filenames: 'x-ai/grok-imagine-image-quality' → 'grok-imagine-image-quality'."""
    return model_id.split("/")[-1]


def ext_from_media_type(media_type: str) -> str:
    if not media_type:
        return "png"
    # e.g. 'image/png' → 'png', 'image/svg+xml' → 'svg'
    sub = media_type.split("/")[-1]
    return sub.replace("+xml", "")


def build_body(args) -> dict:
    body = {"model": args.model, "prompt": args.prompt}
    if args.resolution:
        body["resolution"] = args.resolution
    if args.aspect_ratio:
        body["aspect_ratio"] = args.aspect_ratio
    if args.quality:
        body["quality"] = args.quality
    if args.output_format:
        body["output_format"] = args.output_format
    if args.background:
        body["background"] = args.background
    if args.n and args.n > 1:
        body["n"] = args.n
    if args.reference:
        body["input_references"] = [encode_reference(r) for r in args.reference]
    provider = {}
    if args.provider_only:
        provider["only"] = args.provider_only
    if args.no_fallbacks:
        provider["allow_fallbacks"] = False
    if provider:
        body["provider"] = provider
    if args.stream:
        body["stream"] = True
    return body


def do_request(body: dict, key: str) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        sys.exit(f"ERROR: HTTP {e.code} from OpenRouter:\n{err_body}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: network failure: {e}")


def save_images(resp: dict, args) -> None:
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    short = short_model_name(args.model)
    usage = resp.get("usage", {}) or {}
    cost = usage.get("cost")
    total_tokens = usage.get("total_tokens")
    data = resp.get("data", []) or []
    if not data:
        sys.exit(f"ERROR: no image data in response: {json.dumps(resp)[:400]}")
    for i, item in enumerate(data):
        b64 = item.get("b64_json")
        if not b64:
            sys.stderr.write(f"WARNING: data[{i}] has no b64_json: {item}\n")
            continue
        ext = ext_from_media_type(item.get("media_type", "image/png"))
        if args.out:
            path = outdir / args.out if not Path(args.out).is_absolute() else Path(args.out)
            if len(data) > 1:
                path = path.with_name(f"{path.stem}_{i+1}{path.suffix or '.'+ext}")
            else:
                if not path.suffix:
                    path = path.with_suffix(f".{ext}")
        else:
            suffix = f"_{i+1}" if len(data) > 1 else ""
            path = outdir / f"img_{short}_{ts}{suffix}.{ext}"
        path.write_bytes(base64.b64decode(b64))
        cost_str = f"${cost}" if cost is not None else "?"
        tok_str = str(total_tokens) if total_tokens is not None else "?"
        print(f"saved {path} | cost={cost_str} | tokens={tok_str}")


def main():
    p = argparse.ArgumentParser(description="Generate an image via OpenRouter.")
    p.add_argument("--model", required=True, help="Model id or friendly name (grok, nbp, nb2, flux-pro, ...)")
    p.add_argument("--prompt", required=True, help="Text prompt for the image")
    p.add_argument("--resolution", choices=["512", "1K", "2K", "4K"], help="Output resolution tier")
    p.add_argument("--aspect-ratio", help="Aspect ratio (1:1, 16:9, 9:16, 4:3, 3:4, auto, ...)")
    p.add_argument("--quality", choices=["auto", "low", "medium", "high"], help="Quality tier (OpenAI models)")
    p.add_argument("--output-format", choices=["png", "jpeg", "webp", "svg"], help="Output format")
    p.add_argument("--background", choices=["auto", "transparent", "opaque"], help="Background mode")
    p.add_argument("--n", type=int, default=1, help="Number of images (1-10, provider-dependent)")
    p.add_argument("--reference", action="append", default=[], help="Reference image (URL or local path); repeatable")
    p.add_argument("--provider-only", action="append", default=[], help="Restrict to provider slug(s); repeatable")
    p.add_argument("--no-fallbacks", action="store_true", help="Disable provider fallbacks")
    p.add_argument("--stream", action="store_true", help="Request SSE streaming (final image only)")
    p.add_argument("--out", help="Explicit output filename (default: img_<short>_<ts>.<ext>)")
    p.add_argument("--outdir", default=os.getcwd(), help="Output directory (default: $PWD)")
    args = p.parse_args()

    args.model = resolve(args.model)
    key = load_key()
    body = build_body(args)
    resp = do_request(body, key)
    save_images(resp, args)


if __name__ == "__main__":
    main()