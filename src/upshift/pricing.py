"""Exact token-cost accounting from recorded usage. Never estimated: tokens come from the
API's own usage fields in the rep records; only the $/token rates are external.

Rates verified 2026-08-27 against OpenAI's published pricing (gpt-5.6-sol promotional
pricing effective through 2026-11-21). USD per 1M tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from upshift import recorder

# model prefix -> (sync_input, sync_output, batch_input, batch_output)
RATES: dict[str, tuple[float, float, float, float]] = {
    "gpt-5.5": (5.00, 30.00, 2.50, 15.00),
    "gpt-5.6-sol": (4.00, 20.00, 2.00, 10.00),
}


def _rate_for(model: str) -> tuple[float, float, float, float] | None:
    best = None
    for prefix, rates in RATES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rates)
    return best[1] if best else None


def run_cost(run_directory: str | Path) -> dict[str, Any]:
    """Sum recorded usage for one run and price it. usd is None for unknown rates; 0.0 for
    the sim provider (no real tokens were bought)."""
    run_directory = Path(run_directory)
    manifest = recorder_manifest(run_directory)
    input_tokens = output_tokens = reps = 0
    cases_dir = run_directory / "cases"
    if cases_dir.exists():
        for case_dir in sorted(cases_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            for record in recorder.load_case_reps(run_directory, case_dir.name):
                input_tokens += record.usage.get("input_tokens", 0)
                output_tokens += record.usage.get("output_tokens", 0)
                reps += 1
    provider = manifest.get("provider", "?")
    model = manifest.get("agent", {}).get("model_requested", "?")
    usd: float | None
    if provider == "sim":
        usd = 0.0
    else:
        rates = _rate_for(model)
        if rates is None:
            usd = None
        else:
            in_rate, out_rate = (rates[2], rates[3]) if provider == "openai-batch" else (
                rates[0],
                rates[1],
            )
            usd = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    return {
        "run_id": manifest.get("run_id", run_directory.name),
        "provider": provider,
        "model": model,
        "reps": reps,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd": usd,
    }


def recorder_manifest(run_directory: Path) -> dict[str, Any]:
    import json

    path = run_directory / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}
