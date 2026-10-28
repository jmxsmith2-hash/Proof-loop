"""Token and cost estimation. The heuristic and Ollama engines are free."""

from __future__ import annotations

from dataclasses import dataclass, field

from synthux.core.config import Settings

CHARS_PER_TOKEN = 4.0
# Per call shape: (input tokens without images, output tokens, images)
CALL_SHAPES: dict[str, tuple[int, int, int]] = {
    "actor": (1400, 300, 1),
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


def call_cost(settings: Settings, engine: str, model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = price_for(settings, engine, model)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


@dataclass
class CostEstimate:
    lines: list[dict] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return round(sum(line["usd"] for line in self.lines), 4)

    def add(self, role: str, engine: str, model: str, calls: int, tok_in: int, tok_out: int, usd: float) -> None:
        self.lines.append(
            {"role": role, "engine": engine, "model": model, "calls": calls,
             "input_tokens": tok_in, "output_tokens": tok_out, "usd": round(usd, 4)}
        )

    def describe(self) -> str:
        rows = [f"{'role':9} {'engine':10} {'model':22} {'calls':>6} {'in tok':>10} {'out tok':>9} {'USD':>8}"]
        for line in self.lines:
            rows.append(
                f"{line['role']:9} {line['engine']:10} {line['model'][:22]:22} {line['calls']:>6} "
                f"{line['input_tokens']:>10} {line['output_tokens']:>9} {line['usd']:>8.4f}"
            )
        rows.append(f"{'estimated total':>70} ${self.total_usd:.4f}")
        return "\n".join(rows)


def estimate_run(
    settings: Settings,
    *,
    sessions: int,
    avg_steps: float,
    viewport: tuple[int, int],
    issues_per_session: float = 3.0,
    persona_count: int = 0,
) -> CostEstimate:
    """Estimate the cost of a run before it starts."""
    est = CostEstimate()
    img = image_tokens(*viewport)
    calls = {
        "actor": int(sessions * avg_steps),
        "persona": persona_count + sessions,  # persona writing + one questionnaire per session
        "judge": int(sessions * issues_per_session * 1.5),  # review + replay check
        "grouper": int(sessions * issues_per_session * 0.5),
        "reporter": 1,
    }
    for role, n in calls.items():
        engine, model = settings.model_for(role)
        base_in, base_out, images = CALL_SHAPES[role]
        if engine == "heuristic":  # rule engine: no model calls at all
            est.add(role, engine, model, 0, 0, 0, 0.0)
            continue
        tok_in = n * (base_in + images * img)
        tok_out = n * base_out
        est.add(role, engine, model, n, tok_in, tok_out, call_cost(settings, engine, model, tok_in, tok_out))
    return est
