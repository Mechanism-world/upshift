"""DESIGN.md §D — collateral protection is REPORTED, never assumed.

`verdict.json` used to hardcode `"broken_by_patch": 0` with the comment "the repair loop never
keeps a candidate that breaks a passing case, so an accepted patch always has zero collateral
damage by construction". That is true and it is also the problem: on a suite where the
candidate model passes NOTHING before the repair, the guard has nothing to guard, and "zero
collateral damage" reads to the user as "we checked and it was clean" when nothing was
checked. It is not a hypothetical — it is the ordinary shape of the rescue-ops corpus:

    repair start: 12 regressed case(s), 0 protected passing case(s), budget 6 candidates
        — ops/cases/ghi56-006/evidence/REPORT.md, and the same line in ghisdk-052,
          ghisdk-127, ghc-223, ghi56-001/002/004, p2-001 …

Two tests, one per side: a suite where the guard fires, and a suite where it has nothing to
fire on and the report says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _verdict_support import PATCH_MARKER, ScriptedProvider, write_agent

from upshift import report, verdict
from upshift.differ import diff_runs
from upshift.recorder import run_dir
from upshift.repair import loop as repair_loop
from upshift.repair.loop import repair
from upshift.runner import run_suite
from upshift.schemas import FileEdit, Patch

BASELINE, CANDIDATE = "base-model", "cand-model"
N = 2


def _patch(patch_id: str = "p1") -> Patch:
    return Patch(
        id=patch_id,
        repair_type="prompt_edit",
        signature="other_behavioral",
        description="scripted candidate",
        edits=[FileEdit(file="prompt.txt", new_content=f"BASE {PATCH_MARKER}\n")],
    )


def _only_candidate(patch: Patch, monkeypatch) -> None:
    monkeypatch.setattr(repair_loop, "generate_candidates", lambda *a, **k: [patch])


def _pipeline(tmp_path: Path, case_ids: list[str], script: dict, **repair_kwargs):
    agent_dir = write_agent(tmp_path / "agent", case_ids)
    runs = tmp_path / "runs"
    provider = ScriptedProvider(script, fail_after=repair_kwargs.pop("fail_after", None))
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
        **repair_kwargs,
    )
    return diff, outcome, runs, provider


# ---------------------------------------------------------------------------
# (a) The guard fires: a candidate that fixes one case and breaks a healthy one
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harmful(tmp_path_factory, ):
    tmp_path = tmp_path_factory.mktemp("harmful")
    monkeypatch = pytest.MonkeyPatch()
    _only_candidate(_patch(), monkeypatch)
    try:
        return _pipeline(
            tmp_path,
            ["broken", "healthy"],
            {
                BASELINE: {"base": {"broken", "healthy"}, "patched": {"broken", "healthy"}},
                # Unpatched the candidate model fails `broken` and keeps `healthy`; the
                # patch trades one for the other, which is exactly the swap that must never
                # be accepted as a repair.
                CANDIDATE: {"base": {"healthy"}, "patched": {"broken"}},
            },
        )
    finally:
        monkeypatch.undo()


def test_the_harmful_candidate_is_rejected(harmful) -> None:
    _, outcome, _, _ = harmful
    assert outcome.accepted_patches == []
    assert outcome.unrestored == ["broken"]
    assert any("REJECTED" in line and "healthy" in line for line in outcome.log)


def test_the_verdict_is_stay_pinned_not_safe_with_patch(harmful) -> None:
    diff, outcome, _, _ = harmful
    decided = verdict.decide(diff, outcome)
    assert decided["verdict"] == verdict.STAY_PINNED


def test_collateral_was_exercised_and_is_reported(harmful) -> None:
    diff, outcome, _, _ = harmful
    decided = verdict.decide(diff, outcome)
    assert decided["collateral"] == {
        "protected_cases": 1,
        "checks_executed": 1,  # one full verification run x one protected case
        "exercised": True,
    }
    body = " ".join(report.collateral_lines(decided))
    assert "1 case(s) that passed on the candidate before repair were re-measured" in body
    assert report.NO_COLLATERAL_SENTENCE not in body


# ---------------------------------------------------------------------------
# (b) Nothing to protect: SAFE WITH PATCH is allowed, and says the guard idled
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nothing_to_protect(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("nothing")
    monkeypatch = pytest.MonkeyPatch()
    _only_candidate(_patch(), monkeypatch)
    try:
        return _pipeline(
            tmp_path,
            ["c1", "c2"],
            {
                BASELINE: {"base": {"c1", "c2"}, "patched": {"c1", "c2"}},
                # The rescue-ops shape: the candidate model passes NOTHING until it is
                # patched, so the repair loop has zero protected cases.
                CANDIDATE: {"base": set(), "patched": {"c1", "c2"}},
            },
        )
    finally:
        monkeypatch.undo()


def test_safe_with_patch_is_still_reachable(nothing_to_protect) -> None:
    diff, outcome, _, _ = nothing_to_protect
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert decided["verdict"] == verdict.SAFE_WITH_PATCH
    assert decided["restored"] == 2


def test_the_absence_of_a_guard_is_visible(nothing_to_protect) -> None:
    diff, outcome, _, _ = nothing_to_protect
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert decided["collateral"] == {
        "protected_cases": 0,
        "checks_executed": 0,
        "exercised": False,
    }
    assert report.NO_COLLATERAL_SENTENCE in " ".join(report.collateral_lines(decided))


def test_the_sentence_reaches_the_markdown_report(nothing_to_protect) -> None:
    diff, outcome, _, _ = nothing_to_protect
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert report.NO_COLLATERAL_SENTENCE in report.diff_to_markdown(diff, verdict=decided)


def test_broken_by_patch_is_now_measured_not_asserted(nothing_to_protect) -> None:
    """The count still reads 0 — but because the final verification measured it, not because
    the field is a literal."""
    _, outcome, _, _ = nothing_to_protect
    assert outcome.broken_by_patch == []
    assert outcome.final_run_id is not None
