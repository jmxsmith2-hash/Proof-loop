"""Token and cost estimation. The heuristic and Ollama engines are free."""

from __future__ import annotations

from proofloop.core.config import Settings

CHARS_PER_TOKEN = 4.0
# Per call shape: (input tokens without images, output tokens, images)
CALL_SHAPES: dict[str, tuple[int, int, int]] = {
    "actor": (1400, 600, 1),    # ~900 of the input is the persona/rules text, cached after a session's first step
    "persona": (2500, 900, 0),
    "judge": (1800, 350, 2),
    "grouper": (1200, 200, 0),
    "reporter": (9000, 2500, 0),
}


def text_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def image_tokens(width: int, height: int) -> int:
    """Approximate vision tokens for one image (scaled to about 1.15 megapixels max)."""
    pixels = width * height
    max_pixels = 1_150_000
    if pixels > max_pixels:
        pixels = max_pixels
    return max(1, int(pixels / 750))


def price_for(settings: Settings, engine: str, model: str) -> tuple[float, float]:
    """Return (input, output) USD per million tokens; 0 for local engines."""
    provider = settings.models.get(engine)
    if provider is None or engine in ("heuristic", "ollama", "fake"):
        return 0.0, 0.0
    price = provider.prices.get(model)
    if price is None:
        raise ValueError(f"no price configured for {engine}:{model} in models.yaml")
    return float(price.get("input", 0.0)), float(price.get("output", 0.0))


CACHE_READ_FACTOR = 0.1    # prompt-cache reads cost about a tenth of normal input
CACHE_WRITE_FACTOR = 1.25  # 5-minute cache writes cost a quarter more than normal input


def call_cost(settings: Settings, engine: str, model: str, tokens_in: int, tokens_out: int, *,
              cache_read: int = 0, cache_write: int = 0) -> float:
    """`tokens_in` is the uncached input; cached input is passed separately (as the API reports it)."""
    pin, pout = price_for(settings, engine, model)
    cached = cache_read * CACHE_READ_FACTOR + cache_write * CACHE_WRITE_FACTOR
    return ((tokens_in + cached) * pin + tokens_out * pout) / 1_000_000
