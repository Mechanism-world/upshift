"""Adjudication is not optional: a candidate is never accepted on a sample it disputes.

Two rules, one principle — the contested-status rule from 2026-08-28 ("never a single run")
has to hold in the direction where NOT running the extra reps is the cheap option:

* A protected or earlier-restored case that dips below PASS on a verify run is a SUSPECT, and
  the loop settles it with N more reps. If those reps cannot be run — the cost ceiling stops
  the pipeline, the operator interrupts it — the candidate is REJECTED, not accepted on the
  unadjudicated sample, and the run ends INCONCLUSIVE(cost_ceiling) rather than SAFE WITH
  PATCH. rescue-ops `ghisdk-052`: hunk 2's contested cases sat at 2/5 and 3/5 and were never
  adjudicated because the case went over budget.
* A restoration claim assembled out of two flaky-band samples is not a restoration. 2/5 on
  the screen plus 3/5 on the verify is 5/10 — half — and the threshold is 0.8 on the combined
  2N, which is the same bar a single run faces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _verdict_support import write_agent

from upshift import runner as runner_module
from upshift import verdict
from upshift.budget import CostCeilingExceeded
from upshift.differ import diff_runs
from upshift.providers.base import Provider
from upshift.recorder import run_dir
from upshift.repair import loop as repair_loop
from upshift.repair.loop import repair
from upshift.runner import run_suite
from upshift.schemas import FileEdit, Patch

BASELINE, CANDIDATE = "base-model", "cand-model"
MARKER = "PATCHED"


def _patch() -> Patch:
    return Patch(
        id="p1",
        repair_type="prompt_edit",
        signature="other_behavioral",
        description="scripted candidate",
        edits=[FileEdit(file="prompt.txt", new_content=f"BASE {MARKER}\n")],
    )


def _one_generation(agent_dir, signatures, **_kwargs):
    return [] if MARKER in (Path(agent_dir) / "prompt.txt").read_text() else [_patch()]


class ScheduledProvider(Provider):
    """Answers from a per-(model, variant, case) SCHEDULE of pass/fail, consumed in order.

    `ScriptedProvider` says whether a configuration passes a case; this one says what it does
    on each successive rep of it, which is the only way to build the flaky bands the
    adjudication rules exist for (2 of the first 5, then 3 of the next 5). Once a schedule is
    exhausted its last value repeats, so a run that makes more calls than the test scripted is
    still deterministic rather than an IndexError halfway through a rep.
    """

    name = "sim"

    def __init__(self, schedule: dict[str, list[bool]]) -> None:
        self.schedule = {k: list(v) for k, v in schedule.items()}
        self.position: dict[str, int] = {}

    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        case_id = (sim_context or {}).get("case_id") or seed_key.split(":")[0]
        model = request.get("model", "")
        system = "".join(
            str(m.get("content", ""))
            for m in request.get("messages", [])
            if m.get("role") == "system"
        )
        key = f"{model}:{'patched' if MARKER in system else 'base'}:{case_id}"
        values = self.schedule.get(key) or [False]
        index = min(self.position.get(key, 0), len(values) - 1)
        self.position[key] = self.position.get(key, 0) + 1
        return {
            "model": model,
            "choices": [
                {"message": {"role": "assistant", "content": "OK" if values[index] else "NO"}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


def _always(value: bool, n: int = 200) -> list[bool]:
    return [value] * n


def _pipeline(tmp_path: Path, monkeypatch, *, cases, schedule, n_reps, final_verify=True):
    monkeypatch.setattr(repair_loop, "generate_candidates", _one_generation)
    agent_dir = write_agent(tmp_path / "agent", cases)
    runs = tmp_path / "runs"
    provider = ScheduledProvider(schedule)
    for run_id, model in (("baseline", BASELINE), ("candidate", CANDIDATE)):
        run_suite(
            agent_dir, provider, run_id, n_reps=n_reps, model_override=model,
            runs_root=runs, workers=1,
        )
    diff = diff_runs(run_dir(runs, "baseline"), run_dir(runs, "candidate"))
    outcome = repair(
        original_agent_dir=agent_dir,
        work_dir=tmp_path / "patched_agent",
        provider=provider,
        candidate_model=CANDIDATE,
        baseline_diff=diff,
        n_reps=n_reps,
        runs_root=runs,
        run_prefix="t",
        budget=4,
        workers=1,
        final_verify=final_verify,
    )
    return diff, outcome, tmp_path / "patched_agent"


# ---------------------------------------------------------------------------
# ghisdk-052: adjudication that cannot run means the candidate is not accepted
# ---------------------------------------------------------------------------

#: `keeper` passes on the candidate model unpatched (so it is protected) and fails under the
#: patch, which makes it a suspect the loop must adjudicate. `c1` is restored by the patch.
SUSPECT_CASES = ["c1", "keeper"]
SUSPECT_SCHEDULE = {
    f"{BASELINE}:base:c1": _always(True),
    f"{BASELINE}:base:keeper": _always(True),
    f"{BASELINE}:patched:c1": _always(True),
    f"{BASELINE}:patched:keeper": _always(True),
    f"{CANDIDATE}:base:c1": _always(False),
    f"{CANDIDATE}:base:keeper": _always(True),
    f"{CANDIDATE}:patched:c1": _always(True),
    f"{CANDIDATE}:patched:keeper": _always(False),
}


def _stop_the_adjudication(monkeypatch, exception: BaseException) -> None:
    """Make the adjudication run — and only that run — fail the way a stopped pipeline does."""
    real = runner_module.run_suite

    def guarded(*args, **kwargs):
        if "adjudication" in str(kwargs.get("notes", "")):
            raise exception
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_module, "run_suite", guarded)


def test_a_candidate_whose_adjudication_cannot_run_is_not_accepted(
    tmp_path: Path, monkeypatch
) -> None:
    _stop_the_adjudication(
        monkeypatch,
        CostCeilingExceeded(phase="adjudication", spent_usd=1.0, limit_usd=1.0),
    )
    _, outcome, work_dir = _pipeline(
        tmp_path, monkeypatch, cases=SUSPECT_CASES, schedule=SUSPECT_SCHEDULE, n_reps=2
    )
    assert outcome.accepted_patches == []
    assert outcome.restored == []
    assert outcome.adjudication_skipped == ["keeper"]
    assert outcome.cost_stopped is True
    assert MARKER not in (work_dir / "prompt.txt").read_text(), (
        "the rejected candidate must not have been copied into the stacked agent"
    )


def test_the_verdict_is_inconclusive_not_safe_with_patch(tmp_path: Path, monkeypatch) -> None:
    _stop_the_adjudication(
        monkeypatch,
        CostCeilingExceeded(phase="adjudication", spent_usd=1.0, limit_usd=1.0),
    )
    diff, outcome, _ = _pipeline(
        tmp_path, monkeypatch, cases=SUSPECT_CASES, schedule=SUSPECT_SCHEDULE, n_reps=2
    )
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert decided["verdict"] == verdict.INCONCLUSIVE
    assert decided["inconclusive_reason"] == verdict.REASON_COST_CEILING
    assert decided["patch_path"] is None


def test_the_skipped_adjudication_is_recorded_in_the_log(tmp_path: Path, monkeypatch) -> None:
    _stop_the_adjudication(
        monkeypatch,
        CostCeilingExceeded(phase="adjudication", spent_usd=1.0, limit_usd=1.0),
    )
    _, outcome, _ = _pipeline(
        tmp_path, monkeypatch, cases=SUSPECT_CASES, schedule=SUSPECT_SCHEDULE, n_reps=2
    )
    assert any("adjudication" in line and "REJECTED" in line for line in outcome.log)
    assert any("INCONCLUSIVE" in line for line in outcome.log)


def test_an_interruption_during_adjudication_accepts_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """Ctrl-C is not a cost ceiling and is not swallowed — but it must not leave a candidate
    half-accepted either. The stacked agent is written only after adjudication has spoken."""
    _stop_the_adjudication(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _pipeline(
            tmp_path, monkeypatch, cases=SUSPECT_CASES, schedule=SUSPECT_SCHEDULE, n_reps=2
        )
    assert MARKER not in (tmp_path / "patched_agent" / "prompt.txt").read_text()


# ---------------------------------------------------------------------------
# A restoration built out of two flaky-band samples is not a restoration
# ---------------------------------------------------------------------------

FLAKY_CASES = ["c1", "c2"]
#: 2 of the first 5 reps (the screen) and 3 of the next 5 (the verify) = 5/10, which is 0.5
#: against a 0.8 pass threshold. `c1` is restored outright so the candidate is ranked and
#: verified at all; `c2` is the one that must NOT be counted.
FLAKY_SCHEDULE = {
    f"{BASELINE}:base:c1": _always(True),
    f"{BASELINE}:base:c2": _always(True),
    f"{CANDIDATE}:base:c1": _always(False),
    f"{CANDIDATE}:base:c2": _always(False),
    f"{CANDIDATE}:patched:c1": _always(True),
    f"{CANDIDATE}:patched:c2": [
        True, True, False, False, False,      # screen: 2/5
        True, True, True, False, False,       # verify: 3/5
    ],
}


def test_two_flaky_band_samples_do_not_add_up_to_a_restoration(
    tmp_path: Path, monkeypatch
) -> None:
    _, outcome, _ = _pipeline(
        tmp_path,
        monkeypatch,
        cases=FLAKY_CASES,
        schedule=FLAKY_SCHEDULE,
        n_reps=5,
        # The fresh final run would draw from the same schedule and is not what this test is
        # about; the claim under test is made by the screen+verify arithmetic.
        final_verify=False,
    )
    assert outcome.restored == ["c1"]
    assert outcome.unrestored == ["c2"]


def test_the_combined_count_is_what_decides_not_either_sample(
    tmp_path: Path, monkeypatch
) -> None:
    """The log has to let a reader redo the arithmetic: both samples are in it, and the
    combined 5/10 is what the rejection rests on."""
    _, outcome, _ = _pipeline(
        tmp_path, monkeypatch, cases=FLAKY_CASES, schedule=FLAKY_SCHEDULE, n_reps=5,
        final_verify=False,
    )
    assert any("screen p1: 1/2 broken cases restored" in line for line in outcome.log), (
        "the screen must report c1 restored and c2 not"
    )
    assert "c2" not in outcome.restored
