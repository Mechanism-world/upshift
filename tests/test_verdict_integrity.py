"""DESIGN.md §D — INCONCLUSIVE, and the line between behaviour and an accident.

The whole point of the reason codes is one distinction:

  * the candidate model returns a 400 because it will not accept the request the agent
    sends — that is BEHAVIOUR, it is the regression upshift exists to find, and it must keep
    producing STAY PINNED and a repair attempt (rescue-ops A-075: 15/15 candidate reps, the
    documented `tool_choice` 400, correctly reported as a regression and repaired);
  * the account is out of credit, the key is wrong, the harness crashed, or half the reps
    were never run — that is an ACCIDENT, and reading it as a regression manufactures a
    result out of an operational failure. CLAUDE.md 2026-09-01 records the live version of
    this: "billing 400 mid-run must abort not record".

Every test below is one side of that line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift import verdict as V
from upshift.differ import CaseDiff, DiffResult
from upshift.schemas import (
    LABEL_REGRESSED,
    LABEL_STABLE_FAIL,
    LABEL_STABLE_PASS,
    OUTCOME_FAIL,
    OUTCOME_PASS,
)

N = 5


def _manifest(provider: str = "openai", n_reps: int = N, model: str = "m", **extra) -> dict:
    manifest = {
        "provider": provider,
        "n_reps": n_reps,
        "thresholds": {"pass": 0.8, "fail": 0.4},
        "agent": {"model_requested": model, "endpoint": "chat_completions"},
        "evidence_id": "e" * 64,
        "scope": "adapted_agent",
    }
    manifest.update(extra)
    return manifest


def _case(case_id: str, base: str, cand: str, label: str, *, b_n=N, c_n=N) -> CaseDiff:
    return CaseDiff(
        case_id=case_id,
        baseline_passes=b_n if base == OUTCOME_PASS else 0,
        baseline_n=b_n,
        candidate_passes=c_n if cand == OUTCOME_PASS else 0,
        candidate_n=c_n,
        baseline_outcome=base,
        candidate_outcome=cand,
        label=label,
        p_value=0.004,
        failure_signatures=[],
        failing_check_details=[],
    )


def _diff(cases, baseline=None, candidate=None) -> DiffResult:
    return DiffResult(
        baseline_run_id="base",
        candidate_run_id="cand",
        baseline_manifest=baseline or _manifest(model="gpt-5.5"),
        candidate_manifest=candidate or _manifest(model="gpt-5.6-sol"),
        cases=list(cases),
        counts={},
    )


HEALTHY = _case("healthy", OUTCOME_PASS, OUTCOME_PASS, LABEL_STABLE_PASS)
BROKEN = _case("broken", OUTCOME_PASS, OUTCOME_FAIL, LABEL_REGRESSED)


# ---------------------------------------------------------------------------
# The line: a model's 400 is a regression, an accident is not
# ---------------------------------------------------------------------------


def test_a_model_400_stays_a_regression() -> None:
    """A-075's actual shape: every candidate rep is a documented 400 from the model."""
    assert V.classify_api_error(
        {
            "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
            "status_code": 400,
            "type": "api_status_error",
        }
    ) is None
    assert V.decide(_diff([HEALTHY, BROKEN]))["verdict"] == V.STAY_PINNED


@pytest.mark.parametrize(
    ("api_error", "reason"),
    [
        (
            {"message": "Your credit balance is too low to access the Anthropic API",
             "status_code": 400, "type": "invalid_request_error"},
            V.REASON_BILLING_ERROR,
        ),
        (
            {"message": "You exceeded your current quota, please check your plan and billing",
             "status_code": 429, "type": "insufficient_quota"},
            V.REASON_BILLING_ERROR,
        ),
        (
            {"message": "Incorrect API key provided", "status_code": 401,
             "type": "authentication_error"},
            V.REASON_AUTH_ERROR,
        ),
        (
            {"message": "Request not allowed to access this workspace", "status_code": 403,
             "type": "permission_error"},
            V.REASON_AUTH_ERROR,
        ),
        (
            {"message": "The model `gpt-5.6-nope` does not exist or you do not have access",
             "status_code": 404, "type": "invalid_request_error"},
            V.REASON_MODEL_UNAVAILABLE,
        ),
        (
            {"message": "runner exited 1", "status_code": None, "type": "runner_error"},
            V.REASON_RUNNER_ERROR,
        ),
        (
            {"message": "the recording provided no further assistant turn", "type":
             "continuation_exhausted"},
            V.REASON_CONTINUATION_EXHAUSTED,
        ),
        (
            {"message": "pydantic validation failed", "type": "sdk_validation"},
            V.REASON_RUNNER_ERROR,
        ),
        (
            {"message": "backend raised", "type": "harness_error"},
            V.REASON_RUNNER_ERROR,
        ),
    ],
)
def test_operational_failures_are_classified_as_non_behavioural(api_error, reason) -> None:
    assert V.classify_api_error(api_error) == reason


def test_no_api_error_is_not_a_reason() -> None:
    assert V.classify_api_error(None) is None
    assert V.classify_api_error("some string") is None


# ---------------------------------------------------------------------------
# Each reason produces INCONCLUSIVE, and INCONCLUSIVE never produces a pass
# ---------------------------------------------------------------------------


def test_empty_suite() -> None:
    decided = V.decide(_diff([]))
    assert decided["verdict"] == V.INCONCLUSIVE
    assert decided["reasons"] == [V.REASON_EMPTY_SUITE]


def test_empty_suite_is_checked_before_baseline_broken() -> None:
    """With zero cases the baseline "passed 0 of 0", which is an absent suite, not a broken
    baseline — and reporting BASELINE_BROKEN would send the reader to fix an agent that is
    fine."""
    assert V.decide(_diff([]))["verdict"] != V.BASELINE_BROKEN


def test_incomplete_run() -> None:
    short = _case("short", OUTCOME_PASS, OUTCOME_PASS, LABEL_STABLE_PASS, c_n=3)
    decided = V.decide(_diff([HEALTHY, short]))
    assert decided["verdict"] == V.INCONCLUSIVE
    assert decided["reasons"] == [V.REASON_INCOMPLETE_RUN]
    assert decided["reason_details"][V.REASON_INCOMPLETE_RUN]["cases"] == ["short"]


def test_an_incomplete_run_cannot_be_safe() -> None:
    """3 of 5 reps recorded turns "3/5, flaky" into "3/3, PASS": the danger is precisely
    that a short run looks CLEANER than a complete one."""
    short = _case("short", OUTCOME_PASS, OUTCOME_PASS, LABEL_STABLE_PASS, b_n=3, c_n=3)
    assert V.decide(_diff([short]))["verdict"] == V.INCONCLUSIVE


def test_mixed_sim_and_live_evidence() -> None:
    decided = V.decide(
        _diff([HEALTHY, BROKEN], baseline=_manifest(provider="sim"),
              candidate=_manifest(provider="openai"))
    )
    assert decided["verdict"] == V.INCONCLUSIVE
    assert decided["reasons"] == [V.REASON_MIXED_EVIDENCE]


def test_sim_to_sim_is_not_mixed() -> None:
    """A simulator run is a valid machinery check; the report already stamps it. What is
    forbidden is COMBINING one with a paid run in a single verdict."""
    decided = V.decide(
        _diff([HEALTHY], baseline=_manifest(provider="sim"), candidate=_manifest(provider="sim"))
    )
    assert decided["verdict"] == V.SAFE


def test_transport_variants_count_as_real() -> None:
    decided = V.decide(
        _diff([HEALTHY], baseline=_manifest(provider="openai"),
              candidate=_manifest(provider="openai-flex"))
    )
    assert decided["verdict"] == V.SAFE


def test_cost_ceiling_stop() -> None:
    decided = V.decide(_diff([HEALTHY]), cost_stopped=True)
    assert decided["verdict"] == V.INCONCLUSIVE
    assert decided["reasons"] == [V.REASON_COST_CEILING]


def test_missing_run(tmp_path: Path) -> None:
    decided = V.decide(_diff([HEALTHY]), runs_root=tmp_path)
    assert decided["verdict"] == V.INCONCLUSIVE
    assert V.REASON_MISSING_RUN in decided["reasons"]


def test_billing_failure_recorded_in_a_run_makes_it_inconclusive(tmp_path: Path) -> None:
    for run_id in ("base", "cand"):
        _write_rep(tmp_path / run_id, "healthy", 1, api_error=None)
    _write_rep(
        tmp_path / "cand", "healthy", 2,
        api_error={"message": "Your credit balance is too low", "status_code": 400,
                   "type": "invalid_request_error"},
    )
    decided = V.decide(_diff([HEALTHY]), runs_root=tmp_path)
    assert decided["verdict"] == V.INCONCLUSIVE
    assert V.REASON_BILLING_ERROR in decided["reasons"]
    assert decided["reason_details"][V.REASON_BILLING_ERROR]["cases"] == ["cand:healthy"]


def test_a_behavioural_400_in_a_run_does_not_make_it_inconclusive(tmp_path: Path) -> None:
    for run_id in ("base", "cand"):
        _write_rep(tmp_path / run_id, "broken", 1, api_error=None)
        _write_rep(tmp_path / run_id, "healthy", 1, api_error=None)
    _write_rep(
        tmp_path / "cand", "broken", 2,
        api_error={"message": 'tool_choice: type "tool" and "any" are not supported for this '
                              "model.", "status_code": 400, "type": "api_status_error"},
    )
    decided = V.decide(_diff([HEALTHY, BROKEN]), runs_root=tmp_path)
    assert decided["verdict"] == V.STAY_PINNED
    assert decided["reasons"] == []


def test_integrity_checked_says_whether_rep_records_were_read(tmp_path: Path) -> None:
    assert V.decide(_diff([HEALTHY]))["integrity_checked"] is False
    for run_id in ("base", "cand"):
        _write_rep(tmp_path / run_id, "healthy", 1, api_error=None)
    assert V.decide(_diff([HEALTHY]), runs_root=tmp_path)["integrity_checked"] is True


def _write_rep(run_directory: Path, case_id: str, rep: int, api_error) -> None:
    path = run_directory / "cases" / case_id / f"rep_{rep:02d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"passed": api_error is None, "api_error": api_error}))


# ---------------------------------------------------------------------------
# Verdicts that must NOT change
# ---------------------------------------------------------------------------


def test_baseline_broken_survives() -> None:
    """rescue-ops A-075 §6.3(a): a 0/3 baseline printed SAFE, "a drop-in replacement"."""
    dead = _case("dead", OUTCOME_FAIL, OUTCOME_FAIL, LABEL_STABLE_FAIL)
    assert V.decide(_diff([dead]))["verdict"] == V.BASELINE_BROKEN


def test_safe_no_longer_claims_a_drop_in_replacement() -> None:
    decided = V.decide(_diff([HEALTHY]))
    assert decided["verdict"] == V.SAFE
    from upshift import report

    body = " ".join(report._verdict_summary(_diff([HEALTHY]), decided))
    assert "drop-in replacement" not in body
    assert "no regression detected on these 1 case(s) at 5 reps" in body


def test_every_verdict_carries_the_detectable_effect_sentence() -> None:
    decided = V.decide(_diff([HEALTHY]))
    assert "5/5 -> 1/5" in decided["detectable_effect"]
    assert "not evidence of equivalence" in decided["detectable_effect"]


def test_detectable_effect_says_so_when_n_is_too_small() -> None:
    tiny = _manifest(n_reps=2)
    case = _case("c", OUTCOME_PASS, OUTCOME_PASS, LABEL_STABLE_PASS, b_n=2, c_n=2)
    decided = V.decide(_diff([case], baseline=tiny, candidate=tiny))
    assert "could not have detected even a total collapse" in decided["detectable_effect"]


def test_the_verdict_carries_scope_and_evidence_ids() -> None:
    decided = V.decide(_diff([HEALTHY]))
    assert decided["scope"] == "adapted_agent"
    assert decided["evidence_ids"] == {"base": "e" * 64, "cand": "e" * 64}


def test_scope_defaults_to_the_pre_v05_meaning() -> None:
    manifest = _manifest()
    del manifest["scope"]
    assert V.decide(_diff([HEALTHY], candidate=manifest))["scope"] == "adapted_agent"


# ---------------------------------------------------------------------------
# The committed evidence must keep its verdict
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ["real-56sol", "shellgpt-56sol", "fable-sms"]


@pytest.mark.parametrize("tag", COMMITTED)
def test_no_new_inconclusive_reason_fires_on_a_committed_run(tag) -> None:
    """These three runs cost real money and are cited in the README, the reports and the
    session log. A new rule that quietly turned one of them INCONCLUSIVE would rewrite the
    product's own evidence, so every reason is recomputed against them here."""
    from upshift.differ import load_diff

    diff_path = ROOT / "runs" / tag / "diff.json"
    if not diff_path.is_file():  # pragma: no cover - the evidence tree may be pruned
        pytest.skip(f"{diff_path} not present")
    diff = load_diff(diff_path)
    issues = V.evidence_integrity(diff, runs_root=ROOT / "runs")
    assert issues == {}, f"{tag} would now be INCONCLUSIVE: {sorted(issues)}"


@pytest.mark.parametrize("tag", COMMITTED)
def test_a_committed_verdict_still_renders(tag) -> None:
    """Old verdict.json files have no scope, no collateral block and no evidence ids; the
    report must still render them, and must say which of those it could not check rather
    than inventing a value."""
    import io

    from rich.console import Console

    from upshift import report
    from upshift.differ import load_diff

    diff_path = ROOT / "runs" / tag / "diff.json"
    if not diff_path.is_file():  # pragma: no cover
        pytest.skip(f"{diff_path} not present")
    diff = load_diff(diff_path)
    committed = json.loads((ROOT / "runs" / tag / "verdict.json").read_text())
    buf = io.StringIO()
    report.render_diff(diff, console=Console(file=buf, width=200, no_color=True),
                       verdict=committed)
    rendered = buf.getvalue()
    assert committed["verdict"] in rendered
    assert "Verification scope: adapted_agent" in rendered
    assert "none recorded" in rendered  # no evidence ids on a pre-v0.5 run
