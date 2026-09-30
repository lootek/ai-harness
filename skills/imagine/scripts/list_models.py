#!/usr/bin/env python3
"""List OpenRouter image models with pricing and supported parameters.

Uses a bundled snapshot at ../data/{models.json, endpoints.json} (inside the
skill dir). Pass --refresh to re-fetch live from openrouter.ai and update the
snapshot.

Usage:
  list_models.py                    # compact table of all models
  list_models.py flux               # filter by substring (id or name)
  list_models.py --detail x-ai/grok-imagine-image-quality
  list_models.py --refresh          # re-fetch live catalog + endpoints
  list_models.py --json             # machine-readable output
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

# ai-harness layout change: data/ moved from the plugin root (../../../data)
# into the skill dir (../data) so the skill is self-contained everywhere it is
# synced (claude/opencode/codex skill dirs). The old path stays as a fallback.
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = next((d for d in (
    os.path.normpath(os.path.join(_HERE, "..", "data")),            # canonical skill layout
    os.path.normpath(os.path.join(_HERE, "..", "..", "..", "data")),  # legacy plugin layout
) if os.path.isdir(d)), os.path.normpath(os.path.join(_HERE, "..", "data")))
MODELS_PATH = os.path.join(DATA_DIR, "models.json")
ENDPOINTS_PATH = os.path.join(DATA_DIR, "endpoints.json")
API_BASE = "https://openrouter.ai/api/v1/images/models"


def load_snapshot():
    with open(MODELS_PATH) as f:
        raw = json.load(f)
    models = raw.get("data", raw) if isinstance(raw, dict) else raw
    with open(ENDPOINTS_PATH) as f:
        endpoints = json.load(f)
    return models, endpoints


def refresh_snapshot() -> tuple:
    """Fetch live catalog + per-model endpoints and overwrite the bundled snapshot."""
    print("Fetching live catalog...", file=sys.stderr)
    with urllib.request.urlopen(API_BASE, timeout=30) as r:
        catalog = json.loads(r.read())
    models = catalog.get("data", catalog) if isinstance(catalog, dict) else catalog
    endpoints = {}
    for i, m in enumerate(models, 1):
        mid = m["id"]
        print(f"  [{i}/{len(models)}] {mid}", file=sys.stderr, end="\r")
        url = f"{API_BASE}/{mid}/endpoints"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"\n  WARN: {mid} endpoints HTTP {e.code}", file=sys.stderr)
            continue
        eps = d.get("endpoints", []) or []
        if not eps:
            continue
        providers = []
        pricing = []
        sp = {}
        passthrough = set()
        for ep in eps:
            providers.append({
                "provider_name": ep.get("provider_name", ""),
                "provider_slug": ep.get("provider_slug", ""),
                "provider_tag": ep.get("provider_tag", ""),
                "supports_streaming": ep.get("supports_streaming", False),
            })
            for p in (ep.get("pricing", []) or []):
                pricing.append({
                    "provider_slug": ep.get("provider_slug", ""),
                    "billable": p.get("billable"),
                    "unit": p.get("unit"),
                    "cost_usd": p.get("cost_usd"),
                })
            for k, v in (ep.get("supported_parameters", {}) or {}).items():
                if k not in sp:
                    sp[k] = v
            for x in (ep.get("allowed_passthrough_parameters", []) or []):
                passthrough.add(x)
        endpoints[mid] = {
            "providers": providers,
            "pricing": pricing,
            "supported_parameters": sp,
            "allowed_passthrough_parameters": sorted(passthrough),
        }
    print(f"\nDone. {len(models)} models, {len(endpoints)} with endpoints.", file=sys.stderr)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MODELS_PATH, "w") as f:
        json.dump(models, f, indent=2)
    with open(ENDPOINTS_PATH, "w") as f:
        json.dump(endpoints, f, indent=2)
    return models, endpoints


def fmt_pricing(pricing: list) -> str:
    """One-line pricing summary, e.g. '$0.03/MP' or '$0.04/img' or '$0.00003/tok'."""
    if not pricing:
        return "-"
    # Group by unit, take min cost per unit
    by_unit = {}
    for p in pricing:
        u = p.get("unit", "?")
        c = p.get("cost_usd")
        if c is None:
            continue
        if u not in by_unit or c < by_unit[u]:
            by_unit[u] = c
    parts = []
    for u, c in sorted(by_unit.items()):
        if u == "megapixel":
            parts.append(f"${c}/MP")
        elif u == "image":
            parts.append(f"${c}/img")
        elif u == "token":
            parts.append(f"${c}/tok")
        else:
            parts.append(f"${c}/{u}")
    return ", ".join(parts) or "-"


def fmt_params(sp: dict) -> str:
    """One-line supported-params summary."""
    if not sp:
        return "-"
    bits = []
    if "resolution" in sp:
        vals = sp["resolution"].get("values", [])
        bits.append(f"res={','.join(vals)}")
    if "aspect_ratio" in sp:
        bits.append("ar")
    if "quality" in sp:
        bits.append("quality")
    if "output_format" in sp:
        bits.append("fmt")
    if "background" in sp:
        bits.append("bg")
    if "n" in sp:
        n = sp["n"]
        bits.append(f"n={n.get('min','?')}-{n.get('max','?')}")
    if "input_references" in sp:
        ir = sp["input_references"]
        bits.append(f"refs≤{ir.get('max','?')}")
    if "seed" in sp:
        bits.append("seed")
    return "; ".join(bits) if bits else "-"


def print_table(models, endpoints, filter_str=None):
    rows = []
    for m in models:
        mid = m["id"]
        name = m.get("name", "")
        if filter_str and filter_str.lower() not in mid.lower() and filter_str.lower() not in name.lower():
            continue
        ep = endpoints.get(mid, {})
        provs = ep.get("providers", [])
        prov_names = sorted({p["provider_name"] for p in provs if p.get("provider_name")})
        prov_str = ",".join(prov_names) if prov_names else "-"
        pricing = fmt_pricing(ep.get("pricing", []))
        params = fmt_params(ep.get("supported_parameters", {}))
        rows.append((mid, name, prov_str, pricing, params))
    if not rows:
        print(f"No models match filter '{filter_str}'.")
        return
    # column widths
    cw = [max(len(r[i]) for r in rows) for i in range(5)]
    cw[0] = max(cw[0], len("MODEL_ID"))
    cw[1] = max(cw[1], len("NAME"))
    cw[2] = max(cw[2], len("PROVIDER"))
    cw[3] = max(cw[3], len("PRICING"))
    cw[4] = max(cw[4], len("PARAMS"))
    fmt = f"{{:<{cw[0]}}}  {{:<{cw[1]}}}  {{:<{cw[2]}}}  {{:<{cw[3]}}}  {{:<{cw[4]}}}"
    print(fmt.format("MODEL_ID", "NAME", "PROVIDER", "PRICING", "PARAMS"))
    print(fmt.format("-" * cw[0], "-" * cw[1], "-" * cw[2], "-" * cw[3], "-" * cw[4]))
    for r in rows:
        print(fmt.format(*r))


def print_detail(models, endpoints, model_id: str):
    m = next((x for x in models if x["id"] == model_id), None)
    if not m:
        print(f"Model not found: {model_id}")
        sys.exit(1)
    ep = endpoints.get(model_id, {})
    print(f"id: {m['id']}")
    print(f"name: {m.get('name','')}")
    print(f"description: {m.get('description','')[:300]}")
    print()
    print("providers:")
    for p in ep.get("providers", []):
        print(f"  - {p['provider_name']} (slug={p['provider_slug']}, tag={p['provider_tag']}, stream={p['supports_streaming']})")
    print()
    print("pricing:")
    for p in ep.get("pricing", []):
        print(f"  - [{p['provider_slug']}] {p['billable']} @ ${p['cost_usd']}/{p['unit']}")
    print()
    print("supported_parameters:")
    sp = ep.get("supported_parameters", {})
    if not sp:
        print("  (none)")
    for k, v in sp.items():
        if isinstance(v, dict):
            if "values" in v:
                print(f"  {k}: enum={v['values']}")
            elif "min" in v or "max" in v:
                print(f"  {k}: range min={v.get('min')} max={v.get('max')}")
            else:
                print(f"  {k}: {v}")
        else:
            print(f"  {k}: {v}")
    print()
    pt = ep.get("allowed_passthrough_parameters", [])
    print(f"allowed_passthrough_parameters: {pt if pt else '(none)'}")


def main():
    p = argparse.ArgumentParser(description="List OpenRouter image models + pricing + params.")
    p.add_argument("filter", nargs="?", default=None, help="Substring filter on model id or name")
    p.add_argument("--detail", help="Print full detail for one model id")
    p.add_argument("--refresh", action="store_true", help="Re-fetch live catalog + endpoints before printing")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = p.parse_args()

    if args.refresh:
        models, endpoints = refresh_snapshot()
    else:
        models, endpoints = load_snapshot()

    if args.json:
        out = {"models": models, "endpoints": endpoints}
        print(json.dumps(out, indent=2))
        return

    if args.detail:
        print_detail(models, endpoints, args.detail)
    else:
        print_table(models, endpoints, args.filter)


if __name__ == "__main__":
    main()