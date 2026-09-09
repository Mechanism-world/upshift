"""The repair loop screens every sibling candidate before it accepts one.

Reproduces two rescue-ops failures that greedy acceptance caused, both of them wrong
ANSWERS rather than crashes — which is why they were only found by reading the case files:

* `ghisdk-127` (waku): the first candidate the playbook happened to emit restored 8 of the
  9 broken cases, was verified, and was accepted. No candidate was generated for the one
  case that remained, the loop gave up, and the run published **STAY PINNED** — while a
  sibling candidate in the SAME generation restored all nine. The verdict was wrong, and the
  evidence for it was real.
* `ghc-062` (crispen): two candidates restored the same cases. The one that won was the one
  the playbook listed first, and it carried a disclosed change of guarantee; the maintainer's
  own fix, which changed nothing about what the agent promises, was never tried.

So: SCREEN every candidate of a signature round, then rank the restorers by (a) how many
broken cases each restored, full restoration first, (b) whether the candidate discloses a
changed capability or cost, (c) the playbook's own rank. Verify the best; on rejection fall
to the next. Acceptance thresholds and adjudication are untouched — this decides which
candidate gets to face them, never whether it passes.
"""

from __future__ import annotations

from pathlib import Path

from _verdict_support import ScriptedProvider, write_agent

from upshift import verdict
from upshift.differ import diff_runs
from upshift.recorder import run_dir
from upshift.repair import loop as repair_loop
from upshift.repair.loop import repair
from upshift.runner import run_suite
from upshift.schemas import FileEdit, Patch

BASELINE, CANDIDATE = "base-model", "cand-model"
N = 2

MARK_A, MARK_B = "PATCH_A", "PATCH_B"


def _patch(patch_id: str, marker: str) -> Patch:
    return Patch(
        id=patch_id,
        repair_type="prompt_edit",
        signature="other_behavioral",
        description=f"scripted candidate {patch_id}",
        edits=[FileEdit(file="prompt.txt", new_content=f"BASE {marker}\n")],
    )


def _one_generation(patches: list[Patch]):
    """`generate_candidates` that offers this generation ONCE.

    Once any candidate's marker is in the prompt a repair has been accepted, and the real
    playbook — which computes candidates from the agent's current contents — has nothing
    further to say for the same signature. That is precisely the waku situation: the loop
    does not get a second chance to find the candidate it skipped.
    """

    def generate(agent_dir, signatures, **_kwargs):
        prompt = (Path(agent_dir) / "prompt.txt").read_text()
        if MARK_A in prompt or MARK_B in prompt:
            return []
        return list(patches)

    return generate


def _pipeline(tmp_path: Path, monkeypatch, *, cases, script, patches, budget=6):
    monkeypatch.setattr(repair_loop, "generate_candidates", _one_generation(patches))
    agent_dir = write_agent(tmp_path / "agent", cases)
    runs = tmp_path / "runs"
    provider = ScriptedProvider(script, markers={MARK_A: "a", MARK_B: "b"})
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
        budget=budget,
        workers=1,
    )
    return diff, outcome


# ---------------------------------------------------------------------------
# ghisdk-127 (waku): the partial restorer must not foreclose the full one
# ---------------------------------------------------------------------------

WAKU_CASES = [f"c{i}" for i in range(1, 10)]
WAKU_SCRIPT = {
    BASELINE: {"base": set(WAKU_CASES), "a": set(WAKU_CASES), "b": set(WAKU_CASES)},
    CANDIDATE: {
        "base": set(),
        "a": set(WAKU_CASES[:8]),   # restores 8 of 9 — today's greedy winner
        "b": set(WAKU_CASES),       # restores all 9 — never screened today
    },
}


def test_the_full_restorer_wins_over_the_partial_one(tmp_path: Path, monkeypatch) -> None:
    _, outcome = _pipeline(
        tmp_path,
        monkeypatch,
        cases=WAKU_CASES,
        script=WAKU_SCRIPT,
        patches=[_patch("partial", MARK_A), _patch("full", MARK_B)],
    )
    assert [p.id for p in outcome.accepted_patches] == ["full"]
    assert outcome.unrestored == []
    assert outcome.restored == WAKU_CASES


def test_the_partial_restorer_is_still_screened_and_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    """Screening every sibling is what costs the extra runs; the log must show it happened."""
    _, outcome = _pipeline(
        tmp_path,
        monkeypatch,
        cases=WAKU_CASES,
        script=WAKU_SCRIPT,
        patches=[_patch("partial", MARK_A), _patch("full", MARK_B)],
    )
    assert outcome.tried == 2, "both siblings must be screened before either is verified"
    screens = [r for r in outcome.selection_runs if r.endswith("-screen")]
    assert len(screens) == 2
    assert any("8/9" in line for line in outcome.log)
    assert any("9/9" in line for line in outcome.log)


def test_the_verdict_is_safe_with_patch_not_stay_pinned(tmp_path: Path, monkeypatch) -> None:
    """The published waku result. Greedy acceptance made this STAY PINNED."""
    diff, outcome = _pipeline(
        tmp_path,
        monkeypatch,
        cases=WAKU_CASES,
        script=WAKU_SCRIPT,
        patches=[_patch("partial", MARK_A), _patch("full", MARK_B)],
    )
    decided = verdict.decide(diff, outcome, patch_path="upgrade.patch")
    assert decided["verdict"] == verdict.SAFE_WITH_PATCH


def test_budget_still_bounds_the_candidates_tried(tmp_path: Path, monkeypatch) -> None:
    """Screening more candidates costs more screen runs, so `--budget` must still cap them."""
    _, outcome = _pipeline(
        tmp_path,
        monkeypatch,
        cases=WAKU_CASES,
        script=WAKU_SCRIPT,
        patches=[_patch("partial", MARK_A), _patch("full", MARK_B)],
        budget=1,
    )
    assert outcome.tried == 1
    assert [p.id for p in outcome.accepted_patches] == ["partial"]


# ---------------------------------------------------------------------------
# ghc-062 (crispen): equal restoration -> the candidate with no disclosure wins
# ---------------------------------------------------------------------------

CRISPEN_CASES = ["c1", "c2"]
CRISPEN_SCRIPT = {
    BASELINE: {"base": set(CRISPEN_CASES), "a": set(CRISPEN_CASES), "b": set(CRISPEN_CASES)},
    CANDIDATE: {"base": set(), "a": set(CRISPEN_CASES), "b": set(CRISPEN_CASES)},
}


def test_an_undisclosed_repair_wins_a_tie_against_a_disclosed_one(
    tmp_path: Path, monkeypatch
) -> None:
    """`raise-effort-one-rung` discloses a cost change; `prompt-stop-after-goal` discloses
    nothing. Both sit at the same playbook rank and both restore everything, so the ONLY
    thing that separates them is the disclosure — and the playbook offers the disclosed one
    first, which is what greedy acceptance shipped."""
    from upshift.repair.playbook import disclosures_for, rank_for

    assert disclosures_for("raise-effort-one-rung")
    assert not disclosures_for("prompt-stop-after-goal")
    assert rank_for("raise-effort-one-rung") == rank_for("prompt-stop-after-goal")

    _, outcome = _pipeline(
        tmp_path,
        monkeypatch,
        cases=CRISPEN_CASES,
        script=CRISPEN_SCRIPT,
        patches=[
            _patch("raise-effort-one-rung", MARK_A),
            _patch("prompt-stop-after-goal", MARK_B),
        ],
    )
    assert [p.id for p in outcome.accepted_patches] == ["prompt-stop-after-goal"]


def test_a_disclosed_repair_still_wins_when_it_restores_more(
    tmp_path: Path, monkeypatch
) -> None:
    """The disclosure is a TIE-BREAK, not a veto: restoring more cases outranks it, because
    a repair nobody has to be warned about that leaves the agent broken is not a repair."""
    script = {
        BASELINE: {"base": set(CRISPEN_CASES), "a": set(CRISPEN_CASES), "b": set(CRISPEN_CASES)},
        CANDIDATE: {"base": set(), "a": set(CRISPEN_CASES), "b": {"c1"}},
    }
    _, outcome = _pipeline(
        tmp_path,
        monkeypatch,
        cases=CRISPEN_CASES,
        script=script,
        patches=[
            _patch("prompt-stop-after-goal", MARK_B),   # offered first, restores 1 of 2
            _patch("raise-effort-one-rung", MARK_A),    # discloses cost, restores 2 of 2
        ],
    )
    assert [p.id for p in outcome.accepted_patches] == ["raise-effort-one-rung"]


def test_a_rejected_best_candidate_falls_through_to_the_next(
    tmp_path: Path, monkeypatch
) -> None:
    """Ranking chooses the order of VERIFICATION, and verification still has the last word:
    a candidate that screens best and then breaks a protected case is rejected, and the
    sibling behind it is verified rather than the round being abandoned."""
    cases = ["c1", "c2", "keeper"]
    script = {
        BASELINE: {"base": set(cases), "a": set(cases), "b": set(cases)},
        CANDIDATE: {
            "base": {"keeper"},          # `keeper` passes unpatched: a protected case
            "a": {"c1", "c2"},           # restores both, but loses `keeper`
            "b": {"c1", "c2", "keeper"},
        },
    }
    _, outcome = _pipeline(
        tmp_path,
        monkeypatch,
        cases=cases,
        script=script,
        patches=[_patch("greedy", MARK_A), _patch("safe", MARK_B)],
    )
    assert [p.id for p in outcome.accepted_patches] == ["safe"]
    assert any(line.strip().startswith("REJECTED") for line in outcome.log)
    assert outcome.protected_cases == ["keeper"]
