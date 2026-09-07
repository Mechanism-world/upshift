"""Rescue regression 10 — missing credentials, incomplete runs and budget stops stay distinct.

maintenance coverage derived from rescue cases ghc-024 / ghc-021 / trk-021 (COST_BLOCKED) and
ghisdk-052 (the $7.03 budget breach inside one `upshift upgrade`) — not independent evidence
of general repair capability.

A 429 for no credits, a run that stopped halfway, and a cost ceiling reached mid-suite all
produce the same surface symptom: cases that did not pass. None of them is a fact about the
candidate model. The campaign's single most expensive class of near-miss was a run whose
operational failure could have been read as a behavioural one, so this file pins each one to
its own outcome: abort, INCONCLUSIVE, or a stop marker — never SAFE, never a regression.
"""

from __future__ import annotations

import json

import pytest
from _support import require_attr, write_agent_dir

from upshift import verdict as verdict_mod
from upshift.budget import CostCeiling, CostCeilingExceeded, write_stopped_marker
from upshift.providers.base import ProviderAPIError
from upshift.providers.sim import SimProvider
from upshift.recorder import run_dir
from upshift.runner import BillingError, run_suite

pytestmark = [pytest.mark.mocked_transport]


class NoCreditsProvider:
    """The live shape seen on both providers during the campaign."""

    name = "openai"
    requires_all_workers = False

    def call(self, endpoint, request, seed_key, sim_context=None):
        raise ProviderAPIError(
            message="You exceeded your current quota, please check your plan and billing "
            "details.",
            status_code=429,
            error_type="api_status_error",
        )


# ---------------------------------------------------------------------------
# a. Credentials / billing: abort, do not record
# ---------------------------------------------------------------------------


def test_a_billing_failure_aborts_and_writes_no_reps(tmp_path):
    agent = write_agent_dir(tmp_path / "agent", model="gpt-5.5")

    with pytest.raises(BillingError, match="billing problem"):
        run_suite(
            agent, NoCreditsProvider(), "r10-billing", n_reps=3,
            runs_root=tmp_path / "runs", workers=1,
        )

    assert not list((tmp_path / "runs" / "r10-billing").rglob("rep_*.json")), (
        "a billing failure must not leave rep records that later read as candidate failures"
    )


# ---------------------------------------------------------------------------
# b. Cost ceiling: stop marker, exit 3, never a verdict
# ---------------------------------------------------------------------------


def test_a_reached_ceiling_stops_before_the_next_call_and_writes_a_marker(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    ceiling = CostCeiling(runs_root=runs, prefix="r10-cost", limit_usd=0.01)
    ceiling.observe("openai", "gpt-5.5", {"input_tokens": 10_000_000, "output_tokens": 0})

    with pytest.raises(CostCeilingExceeded) as excinfo:
        ceiling.check("candidate")

    directory = run_dir(runs, "r10-cost")
    directory.mkdir(parents=True, exist_ok=True)
    write_stopped_marker(directory, excinfo.value)
    marker = json.loads((directory / "COST_STOPPED.json").read_text())

    assert marker["phase"] == "candidate"
    assert not (directory / "verdict.json").exists(), (
        "a cost-stopped run must not carry a verdict of any kind"
    )


def test_a_cost_stopped_run_is_not_safe(tmp_path):
    """The verdict enum must have a reason-coded place to put this (DESIGN §D)."""
    inconclusive = require_attr(
        verdict_mod,
        "INCONCLUSIVE",
        "DESIGN §D: INCONCLUSIVE whenever a run is incomplete, was interrupted by the cost "
        "ceiling, contains billing/auth/quota/runner_error failures, or a required run is "
        "missing",
    )
    assert inconclusive not in (verdict_mod.SAFE, verdict_mod.SAFE_WITH_PATCH)


# ---------------------------------------------------------------------------
# c. Incomplete run: INCONCLUSIVE, not a regression
# ---------------------------------------------------------------------------


def test_an_incomplete_run_is_inconclusive_not_a_regression(tmp_path):
    agent = write_agent_dir(tmp_path / "agent", model="sim-5.5")
    runs = tmp_path / "runs"
    run_suite(agent, SimProvider(), "r10-base", n_reps=5, runs_root=runs, workers=1)
    run_suite(agent, SimProvider(), "r10-cand", n_reps=5, runs_root=runs, workers=1)

    # Truncate the candidate run: 5 reps requested, 2 on disk.
    case_dir = next((run_dir(runs, "r10-cand") / "cases").iterdir())
    for rep in (3, 4, 5):
        (case_dir / f"rep_{rep:02d}.json").unlink()

    from upshift.differ import diff_runs

    diff = diff_runs(run_dir(runs, "r10-base"), run_dir(runs, "r10-cand"))
    decide = verdict_mod.decide
    inconclusive = require_attr(
        verdict_mod,
        "INCONCLUSIVE",
        "DESIGN §D: a run with fewer reps than n_reps for any case makes the verdict "
        "INCONCLUSIVE (reason-coded), never SAFE / SAFE WITH PATCH",
    )

    result = decide(diff)
    assert result["verdict"] == inconclusive
    assert result.get("inconclusive_reason"), "INCONCLUSIVE must be reason-coded"
    assert result["verdict"] not in (verdict_mod.SAFE, verdict_mod.SAFE_WITH_PATCH)
