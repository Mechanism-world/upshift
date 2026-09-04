"""The SIMULATED PROVIDER stamp must mark simulated runs — and only simulated runs.

DESIGN.md's honesty rule cuts both ways: a sim run must never read as evidence about a real
model, and a real run must never be discredited as a sim. `anthropic` shipped as a real
provider in v0.3.0 (endpoint `messages`); it was missing from `report._providers`' real-provider
list, so every live Fable 5 -> 5.1 run was stamped "machinery validation only".
"""

from __future__ import annotations

from upshift.differ import DiffResult
from upshift.report import SIM_WARNING, diff_to_markdown


def _result(provider: str) -> DiffResult:
    manifest = {
        "provider": provider,
        "n_reps": 5,
        "thresholds": {"pass": 0.8, "fail": 0.4},
        "agent": {"model_requested": "m", "endpoint": "messages"},
    }
    return DiffResult(
        baseline_run_id="b",
        candidate_run_id="c",
        baseline_manifest=dict(manifest),
        candidate_manifest=dict(manifest),
        cases=[],
        counts={},
    )


def test_real_provider_runs_are_not_stamped_simulated() -> None:
    for provider in ("openai", "openai-batch", "openai-flex", "anthropic"):
        assert SIM_WARNING not in diff_to_markdown(_result(provider)), provider


def test_sim_runs_are_stamped_simulated() -> None:
    assert SIM_WARNING in diff_to_markdown(_result("sim"))
