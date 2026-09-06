"""Tests for upshift.differ (and a rendering smoke test for upshift.report).

Every fixture builds real run directories on disk from real RepRecord objects, so the tests
exercise the same serialize/deserialize path the runner and the CLI use.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from upshift import differ, report, verdict
from upshift.differ import (
    SIG_ACTING_PAST_GOAL,
    SIG_API_ERROR_OTHER,
    SIG_API_ERROR_TOOLS_REASONING,
    SIG_DUPLICATE_TOOL_CALLS,
    SIG_OTHER_BEHAVIORAL,
    SIG_SKIPPED_TOOL_HALLUCINATION,
    SIG_WRONG_OR_MISSING_TOOL_CALL,
    diff_runs,
    failure_signatures,
    load_diff,
    save_diff,
)
from upshift.schemas import (
    LABEL_FLAKY,
    LABEL_IMPROVED,
    LABEL_REGRESSED,
    LABEL_STABLE_FAIL,
    LABEL_STABLE_PASS,
    CheckResult,
    RepRecord,
    ToolExecution,
)

CASE_SET_HASH = "cases-hash-abc123"
BASE_HASHES = {"agent.json": "aaa", "system_prompt.txt": "bbb", "tools.json": "ccc"}
PATCHED_HASHES = {"agent.json": "zzz", "system_prompt.txt": "bbb", "tools.json": "ccc"}

TOOLS_REASONING_400 = {
    "status_code": 400,
    "message": (
        "Function tools with reasoning_effort are not supported with this model. "
        "Use /v1/responses or set reasoning_effort to 'none'."
    ),
    "type": "api_error",
}


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def check(ctype: str, passed: bool, detail: str = "", **extra: Any) -> CheckResult:
    return CheckResult(check={"type": ctype, **extra}, passed=passed, detail=detail)


def rep(
    case_id: str,
    k: int,
    passed: bool,
    *,
    checks: list[CheckResult] | None = None,
    api_error: dict[str, Any] | None = None,
    tool_executions: list[ToolExecution] | None = None,
    final_state: dict[str, Any] | None = None,
    final_message: str = "",
) -> RepRecord:
    if checks is None:
        checks = [check("no_api_error", passed, "" if passed else "api error")]
    return RepRecord(
        case_id=case_id,
        rep=k,
        seed=k,
        model_requested="gpt-5.6-sol",
        resolved_model="gpt-5.6-sol-2026-08-01",
        endpoint="chat_completions",
        params={},
        api_calls=[],
        tool_executions=tool_executions or [],
        final_state=final_state if final_state is not None else {"bookings": {}},
        final_message=final_message,
        check_results=checks,
        passed=passed,
        api_error=api_error,
        usage={"input_tokens": 10, "output_tokens": 5},
        latency_s=0.5,
    )


def reps(case_id: str, n_pass: int, n: int, **kwargs: Any) -> list[RepRecord]:
    """n reps of one case, the first n_pass of which passed."""
    return [rep(case_id, k, k <= n_pass, **kwargs) for k in range(1, n + 1)]


def write_run(
    root: Path,
    run_id: str,
    cases: dict[str, list[RepRecord]],
    *,
    provider: str = "sim",
    model: str = "gpt-5.5",
    endpoint: str = "chat_completions",
    n_reps: int = 5,
    thresholds: dict[str, float] | None = None,
    case_set_hash: str = CASE_SET_HASH,
    file_hashes: dict[str, str] | None = None,
) -> Path:
    run_dir = root / run_id
    (run_dir / "cases").mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "created_at": "2026-08-27T12:00:00Z",
        "provider": provider,
        "agent": {
            "name": "booking_agent",
            "endpoint": endpoint,
            "model_requested": model,
            "params": {},
            "max_turns": 12,
            "file_hashes": file_hashes or BASE_HASHES,
        },
        "n_reps": n_reps,
        "thresholds": thresholds or {"pass": 0.8, "fail": 0.4},
        "case_set_hash": case_set_hash,
        "upshift_version": "0.1.0",
        "notes": "",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    summary = {}
    for case_id, records in cases.items():
        case_dir = run_dir / "cases" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        for record in records:
            (case_dir / f"rep_{record.rep:02d}.json").write_text(record.to_json())
        # Deliberately WRONG: the differ must recompute and never trust summary.json.
        summary[case_id] = {"passes": 999, "n": 999}
    (run_dir / "summary.json").write_text(json.dumps(summary))
    return run_dir


def dup_executions(name: str = "book_flight") -> list[ToolExecution]:
    args = {"flight_id": "UA123", "passenger": "ada"}
    return [
        ToolExecution(turn=0, segment=0, name=name, arguments=args, result={"ok": True}),
        ToolExecution(turn=1, segment=0, name=name, arguments=args, result={"ok": True}),
    ]


@pytest.fixture
def synthetic_runs(tmp_path: Path) -> tuple[Path, Path]:
    """Five cases, one per label, with the failure modes the playbook cares about."""
    baseline = write_run(
        tmp_path / "runs",
        "gpt-5.5@chat_completions-20260827-1",
        {
            "book_happy": reps("book_happy", 5, 5),
            "search_basic": reps("search_basic", 5, 5),
            "cancel_flow": reps("cancel_flow", 5, 5),
            "budget_edge": reps("budget_edge", 1, 5),
            "invalid_airport": reps("invalid_airport", 0, 5),
        },
        model="gpt-5.5",
    )
    candidate = write_run(
        tmp_path / "runs",
        "gpt-5.6-sol@chat_completions-20260827-1",
        {
            # regressed, killed by the documented 5.6 tool/reasoning 400
            "book_happy": reps(
                "book_happy",
                0,
                5,
                api_error=TOOLS_REASONING_400,
                checks=[
                    check(
                        "no_api_error",
                        False,
                        "API error 400: Function tools with "
                        "reasoning_effort are not supported with this model.",
                    )
                ],
            ),
            "search_basic": reps("search_basic", 5, 5),
            # flaky: 3/5, the failures are duplicate bookings
            "cancel_flow": [
                rep("cancel_flow", k, k <= 3)
                if k <= 3
                else rep(
                    "cancel_flow",
                    k,
                    False,
                    checks=[
                        check("bookings_count", False, "expected 1 booking, found 2", equals=1)
                    ],
                    tool_executions=dup_executions(),
                    final_state={
                        "bookings": {
                            "UPS-1": {"status": "confirmed"},
                            "UPS-2": {"status": "confirmed"},
                        }
                    },
                )
                for k in range(1, 6)
            ],
            "budget_edge": reps("budget_edge", 5, 5),
            "invalid_airport": reps(
                "invalid_airport",
                0,
                5,
                checks=[
                    check(
                        "response_contains",
                        False,
                        "final message did not mention 'invalid'",
                        text="invalid",
                    )
                ],
            ),
        },
        model="gpt-5.6-sol",
    )
    return baseline, candidate


# ---------------------------------------------------------------------------
# diff_runs: labels, counts, statistics
# ---------------------------------------------------------------------------


def test_diff_labels_and_counts(synthetic_runs: tuple[Path, Path]) -> None:
    result = diff_runs(*synthetic_runs)
    labels = {c.case_id: c.label for c in result.cases}
    assert labels == {
        "book_happy": LABEL_REGRESSED,
        "search_basic": LABEL_STABLE_PASS,
        "cancel_flow": LABEL_FLAKY,
        "budget_edge": LABEL_IMPROVED,
        "invalid_airport": LABEL_STABLE_FAIL,
    }
    assert result.counts == {
        LABEL_REGRESSED: 1,
        LABEL_STABLE_PASS: 1,
        LABEL_FLAKY: 1,
        LABEL_IMPROVED: 1,
        LABEL_STABLE_FAIL: 1,
    }
    assert result.baseline_run_id == "gpt-5.5@chat_completions-20260827-1"
    assert result.candidate_run_id == "gpt-5.6-sol@chat_completions-20260827-1"


def test_diff_recomputes_pass_counts_and_ignores_summary_json(
    synthetic_runs: tuple[Path, Path],
) -> None:
    # summary.json claims 999/999 for every case; the differ must ignore it entirely.
    result = diff_runs(*synthetic_runs)
    by_id = {c.case_id: c for c in result.cases}
    assert (by_id["book_happy"].baseline_passes, by_id["book_happy"].baseline_n) == (5, 5)
    assert (by_id["book_happy"].candidate_passes, by_id["book_happy"].candidate_n) == (0, 5)
    assert (by_id["cancel_flow"].candidate_passes, by_id["cancel_flow"].candidate_n) == (3, 5)
    assert (by_id["budget_edge"].baseline_passes, by_id["budget_edge"].candidate_passes) == (1, 5)
    assert by_id["invalid_airport"].baseline_passes == 0


def test_diff_outcomes(synthetic_runs: tuple[Path, Path]) -> None:
    by_id = {c.case_id: c for c in diff_runs(*synthetic_runs).cases}
    assert (by_id["book_happy"].baseline_outcome, by_id["book_happy"].candidate_outcome) == (
        "PASS",
        "FAIL",
    )
    assert by_id["cancel_flow"].candidate_outcome == "FLAKY"
    assert by_id["budget_edge"].baseline_outcome == "FAIL"


def test_diff_p_values(synthetic_runs: tuple[Path, Path]) -> None:
    by_id = {c.case_id: c for c in diff_runs(*synthetic_runs).cases}
    # 5/5 -> 0/5 = 1/252; 5/5 -> 3/5 = 56/252; no-change and improvement -> 1.0
    assert by_id["book_happy"].p_value == pytest.approx(1 / 252, abs=1e-9)
    assert by_id["cancel_flow"].p_value == pytest.approx(2 / 9, abs=1e-9)
    assert by_id["search_basic"].p_value == pytest.approx(1.0)
    assert by_id["budget_edge"].p_value == pytest.approx(1.0)
    assert by_id["invalid_airport"].p_value == pytest.approx(1.0)


def test_diff_signatures_and_details(synthetic_runs: tuple[Path, Path]) -> None:
    by_id = {c.case_id: c for c in diff_runs(*synthetic_runs).cases}
    assert by_id["book_happy"].failure_signatures == [SIG_API_ERROR_TOOLS_REASONING]
    assert by_id["cancel_flow"].failure_signatures == [SIG_DUPLICATE_TOOL_CALLS]
    assert by_id["invalid_airport"].failure_signatures == [SIG_OTHER_BEHAVIORAL]
    # A case with no failing candidate reps has nothing to repair.
    assert by_id["search_basic"].failure_signatures == []
    assert by_id["search_basic"].failing_check_details == []
    assert by_id["cancel_flow"].failing_check_details == ["expected 1 booking, found 2"]
    assert "Function tools" in by_id["book_happy"].failing_check_details[0]


def test_failing_details_are_deduplicated_and_capped(tmp_path: Path) -> None:
    records = [
        rep(
            "c",
            k,
            False,
            checks=[
                check("tool_called", False, f"detail number {k}"),
                check("tool_called", False, "repeated detail"),
            ],
        )
        for k in range(1, 9)
    ]
    details = differ.failing_details(records)
    assert len(details) == differ.MAX_FAILING_DETAILS
    assert len(set(details)) == len(details)
    assert details[0] == "detail number 1"
    assert details[1] == "repeated detail"


# ---------------------------------------------------------------------------
# Failure signature taxonomy
# ---------------------------------------------------------------------------


def test_signature_api_error_tools_reasoning() -> None:
    assert failure_signatures([rep("c", 1, False, api_error=TOOLS_REASONING_400)]) == [
        SIG_API_ERROR_TOOLS_REASONING
    ]


@pytest.mark.parametrize(
    "message",
    [
        "Function tools with reasoning_effort are not supported",
        "FUNCTION TOOLS cannot be combined with REASONING EFFORT",
        "function tools + reasoning effort is unsupported on this endpoint",
    ],
)
def test_signature_api_error_tools_reasoning_matching_is_case_and_spelling_tolerant(
    message: str,
) -> None:
    err = {"status_code": 400, "message": message, "type": "api_error"}
    assert failure_signatures([rep("c", 1, False, api_error=err)]) == [
        SIG_API_ERROR_TOOLS_REASONING
    ]


@pytest.mark.parametrize(
    "err",
    [
        {"status_code": 500, "message": "internal server error", "type": "api_error"},
        {"status_code": 429, "message": "rate limited", "type": "api_error"},
        # right words, wrong status code -> not the documented break
        {"status_code": 422, "message": "function tools reasoning_effort", "type": "api_error"},
        # right status, only half the phrase
        {"status_code": 400, "message": "function tools are malformed", "type": "api_error"},
        {"status_code": 400, "message": "reasoning_effort must be a string", "type": "api_error"},
    ],
)
def test_signature_api_error_other(err: dict[str, Any]) -> None:
    assert failure_signatures([rep("c", 1, False, api_error=err)]) == [SIG_API_ERROR_OTHER]


def test_signature_duplicate_tool_calls_from_repeated_executions() -> None:
    record = rep(
        "c",
        1,
        False,
        tool_executions=dup_executions(),
        checks=[check("final_state", False, "wrong state")],
    )
    assert SIG_DUPLICATE_TOOL_CALLS in failure_signatures([record])


def test_signature_duplicate_ignores_errored_and_cross_segment_repeats() -> None:
    args = {"flight_id": "UA123"}
    errored = [
        ToolExecution(0, 0, "book_flight", args, {"error": "no availability"}),
        ToolExecution(1, 0, "book_flight", args, {"error": "no availability"}),
    ]
    record = rep("c", 1, False, tool_executions=errored, checks=[check("final_state", False, "x")])
    assert failure_signatures([record]) == [SIG_OTHER_BEHAVIORAL]

    cross_segment = [
        ToolExecution(0, 0, "book_flight", args, {"ok": True}),
        ToolExecution(1, 1, "book_flight", args, {"ok": True}),
    ]
    record = rep(
        "c", 1, False, tool_executions=cross_segment, checks=[check("final_state", False, "x")]
    )
    assert failure_signatures([record]) == [SIG_OTHER_BEHAVIORAL]


def test_signature_duplicate_from_recomputed_bookings_count() -> None:
    record = rep(
        "c",
        1,
        False,
        checks=[check("bookings_count", False, "detail is not parsed", equals=1)],
        final_state={
            "bookings": {"UPS-1": {"status": "confirmed"}, "UPS-2": {"status": "confirmed"}}
        },
    )
    assert failure_signatures([record]) == [SIG_DUPLICATE_TOOL_CALLS]


def test_signature_bookings_count_shortfall_is_not_a_duplicate() -> None:
    # Booking too FEW is a missing call, not a duplicated one.
    record = rep(
        "c",
        1,
        False,
        checks=[check("bookings_count", False, "expected 1, found 0", equals=1)],
        final_state={"bookings": {}},
    )
    assert failure_signatures([record]) == [SIG_OTHER_BEHAVIORAL]


def test_signature_bookings_count_ignores_cancelled_bookings() -> None:
    record = rep(
        "c",
        1,
        False,
        checks=[check("bookings_count", False, "d", equals=1)],
        final_state={
            "bookings": {"UPS-1": {"status": "confirmed"}, "UPS-2": {"status": "cancelled"}}
        },
    )
    assert failure_signatures([record]) == [SIG_OTHER_BEHAVIORAL]


def test_signature_bookings_count_handles_list_shaped_state() -> None:
    record = rep(
        "c",
        1,
        False,
        checks=[check("bookings_count", False, "d", equals=1)],
        final_state={"bookings": [{"status": "confirmed"}, {"status": "confirmed"}]},
    )
    assert failure_signatures([record]) == [SIG_DUPLICATE_TOOL_CALLS]


def test_signature_acting_past_goal() -> None:
    record = rep(
        "c",
        1,
        False,
        checks=[
            check(
                "no_tool_calls_after_success",
                False,
                "2 tool calls after book_flight",
                name="book_flight",
            )
        ],
    )
    assert failure_signatures([record]) == [SIG_ACTING_PAST_GOAL]


def test_signature_skipped_tool_hallucination_from_confirmation_check() -> None:
    record = rep(
        "c",
        1,
        False,
        checks=[check("confirmation_id_valid", False, "UPS-9 not in backend state")],
        final_message="Booked! Your confirmation is UPS-9.",
    )
    assert failure_signatures([record]) == [SIG_SKIPPED_TOOL_HALLUCINATION]


def test_signature_skipped_tool_hallucination_from_missing_book_plus_fake_id() -> None:
    record = rep(
        "c",
        1,
        False,
        checks=[check("tool_called", False, "book_flight never called", name="book_flight")],
        final_message="All set, your confirmation number is UPS-42.",
    )
    assert failure_signatures([record]) == [SIG_SKIPPED_TOOL_HALLUCINATION]


def test_signature_missing_book_without_fake_id_is_wrong_or_missing_tool_call() -> None:
    record = rep(
        "c",
        1,
        False,
        checks=[check("tool_called", False, "book_flight never called", name="book_flight")],
        final_message="I could not complete the booking.",
    )
    assert failure_signatures([record]) == [SIG_WRONG_OR_MISSING_TOOL_CALL]


@pytest.mark.parametrize("ctype", ["tool_called", "tool_not_called"])
def test_signature_wrong_or_missing_tool_call(ctype: str) -> None:
    record = rep("c", 1, False, checks=[check(ctype, False, "detail", name="search_flights")])
    assert failure_signatures([record]) == [SIG_WRONG_OR_MISSING_TOOL_CALL]


def test_signature_other_behavioral() -> None:
    record = rep("c", 1, False, checks=[check("response_matches", False, "no regex match")])
    assert failure_signatures([record]) == [SIG_OTHER_BEHAVIORAL]


def test_signatures_are_deduplicated_and_priority_ordered() -> None:
    records = [
        rep("c", 1, False, api_error=TOOLS_REASONING_400),
        rep("c", 2, False, api_error={"status_code": 500, "message": "boom", "type": "api_error"}),
        rep("c", 3, False, checks=[check("response_contains", False, "d")]),
        rep(
            "c",
            4,
            False,
            checks=[check("no_tool_calls_after_success", False, "d", name="book_flight")],
            tool_executions=dup_executions(),
        ),
        rep(
            "c",
            5,
            False,
            checks=[
                check("confirmation_id_valid", False, "d"),
                check("tool_not_called", False, "d", name="cancel_booking"),
            ],
        ),
        # duplicated across reps: must appear once
        rep("c", 6, False, api_error=TOOLS_REASONING_400),
    ]
    assert failure_signatures(records) == [
        SIG_API_ERROR_TOOLS_REASONING,
        SIG_API_ERROR_OTHER,
        SIG_DUPLICATE_TOOL_CALLS,
        SIG_ACTING_PAST_GOAL,
        SIG_SKIPPED_TOOL_HALLUCINATION,
        SIG_WRONG_OR_MISSING_TOOL_CALL,
        SIG_OTHER_BEHAVIORAL,
    ]


def test_no_failing_reps_yields_no_signatures() -> None:
    assert failure_signatures(reps("c", 5, 5)) == []
    assert failure_signatures([]) == []


def test_passing_reps_do_not_contribute_behavioral_signatures() -> None:
    # A rep that passed cannot be the source of a behavioral signature even if it happens to
    # contain a repeated tool call; only failing reps are classified.
    records = [
        rep("c", 1, True, tool_executions=dup_executions()),
        rep("c", 2, False, checks=[check("response_contains", False, "d")]),
    ]
    assert failure_signatures(records) == [SIG_OTHER_BEHAVIORAL]


# ---------------------------------------------------------------------------
# Hard errors: mismatched runs
# ---------------------------------------------------------------------------


def test_case_set_hash_mismatch_is_an_error(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5)})
    b = write_run(tmp_path, "b", {"c1": reps("c1", 5, 5)}, case_set_hash="different-hash")
    with pytest.raises(ValueError, match="case_set_hash"):
        diff_runs(a, b)


def test_threshold_mismatch_is_an_error(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5)})
    b = write_run(tmp_path, "b", {"c1": reps("c1", 5, 5)}, thresholds={"pass": 0.6, "fail": 0.2})
    with pytest.raises(ValueError, match="thresholds differ"):
        diff_runs(a, b)


def test_n_reps_mismatch_is_an_error(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5)})
    b = write_run(tmp_path, "b", {"c1": reps("c1", 3, 3)}, n_reps=3)
    with pytest.raises(ValueError, match="n_reps differs"):
        diff_runs(a, b)


def test_case_missing_from_candidate_is_an_error(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5), "gone": reps("gone", 5, 5)})
    b = write_run(tmp_path, "b", {"c1": reps("c1", 5, 5)})
    with pytest.raises(ValueError, match="'gone'.*candidate"):
        diff_runs(a, b)


def test_case_missing_from_baseline_is_an_error(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5)})
    b = write_run(tmp_path, "b", {"c1": reps("c1", 5, 5), "extra": reps("extra", 5, 5)})
    with pytest.raises(ValueError, match="'extra'.*baseline"):
        diff_runs(a, b)


def test_case_with_zero_reps_is_an_error(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5), "empty": reps("empty", 5, 5)})
    b = write_run(tmp_path, "b", {"c1": reps("c1", 5, 5)})
    (b / "cases" / "empty").mkdir(parents=True)  # directory exists, no rep files
    with pytest.raises(ValueError, match="'empty'.*no reps"):
        diff_runs(a, b)


def test_missing_manifest_is_an_error(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5)})
    b = write_run(tmp_path, "b", {"c1": reps("c1", 5, 5)})
    (b / "manifest.json").unlink()
    with pytest.raises(ValueError, match="manifest.json"):
        diff_runs(a, b)


# ---------------------------------------------------------------------------
# agent_files_differ
# ---------------------------------------------------------------------------


def test_agent_files_differ_flag(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5)})
    same = write_run(tmp_path, "same", {"c1": reps("c1", 5, 5)})
    patched = write_run(tmp_path, "patched", {"c1": reps("c1", 5, 5)}, file_hashes=PATCHED_HASHES)
    assert diff_runs(a, same).agent_files_differ is False
    # A patched candidate must diff cleanly; it only raises the flag.
    result = diff_runs(a, patched)
    assert result.agent_files_differ is True
    assert result.counts == {LABEL_STABLE_PASS: 1}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_save_load_round_trip(synthetic_runs: tuple[Path, Path], tmp_path: Path) -> None:
    result = diff_runs(*synthetic_runs)
    path = save_diff(result, tmp_path / "out" / "diff.json")
    assert path.is_file()
    reloaded = load_diff(path)
    assert asdict(reloaded) == asdict(result)
    assert reloaded.cases[0].__class__ is differ.CaseDiff
    assert reloaded.by_label(LABEL_REGRESSED)[0].case_id == "book_happy"


# ---------------------------------------------------------------------------
# report.py smoke tests
# ---------------------------------------------------------------------------


def render(result: differ.DiffResult, verdict: dict[str, Any] | None = None) -> str:
    buf = StringIO()
    report.render_diff(result, console=Console(file=buf, width=200, no_color=True), verdict=verdict)
    return buf.getvalue()


def test_render_diff_smoke(synthetic_runs: tuple[Path, Path]) -> None:
    out = render(diff_runs(*synthetic_runs))
    assert "gpt-5.5 @ chat_completions" in out
    assert "gpt-5.6-sol @ chat_completions" in out
    assert "SIMULATED PROVIDER" in out  # provider is "sim", not "openai"
    assert "book_happy" in out
    assert "regressed" in out
    assert "search_basic" not in out  # stable-pass cases are never rows
    assert "Wilson 95%" in out
    assert "rep_k.json" in out


def test_render_diff_with_each_verdict(synthetic_runs: tuple[Path, Path]) -> None:
    result = diff_runs(*synthetic_runs)
    safe = render(
        result,
        {
            "verdict": "SAFE",
            "restored": 0,
            "regressed_total": 0,
            "broken_by_patch": 0,
            "patch_path": None,
            "repair_log": [],
        },
    )
    assert "SAFE" in safe

    patched = render(
        result,
        {
            "verdict": "SAFE WITH PATCH",
            "restored": 1,
            "regressed_total": 1,
            "broken_by_patch": 0,
            "patch_path": "out/patch.diff",
            "repair_log": ["endpoint_routing: chat_completions -> responses"],
        },
    )
    assert "restored 1/1 regressed, 0 previously-passing broken" in patched
    assert "out/patch.diff" in patched
    assert "endpoint_routing" in patched

    pinned = render(
        result,
        {
            "verdict": "STAY PINNED",
            "restored": 0,
            "regressed_total": 1,
            "broken_by_patch": 0,
            "patch_path": None,
            "repair_log": [],
        },
    )
    assert "STAY PINNED" in pinned
    assert "still regressed: book_happy" in pinned


def test_diff_to_markdown_smoke(synthetic_runs: tuple[Path, Path]) -> None:
    md = report.diff_to_markdown(diff_runs(*synthetic_runs))
    assert "| case | label | base | cand | p | signatures | first failing detail |" in md
    assert "| book_happy | regressed | 5/5 | 0/5 |" in md
    assert "1 stable-pass cases not listed." in md
    assert "search_basic" not in md
    assert "SIMULATED PROVIDER" in md
    assert "p=0.00397 **" in md
    # No emoji anywhere in the report (the middle dot in the counts line is deliberate).
    assert not [ch for ch in md if ord(ch) >= 0x2190 and ch != "•"]


def test_diff_to_markdown_with_verdict(synthetic_runs: tuple[Path, Path]) -> None:
    md = report.diff_to_markdown(
        diff_runs(*synthetic_runs),
        {
            "verdict": "SAFE WITH PATCH",
            "restored": 1,
            "regressed_total": 1,
            "broken_by_patch": 0,
            "patch_path": "out/patch.diff",
            "repair_log": ["step one"],
        },
    )
    assert "## Verdict: SAFE WITH PATCH" in md
    assert "restored 1/1 regressed, 0 previously-passing broken" in md
    assert "repair log:\n\n- step one" in md


def test_report_marks_real_provider_without_warning(tmp_path: Path) -> None:
    a = write_run(tmp_path, "a", {"c1": reps("c1", 5, 5)}, provider="openai")
    b = write_run(tmp_path, "b", {"c1": reps("c1", 5, 5)}, provider="openai")
    result = diff_runs(a, b)
    assert "SIMULATED PROVIDER" not in render(result)
    assert "SIMULATED PROVIDER" not in report.diff_to_markdown(result)
    assert "no non-stable-pass cases" in render(result).lower()


def test_report_significance_stars() -> None:
    assert report._fmt_p(1 / 252) == "p=0.00397 **"
    assert report._fmt_p(0.08333333) == "p=0.0833 *"
    assert report._fmt_p(2 / 9) == "p=0.222"
    assert report._fmt_p(1.0) == "p=1"


def test_report_detail_truncation() -> None:
    long = "x" * 200
    assert len(report._truncate(long)) == report.DETAIL_WIDTH
    assert report._truncate(long).endswith("...")
    assert report._truncate("short") == "short"


# ---------------------------------------------------------------------------
# A baseline that never worked is not a SAFE upgrade
# ---------------------------------------------------------------------------


def test_a_baseline_with_no_passing_case_is_baseline_broken_not_safe(tmp_path: Path) -> None:
    """The safety net under rescue-ops `A-075` §6.3(a).

    When the baseline model passes 0 of N on every case, nothing was measured: there is no
    regression to find, so the differ counts zero and the verdict read `SAFE` — "the
    candidate model is a drop-in replacement" — over a suite that never worked once. That is
    a false pass in the one direction the product must never fail. `BASELINE_BROKEN` is what
    the runbook already calls it, and it is terminal.
    """
    baseline = write_run(tmp_path, "base", {"a": reps("a", 0, 5), "b": reps("b", 0, 5)})
    candidate = write_run(tmp_path, "cand", {"a": reps("a", 0, 5), "b": reps("b", 0, 5)})
    result = diff_runs(baseline, candidate)

    assert result.counts == {LABEL_STABLE_FAIL: 2}
    assert result.baseline_passing_cases() == 0
    assert verdict.decide(result)["verdict"] == verdict.BASELINE_BROKEN


def test_one_passing_baseline_case_is_enough_to_have_measured_something(tmp_path: Path) -> None:
    """The guard is "nothing worked", not "something failed": a suite with one working case
    and one broken one still measures the candidate against the working one."""
    baseline = write_run(tmp_path, "base", {"a": reps("a", 5, 5), "b": reps("b", 0, 5)})
    candidate = write_run(tmp_path, "cand", {"a": reps("a", 5, 5), "b": reps("b", 0, 5)})
    result = diff_runs(baseline, candidate)

    assert result.baseline_passing_cases() == 1
    assert verdict.decide(result)["verdict"] == verdict.SAFE


def test_baseline_broken_says_so_instead_of_drop_in_replacement(tmp_path: Path) -> None:
    baseline = write_run(tmp_path, "base", {"a": reps("a", 0, 5)})
    candidate = write_run(tmp_path, "cand", {"a": reps("a", 0, 5)})
    result = diff_runs(baseline, candidate)
    decided = verdict.decide(result)

    markdown = report.diff_to_markdown(result, verdict=decided)
    assert "drop-in replacement" not in markdown
    assert verdict.BASELINE_BROKEN in markdown
    console = Console(file=StringIO(), width=100, no_color=True)
    report.render_diff(result, console=console, verdict=decided)
    assert "drop-in replacement" not in console.file.getvalue()


def test_a_regression_still_outranks_the_baseline_guard(tmp_path: Path) -> None:
    """A suite with a regression has a passing baseline case by definition, so the guard can
    never mask a STAY PINNED."""
    baseline = write_run(tmp_path, "base", {"a": reps("a", 5, 5)})
    candidate = write_run(tmp_path, "cand", {"a": reps("a", 0, 5)})
    result = diff_runs(baseline, candidate)

    assert verdict.decide(result)["verdict"] == verdict.STAY_PINNED


def test_passing_cases_counts_one_run_the_way_the_diff_would(tmp_path: Path) -> None:
    """`upshift upgrade` asks this between the two legs, so it must agree with the diff that
    is computed after them."""
    run = write_run(tmp_path, "base", {"a": reps("a", 5, 5), "b": reps("b", 2, 5),
                                       "c": reps("c", 0, 5)})
    assert differ.passing_cases(run) == 1

    broken = write_run(tmp_path, "broken", {"a": reps("a", 0, 5)})
    assert differ.passing_cases(broken) == 0
