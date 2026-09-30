"""Friendly-name → OpenRouter image model_id map.

Used by gen_image.py so `--model grok` works without memorizing slugs.
Also duplicated (as text) in SKILL.md so Claude can resolve names inline.
"""

FRIENDLY_NAMES = {
    "grok": "x-ai/grok-imagine-image-quality",
    "grok-imagine": "x-ai/grok-imagine-image-quality",
    "grok-imagine-image-quality": "x-ai/grok-imagine-image-quality",
    "nano-banana-pro": "google/gemini-3-pro-image",
    "nbp": "google/gemini-3-pro-image",
    "nano banana pro": "google/gemini-3-pro-image",
    "nano-banana-2": "google/gemini-3.1-flash-image",
    "nb2": "google/gemini-3.1-flash-image",
    "nano banana 2": "google/gemini-3.1-flash-image",
    "nano-banana-2-lite": "google/gemini-3.1-flash-lite-image",
    "nb2-lite": "google/gemini-3.1-flash-lite-image",
    "nano-banana": "google/gemini-2.5-flash-image",
    "nb": "google/gemini-2.5-flash-image",
    "nano banana": "google/gemini-2.5-flash-image",
    "flux-pro": "black-forest-labs/flux.2-pro",
    "flux.2-pro": "black-forest-labs/flux.2-pro",
    "flux-max": "black-forest-labs/flux.2-max",
    "flux.2-max": "black-forest-labs/flux.2-max",
    "flux-flex": "black-forest-labs/flux.2-flex",
    "flux.2-flex": "black-forest-labs/flux.2-flex",
    "flux-klein": "black-forest-labs/flux.2-klein-4b",
    "flux-fast": "black-forest-labs/flux.2-klein-4b",
    "flux.2-klein-4b": "black-forest-labs/flux.2-klein-4b",
    "gpt-image": "openai/gpt-image-2",
    "gpt-image-2": "openai/gpt-image-2",
    "gpt-image-1": "openai/gpt-image-1",
    "gpt-image-mini": "openai/gpt-image-1-mini",
    "gpt-image-1-mini": "openai/gpt-image-1-mini",
    "gpt-5-image": "openai/gpt-5-image",
    "gpt-5-image-mini": "openai/gpt-5-image-mini",
    "seedream": "bytedance-seed/seedream-4.5",
    "seedream-4.5": "bytedance-seed/seedream-4.5",
    "recraft-pro": "recraft/recraft-v4.1-pro",
    "recraft": "recraft/recraft-v4.1",
    "recraft-v4.1-pro": "recraft/recraft-v4.1-pro",
    "recraft-v4.1": "recraft/recraft-v4.1",
    "recraft-v3": "recraft/recraft-v3",
    "mai": "microsoft/mai-image-2.5",
    "mai-image": "microsoft/mai-image-2.5",
    "mai-image-2.5": "microsoft/mai-image-2.5",
    "riverflow-fast": "sourceful/riverflow-v2.5-fast",
    "riverflow-pro": "sourceful/riverflow-v2.5-pro",
}


def resolve(name: str) -> str:
    """Resolve a friendly name or raw model_id to a canonical model_id.

    Returns the input unchanged if it already looks like a `<owner>/<model>` slug
    and isn't in the friendly map.
    """
    if not name:
        return name
    key = name.strip().lower()
    if key in FRIENDLY_NAMES:
        return FRIENDLY_NAMES[key]
    return name


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(resolve(sys.argv[1]))
    else:
        for k, v in sorted(FRIENDLY_NAMES.items()):
            print(f"{k:30s} → {v}")