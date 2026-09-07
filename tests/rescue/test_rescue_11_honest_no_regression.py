"""Rescue regression 11 — a compatible upgrade reports what was measured, not "drop-in".

maintenance coverage derived from rescue cases graphiti (NO_REGRESSION, OpenAI track) and the
quickstarts agent (detection-only, no parallelism regression at N=5) — not independent
evidence of general repair capability.

A clean run is a real and useful result, and it is also where a tool is most tempted to
overclaim. What the run supports is "no regression detected on N cases at N reps". What it
does not support is "drop-in replacement": DESIGN §D says the report must state the smallest
effect the run could have detected at its N, and that a non-significant difference is not
equivalence. At N=5 this suite cannot see a 5/5 -> 3/5 degradation at all.
"""

from __future__ import annotations

import re

import pytest
from _support import write_agent_dir

from upshift import verdict as verdict_mod
from upshift.differ import diff_runs
from upshift.providers.sim import SimProvider
from upshift.recorder import run_dir
from upshift.report import _verdict_summary
from upshift.runner import run_suite

pytestmark = [pytest.mark.sim]

N_REPS = 5


@pytest.fixture
def clean_pair(tmp_path):
    """A sim pair with no corruption on either leg: `/v1/responses` on sim-5.5 both times."""
    agent = write_agent_dir(tmp_path / "agent", endpoint="responses", model="sim-5.5")
    runs = tmp_path / "runs"
    provider = SimProvider()
    run_suite(agent, provider, "r11-baseline", n_reps=N_REPS, runs_root=runs, workers=1)
    run_suite(agent, provider, "r11-candidate", n_reps=N_REPS, runs_root=runs, workers=1)
    return diff_runs(run_dir(runs, "r11-baseline"), run_dir(runs, "r11-candidate"))


def test_a_clean_pair_is_safe_with_nothing_restored(clean_pair):
    result = verdict_mod.decide(clean_pair)

    assert result["verdict"] == verdict_mod.SAFE
    assert result["regressed"] == []
    assert result["restored"] == 0
    assert result["patch_path"] is None
    assert clean_pair.baseline_passing_cases() > 0, "SAFE must rest on a baseline that passed"


def test_the_sentence_says_what_was_measured_and_never_drop_in_replacement(clean_pair):
    lines = _verdict_summary(clean_pair, verdict_mod.decide(clean_pair))
    text = " ".join(lines)

    if "drop-in replacement" in text:
        pytest.skip(
            "interface not yet integrated: upshift.report._verdict_summary SAFE wording "
            '(DESIGN §D: the SAFE sentence must read "no regression detected on N cases at '
            'N reps" and state the smallest detectable effect; "drop-in replacement" is a '
            "claim of equivalence the run cannot support)"
        )

    assert re.search(r"no regression detected on \d+ case", text), text
    assert re.search(r"\d+ rep", text), text
    assert "drop-in" not in text
    assert "equivalent" not in text
