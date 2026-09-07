"""Rescue regression 1 — an entirely failing baseline can never produce a positive verdict.

maintenance coverage derived from rescue case A-075 (capture, BASELINE_BROKEN) — not
independent evidence of general repair capability.

The campaign hit suites where the baseline model passed nothing: the agent directory or the
eval suite was wrong, not the candidate model. Before the guard, `regressed` was necessarily
empty there and the verdict came out a vacuous SAFE — the single most dangerous output this
tool can produce, because it says "ship it" from a run that measured nothing.
"""

from __future__ import annotations

import pytest
from _support import write_agent_dir

from upshift import verdict as verdict_mod
from upshift.differ import diff_runs
from upshift.providers.sim import SimProvider
from upshift.recorder import run_dir
from upshift.report import _verdict_summary
from upshift.runner import run_suite

pytestmark = [pytest.mark.sim]


def _all_failing_pair(tmp_path):
    """A run pair where BOTH legs 400: sim-5.6 rejects function tools on chat/completions."""
    agent = write_agent_dir(tmp_path / "agent", endpoint="chat_completions", model="sim-5.6-sol")
    runs = tmp_path / "runs"
    provider = SimProvider()
    for run_id in ("rescue01-baseline", "rescue01-candidate"):
        run_suite(agent, provider, run_id, n_reps=3, runs_root=runs, workers=1)
    return diff_runs(run_dir(runs, "rescue01-baseline"), run_dir(runs, "rescue01-candidate"))


def test_all_failing_baseline_is_baseline_broken_not_safe(tmp_path):
    diff = _all_failing_pair(tmp_path)
    assert diff.baseline_passing_cases() == 0, "fixture precondition: the baseline passes nothing"

    result = verdict_mod.decide(diff)

    assert result["verdict"] == verdict_mod.BASELINE_BROKEN
    assert result["verdict"] not in (verdict_mod.SAFE, verdict_mod.SAFE_WITH_PATCH)
    assert result["patch_path"] is None
    assert result["restored"] == 0


def test_the_report_says_the_run_measured_nothing(tmp_path):
    diff = _all_failing_pair(tmp_path)
    lines = _verdict_summary(diff, verdict_mod.decide(diff))
    text = " ".join(lines)

    assert "this run measured nothing about the candidate" in text
    assert "the BASELINE model passed 0 of" in text
    # The sentence must not offer the reader a safe-sounding out.
    assert "drop-in" not in text
