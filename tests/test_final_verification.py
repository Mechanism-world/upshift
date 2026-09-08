"""DESIGN.md §D — the final verification is FRESH, and the verdict rests on it.

Until v0.5 the run that ACCEPTED a candidate was also the run that PROVED it. That is the
oldest form of overfitting there is: a candidate is chosen because it won on one sample, and
then confirmed by that same sample. With six candidates screened against the same reps, the
last one standing is partly a winner and partly a survivor.

So after the last accepted candidate the loop runs the stacked patch once more, on the full
suite, at seeds no selection run used, and the verdict cites that run. The screening,
verification and adjudication runs remain on disk and in the report — as `selection_runs`,
which is what they are.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _verdict_support import PATCH_MARKER, ScriptedProvider, write_agent

from upshift import recorder, report, verdict
from upshift.differ import diff_runs
from upshift.recorder import run_dir
from upshift.repair import loop as repair_loop
from upshift.repair.loop import repair
from upshift.runner import run_suite, seed_for
from upshift.schemas import FileEdit, Patch

BASELINE, CANDIDATE = "base-model", "cand-model"
CASES = ["c1", "c2"]
N = 2

#: The candidate model passes nothing until patched, so there are no protected cases and no
#: adjudication run: the runs are exactly screen, verify, final.
SCRIPT = {
    BASELINE: {"base": set(CASES), "patched": set(CASES)},
    CANDIDATE: {"base": set(), "patched": set(CASES)},
}


def _patch() -> Patch:
    return Patch(
        id="p1",
        repair_type="prompt_edit",
        signature="other_behavioral",
        description="scripted candidate",
        edits=[FileEdit(file="prompt.txt", new_content=f"BASE {PATCH_MARKER}\n")],
    )


def _pipeline(tmp_path: Path, monkeypatch, *, fail_after=None, final_verify=True):
    monkeypatch.setattr(repair_loop, "generate_candidates", lambda *a, **k: [_patch()])
    agent_dir = write_agent(tmp_path / "agent", CASES)
    runs = tmp_path / "runs"
    provider = ScriptedProvider(SCRIPT, fail_after=fail_after)
    for run_id, model in (("baseline", BASELINE), ("candidate", CANDIDATE)):
        run_suite(
            agent_dir, provider, run_id, n_reps=N, model_override=model,
            runs_root=runs, workers=1,
        )
    diff = diff_runs(run_dir(runs, "baseline"), run_dir(runs, "candidate"))
    outcome = repair(
        original_agent_dir=agent_dir,
        work_dir=tmp_path / "patched_agent",
        provider=provider,
        candidate_model=CANDIDATE,
        baseline_diff=diff,
        n_reps=N,
        runs_root=runs,
        run_prefix="t",
        budget=3,
        workers=1,
        final_verify=final_verify,
    )
    return diff, outcome, runs


# ---------------------------------------------------------------------------
# The happy path: a final run exists, is fresh, and is what the verdict cites
# ---------------------------------------------------------------------------


def test_a_final_run_is_performed_on_the_full_suite(tmp_path: Path, monkeypatch) -> None:
    _, outcome, runs = _pipeline(tmp_path, monkeypatch)
    assert outcome.final_run_id == "t-final"
    final = runs / "t-final"
    assert sorted(p.name for p in (final / "cases").iterdir()) == CASES
    for case_id in CASES:
        assert len(list((final / "cases" / case_id).glob("rep_*.json"))) == N


def test_the_selection_runs_are_listed_separately(tmp_path: Path, monkeypatch) -> None:
    _, outcome, _ = _pipeline(tmp_path, monkeypatch)
    assert outcome.selection_runs, "the screen/verify runs must still be recorded"
    assert all(r.endswith(("-screen", "-verify", "-adj")) for r in outcome.selection_runs)
    assert outcome.final_run_id not in outcome.selection_runs


def test_the_final_run_uses_seeds_no_selection_run_used(tmp_path: Path, monkeypatch) -> None:
    """The whole point: a NEW sample. Same (case, rep), different seed."""
    _, outcome, runs = _pipeline(tmp_path, monkeypatch)
    verify = next(r for r in outcome.selection_runs if r.endswith("-verify"))
    for case_id in CASES:
        for rep in (1, N):
            final_seed = recorder.load_rep(runs / "t-final", case_id, rep).seed
            verify_seed = recorder.load_rep(runs / verify, case_id, rep).seed
            assert final_seed != verify_seed
            assert final_seed != seed_for(case_id, rep)


def test_seed_for_is_restored_after_the_final_run(tmp_path: Path, monkeypatch) -> None:
    """The salt is applied by swapping a module-level function; leaving it swapped would
    quietly change every later run in the same process."""
    before = seed_for("c1", 1)
    _pipeline(tmp_path, monkeypatch)
    from upshift import runner

    assert runner.seed_for("c1", 1) == before


def test_the_verdict_cites_the_final_run(tmp_path: Path, monkeypatch) -> None:
    diff, outcome, _ = _pipeline(tmp_path, monkeypatch)
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert decided["verdict"] == verdict.SAFE_WITH_PATCH
    assert decided["final_run_id"] == "t-final"
    assert decided["evidence_label"] == "fresh_final_verification"
    assert decided["selection_runs"] == outcome.selection_runs
    body = " ".join(report.evidence_lines(diff, decided))
    assert "FRESH final verification run `t-final`" in body
    assert "selection_runs" in body


# ---------------------------------------------------------------------------
# The path this exists for: selection says yes, the fresh sample says no
# ---------------------------------------------------------------------------


def test_a_candidate_that_fails_the_fresh_run_is_not_safe_with_patch(
    tmp_path: Path, monkeypatch
) -> None:
    """`c1` passes every rep of the screen and the verify (4 calls) and then fails — the
    shape of a candidate that was selected by luck. The old loop would have shipped it."""
    diff, outcome, runs = _pipeline(
        tmp_path, monkeypatch, fail_after={f"{CANDIDATE}:patched:c1": 4}
    )
    assert (runs / "t-final").is_dir()
    assert outcome.unconfirmed_by_final == ["c1"]
    assert outcome.unrestored == ["c1"]
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert decided["verdict"] == verdict.STAY_PINNED
    assert decided["patch_path"] is None


def test_the_reason_is_recorded_in_the_log(tmp_path: Path, monkeypatch) -> None:
    _, outcome, _ = _pipeline(tmp_path, monkeypatch, fail_after={f"{CANDIDATE}:patched:c1": 4})
    assert any("NOT CONFIRMED by the fresh final run" in line for line in outcome.log)
    assert any("selection evidence is not verification evidence" in line for line in outcome.log)


# ---------------------------------------------------------------------------
# Opting out is allowed and is labelled
# ---------------------------------------------------------------------------


def test_final_verify_false_labels_the_evidence(tmp_path: Path, monkeypatch) -> None:
    diff, outcome, runs = _pipeline(tmp_path, monkeypatch, final_verify=False)
    assert outcome.final_run_id is None
    assert outcome.evidence_label == "selection_evidence_only"
    assert not (runs / "t-final").exists()
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert decided["verdict"] == verdict.SAFE_WITH_PATCH
    assert decided["evidence_label"] == "selection_evidence_only"
    body = " ".join(report.evidence_lines(diff, decided))
    assert "no fresh final verification was run" in body
    assert "weaker evidence" in body


def test_acceptance_thresholds_are_unchanged(tmp_path: Path, monkeypatch) -> None:
    """The final run is extra evidence, never a lowered bar: the thresholds recorded in the
    final run's own manifest are the ones every other run used."""
    _, _outcome, runs = _pipeline(tmp_path, monkeypatch)
    final = json.loads((runs / "t-final" / "manifest.json").read_text())
    baseline = json.loads((runs / "baseline" / "manifest.json").read_text())
    assert final["thresholds"] == baseline["thresholds"] == {"pass": 0.8, "fail": 0.4}
    assert final["n_reps"] == baseline["n_reps"] == N


@pytest.mark.parametrize("final_verify", [True, False])
def test_no_final_run_when_nothing_was_accepted(
    tmp_path: Path, monkeypatch, final_verify
) -> None:
    monkeypatch.setattr(repair_loop, "generate_candidates", lambda *a, **k: [])
    agent_dir = write_agent(tmp_path / "agent", CASES)
    runs = tmp_path / "runs"
    provider = ScriptedProvider(SCRIPT)
    for run_id, model in (("baseline", BASELINE), ("candidate", CANDIDATE)):
        run_suite(
            agent_dir, provider, run_id, n_reps=N, model_override=model,
            runs_root=runs, workers=1,
        )
    outcome = repair(
        original_agent_dir=agent_dir,
        work_dir=tmp_path / "patched_agent",
        provider=provider,
        candidate_model=CANDIDATE,
        baseline_diff=diff_runs(run_dir(runs, "baseline"), run_dir(runs, "candidate")),
        n_reps=N,
        runs_root=runs,
        run_prefix="t",
        workers=1,
        final_verify=final_verify,
    )
    assert outcome.final_run_id is None
    assert outcome.evidence_label is None
    assert not (runs / "t-final").exists()
