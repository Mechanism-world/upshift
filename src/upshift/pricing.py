"""Exact token-cost accounting from recorded usage. Never estimated: tokens come from the
API's own usage fields in the rep records; only the $/token rates are external.

Anthropic rates verified 2026-09-01 (both Fables $10/$50 per MTok; cache reads are 10% of
the input rate on claude-fable-5 and 2.5% on claude-fable-5-1; no flex tier exists).

OpenAI rates verified 2026-08-27 against OpenAI's published pricing (gpt-5.6-sol promotional
pricing effective through 2026-11-21). USD per 1M tokens, standard sync tier. Flex and
Batch bill at 50% of standard; cached input tokens bill at 10% of the applicable input
rate (90% caching discount, which stacks with flex).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from upshift import recorder

# model prefix -> (input, output) at standard sync rates
RATES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-fable-5-1": (10.00, 50.00),
}

TIER_MULTIPLIER = {
    "openai": 1.0,
    "openai-flex": 0.5,
    "openai-batch": 0.5,
    "anthropic": 1.0,
}
CACHED_INPUT_FRACTION = 0.1
#: Per-model cache-read fraction of the input rate; models not listed use the default above.
MODEL_CACHED_INPUT_FRACTION: dict[str, float] = {
    "claude-fable-5": 0.1,
    "claude-fable-5-1": 0.025,
}


def _longest_prefix(model: str, table: dict[str, Any]) -> Any | None:
    """Longest matching model-id prefix, so a snapshot id and a more specific family id
    (claude-fable-5-1 vs claude-fable-5) both resolve correctly."""
    best: tuple[str, Any] | None = None
    for prefix, value in table.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, value)
    return best[1] if best else None


def _rate_for(model: str) -> tuple[float, float] | None:
    return _longest_prefix(model, RATES)


def cached_input_fraction(model: str) -> float:
    """Fraction of the input rate a cache-read token bills at."""
    found = _longest_prefix(model, MODEL_CACHED_INPUT_FRACTION)
    return CACHED_INPUT_FRACTION if found is None else found


def price(
    provider: str, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int
) -> float | None:
    """USD for the given recorded usage; None when the model has no known rate; 0.0 for sim."""
    if provider == "sim":
        return 0.0
    rates = _rate_for(model)
    tier = TIER_MULTIPLIER.get(provider)
    if rates is None or tier is None:
        return None
    in_rate, out_rate = rates[0] * tier, rates[1] * tier
    cached = min(cached_input_tokens, input_tokens)
    uncached = input_tokens - cached
    cached_fraction = cached_input_fraction(model)
    return (
        uncached * in_rate + cached * in_rate * cached_fraction + output_tokens * out_rate
    ) / 1_000_000


def run_cost(run_directory: str | Path) -> dict[str, Any]:
    """Sum recorded usage for one run and price it."""
    run_directory = Path(run_directory)
    manifest = recorder_manifest(run_directory)
    input_tokens = output_tokens = cached_tokens = reps = 0
    cases_dir = run_directory / "cases"
    if cases_dir.exists():
        for case_dir in sorted(cases_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            for record in recorder.load_case_reps(run_directory, case_dir.name):
                input_tokens += record.usage.get("input_tokens", 0)
                output_tokens += record.usage.get("output_tokens", 0)
                cached_tokens += record.usage.get("cached_input_tokens", 0)
                reps += 1
    provider = manifest.get("provider", "?")
    model = manifest.get("agent", {}).get("model_requested", "?")
    return {
        "run_id": manifest.get("run_id", run_directory.name),
        "provider": provider,
        "model": model,
        "reps": reps,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_tokens,
        "usd": price(provider, model, input_tokens, output_tokens, cached_tokens),
    }


def recorder_manifest(run_directory: Path) -> dict[str, Any]:
    path = run_directory / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}
