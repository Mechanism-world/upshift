"""Exact token-cost accounting from recorded usage. Never estimated: tokens come from the
API's own usage fields in the rep records; only the $/token rates are external.

Anthropic rates verified 2026-09-01 (both Fables $10/$50 per MTok; cache reads are 10% of
the input rate on claude-fable-5 and 2.5% on claude-fable-5-1; a 5-minute cache write is
1.25x the input rate, i.e. $12.50/MTok on both; no flex tier exists). claude-sonnet-4-5
verified 2026-09-04 against claude.com/pricing (Legacy models): $3/$15 per MTok, cache
reads $0.30/MTok. claude-opus-4-8 verified 2026-09-04 against the published Anthropic
model/pricing reference: $5/$25 per MTok, cache reads at the default 10% ($0.50/MTok).
claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-6, claude-sonnet-5 and claude-opus-5
verified 2026-09-05 against the same published Anthropic model/pricing reference (see RATES
below); all take the default 10% cache-read fraction — that reference names claude-fable-5-1
(0.025x, $0.25/MTok) as the only exception, and states it is "half of Claude Opus 5's" rate,
which pins Opus 5 cache reads at $0.50/MTok = 0.1x. claude-opus-4-5 is deliberately absent:
the reference lists it as an active legacy model but carries no per-MTok rate for it, and a
guessed rate is worse than an honest "unknown rate".

OpenAI rates verified 2026-08-27 against OpenAI's published pricing (gpt-5.6-sol promotional
pricing effective through 2026-11-21), the whole gpt-5.6 family re-verified 2026-09-03, and
gpt-4o-mini and gpt-4.1-nano verified 2026-09-05, all against
https://developers.openai.com/api/docs/pricing. USD per 1M tokens, standard sync tier. Flex
and Batch bill at 50% of standard; cached input tokens bill at 10% of the applicable input
rate on the gpt-5 era models (90% caching discount, which stacks with flex) — the two
pre-gpt-5 models carry their own published cached fractions instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from upshift import recorder

# model prefix -> (input, output) at standard sync rates
RATES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.00, 30.00),
    # gpt-5.6 family: https://developers.openai.com/api/docs/pricing, fetched 2026-09-03.
    # Standard tier, USD per 1M tokens. The published flex and batch rows are exactly half
    # of these, and every published cached-input rate is exactly 10% of the model's input
    # rate, so TIER_MULTIPLIER and CACHED_INPUT_FRACTION reproduce the table as printed.
    # (The page also lists a fast-mode tier at 2x standard; upshift has no fast-mode
    # provider, so no multiplier is recorded for it.)
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    # gpt-5.2 family and gpt-5-mini: https://developers.openai.com/api/docs/pricing,
    # fetched 2026-09-03. Standard tier, USD per 1M tokens; the published flex and batch
    # rows are exactly half of these and the published cached-input rows exactly 10% of
    # input, so the existing multipliers reproduce the table as printed. gpt-5.2-pro gets
    # its own entry because it is a separate published row at 12x gpt-5.2: without it,
    # longest-prefix matching would price a -pro run as gpt-5.2 and understate the spend
    # twelvefold. (The pro row publishes no cached-input price — it is "-" in the table.)
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.2-pro": (21.00, 168.00),
    "gpt-5-mini": (0.25, 2.00),
    # Pre-gpt-5 OpenAI models a target repo still ships as its configured default, which a
    # migration case therefore runs as the BASELINE leg. $0.15/$0.60 and $0.10/$0.40 per
    # MTok, per https://developers.openai.com/api/docs/pricing (Standard tier), verified
    # 2026-09-05. Their cached-input rates are NOT the 10% default (see
    # MODEL_CACHED_INPUT_FRACTION): $0.075 = 50% of input on gpt-4o-mini, $0.025 = 25% on
    # gpt-4.1-nano.
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "claude-fable-5": (10.00, 50.00),
    "claude-fable-5-1": (10.00, 50.00),
    # Legacy Anthropic model still served, and still the model some target harnesses pin.
    # Cache reads are $0.30/MTok = 10% of input, i.e. CACHED_INPUT_FRACTION; no override.
    "claude-sonnet-4-5": (3.00, 15.00),
    # Opus-tier model the Anthropic rescue track runs against. $5/$25 per MTok, verified
    # 2026-09-04 against the published Anthropic model/pricing reference (Claude Opus 4.8;
    # Claude Opus 5 is described there as "a drop-in upgrade at Opus 4.8's pricing
    # ($5/$25 per MTok)"). Cache reads take the documented default 0.1x of input
    # ($0.50/MTok) — the reference names 0.025x as the claude-fable-5-1 exception only —
    # so no MODEL_CACHED_INPUT_FRACTION override; 5-minute cache writes are the standard
    # 1.25x ($6.25/MTok).
    "claude-opus-4-8": (5.00, 25.00),
    # The Anthropic rescue track's scope extension runs its baselines on the models below —
    # the near side of the 4.7+ wall — so every one of them must price or the baseline leg of
    # an `upgrade` reports "unknown rate" and freezes the spend ledger. Rates verified
    # 2026-09-05 against the published Anthropic model/pricing reference. None of them takes
    # a MODEL_CACHED_INPUT_FRACTION override: that reference names claude-fable-5-1 (0.025x)
    # as its sole exception, so cache reads here are the default 0.1x of input and 5-minute
    # cache writes the standard 1.25x.
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
}

TIER_MULTIPLIER = {
    "openai": 1.0,
    "openai-flex": 0.5,
    "openai-batch": 0.5,
    "anthropic": 1.0,
}
CACHED_INPUT_FRACTION = 0.1
#: A 5-minute cache WRITE bills at 1.25x the input rate ($12.50/MTok on both Fables).
CACHE_WRITE_MULTIPLIER = 1.25
#: Per-model cache-read fraction of the input rate; models not listed use the default above.
MODEL_CACHED_INPUT_FRACTION: dict[str, float] = {
    "claude-fable-5": 0.1,
    "claude-fable-5-1": 0.025,
    # The 90% caching discount is a gpt-5-era rate. The published cached-input prices for
    # these two are $0.075 of $0.15 and $0.025 of $0.10, so the default 0.1 would
    # UNDER-report a cache-heavy baseline leg by 4-8x on the cached part.
    "gpt-4o-mini": 0.5,
    "gpt-4.1-nano": 0.25,
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
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    cache_creation_tokens: int = 0,
) -> float | None:
    """USD for the given recorded usage; None when the model has no known rate; 0.0 for sim.

    `cached_input_tokens` is the part of `input_tokens` that was served from cache (both
    providers' records follow that convention). `cache_creation_tokens` is separate — cache
    writes are billed on top of the input total, at `CACHE_WRITE_MULTIPLIER` x the input rate.
    """
    if provider == "sim":
        return 0.0
    rates = _rate_for(model)
    tier = TIER_MULTIPLIER.get(provider)
    if tier is None:
        return None
    if rates is None:
        # No usage was recorded, so the cost is zero at any rate: an aborted run (a
        # provider 400 on the first call) must not be reported as unknown-rate, which
        # would freeze the budget guard over a run that provably billed nothing.
        no_usage = (input_tokens == output_tokens == cached_input_tokens
                    == cache_creation_tokens == 0)
        return 0.0 if no_usage else None
    in_rate, out_rate = rates[0] * tier, rates[1] * tier
    cached = min(cached_input_tokens, input_tokens)
    uncached = input_tokens - cached
    cached_fraction = cached_input_fraction(model)
    return (
        uncached * in_rate
        + cached * in_rate * cached_fraction
        + cache_creation_tokens * in_rate * CACHE_WRITE_MULTIPLIER
        + output_tokens * out_rate
    ) / 1_000_000


def has_rate(model: str) -> bool:
    """Whether `model` resolves to a published rate. A spend ceiling has to say so up front."""
    return _rate_for(model) is not None


def highest_rate() -> tuple[float, float]:
    """The most expensive (input, output) pair in the table, used to fail closed."""
    return (max(r[0] for r in RATES.values()), max(r[1] for r in RATES.values()))


def ceiling_price(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    cache_creation_tokens: int = 0,
) -> tuple[float, bool]:
    """USD to charge against a spend ceiling, and whether that number is a published rate.

    Differs from `price` in two ways, both so a ceiling fails closed rather than open:

    * an unpriced model on a billing provider costs `highest_rate()` rather than None —
      "we do not know" must never be spent as "$0" — and the caller is told (second element
      False) so it can say so out loud;
    * the provider is not what decides that something is free. `sim` is free only because
      no rate exists for its model ids; if the table does list one (a test injecting a fake
      table, a real model id run through the simulator) it is priced like any other. That
      keeps the ceiling testable without a paid provider.
    """
    rates = _rate_for(model)
    published = rates is not None
    if rates is None:
        if provider == "sim":
            # The simulator issues no HTTP request; there is nothing to bill.
            return 0.0, True
        rates = highest_rate()
    tier = TIER_MULTIPLIER.get(provider, 1.0)
    in_rate, out_rate = rates[0] * tier, rates[1] * tier
    cached = min(cached_input_tokens, input_tokens)
    uncached = input_tokens - cached
    cached_fraction = cached_input_fraction(model)
    usd = (
        uncached * in_rate
        + cached * in_rate * cached_fraction
        + cache_creation_tokens * in_rate * CACHE_WRITE_MULTIPLIER
        + output_tokens * out_rate
    ) / 1_000_000
    return usd, published


def run_cost(run_directory: str | Path) -> dict[str, Any]:
    """Sum recorded usage for one run and price it."""
    run_directory = Path(run_directory)
    manifest = recorder_manifest(run_directory)
    input_tokens = output_tokens = cached_tokens = cache_write_tokens = reps = 0
    cases_dir = run_directory / "cases"
    if cases_dir.exists():
        for case_dir in sorted(cases_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            for record in recorder.load_case_reps(run_directory, case_dir.name):
                input_tokens += record.usage.get("input_tokens", 0)
                output_tokens += record.usage.get("output_tokens", 0)
                cached_tokens += record.usage.get("cached_input_tokens", 0)
                cache_write_tokens += record.usage.get("cache_creation_input_tokens", 0)
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
        "cache_creation_input_tokens": cache_write_tokens,
        "usd": price(
            provider, model, input_tokens, output_tokens, cached_tokens, cache_write_tokens
        ),
    }


def recorder_manifest(run_directory: Path) -> dict[str, Any]:
    path = run_directory / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}
