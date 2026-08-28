"""Tests for the deterministic check engine: every check type, both directions."""

from __future__ import annotations

import pytest

from upshift.checks import evaluate_checks
from upshift.schemas import Case, ToolExecution

STATE = {
    "flights": [
        {
            "flight_id": "UA771",
            "origin": "SEA",
            "destination": "DEN",
            "date": "2026-04-12",
            "depart_time": "09:20",
            "price": 168,
            "stops": 0,
            "seats_available": 5,
        }
    ],
    "bookings": [
        {"booking_id": "UPS-1001", "flight_id": "UA771", "passenger": "Maria Lopez", "status": "confirmed"}
    ],
}


def case(*checks):
    return Case(
        id="t",
        description="",
        initial_state={},
        user_messages=["hi"],
        checks=list(checks),
    )


def execution(name, arguments, result=None, turn=0, segment=0):
    return ToolExecution(
        turn=turn, segment=segment, name=name, arguments=arguments, result=result or {"ok": True}
    )


def run(checks, *, api_error=None, executions=None, state=None, message=""):
    return evaluate_checks(
        case(*checks),
        api_error=api_error,
        tool_executions=executions or [],
        final_state=STATE if state is None else state,
        final_message=message,
    )


def only(checks, **kw):
    results, passed = run(checks, **kw)
    assert len(results) == 1
    return results[0], passed


# ---------------------------------------------------------------------------
# hard rule: an errored episode is never judged on anything else
# ---------------------------------------------------------------------------


def test_api_error_short_circuits_every_other_check():
    checks = [
        {"type": "no_api_error"},
        {"type": "tool_called", "name": "book_flight"},
        {"type": "bookings_count", "equals": 99},
    ]
    error = {
        "status_code": 400,
        "type": "api_error",
        "message": "Function tools with reasoning_effort are not supported",
    }
    results, passed = run(checks, api_error=error, message="anything")
    assert passed is False
    assert len(results) == 1
    assert results[0].check == {"type": "no_api_error"}
    assert results[0].passed is False
    assert "Function tools with reasoning_effort are not supported" in results[0].detail
    assert "400" in results[0].detail


def test_api_error_as_plain_string_is_still_reported():
    results, passed = run([{"type": "no_api_error"}], api_error="connection reset")
    assert passed is False
    assert "connection reset" in results[0].detail


def test_no_api_error_passes_when_there_is_none():
    result, passed = only([{"type": "no_api_error"}])
    assert passed is True
    assert result.passed is True


def test_case_with_no_checks_passes_vacuously():
    results, passed = run([])
    assert results == []
    assert passed is True


def test_overall_pass_requires_every_check():
    results, passed = run(
        [{"type": "no_api_error"}, {"type": "tool_not_called", "name": "book_flight"}],
        executions=[execution("book_flight", {"flight_id": "UA771"})],
    )
    assert [r.passed for r in results] == [True, False]
    assert passed is False


# ---------------------------------------------------------------------------
# tool_called
# ---------------------------------------------------------------------------


def test_tool_called_pass_with_default_min_times():
    result, passed = only(
        [{"type": "tool_called", "name": "book_flight"}],
        executions=[execution("book_flight", {"flight_id": "UA771"})],
    )
    assert passed is True
    assert "book_flight called 1 time" in result.detail


def test_tool_called_fail_when_never_called():
    result, _ = only([{"type": "tool_called", "name": "book_flight"}])
    assert result.passed is False
    assert "called 0 times" in result.detail
    assert "min_times=1" in result.detail


def test_tool_called_min_times_not_met():
    result, _ = only(
        [{"type": "tool_called", "name": "search_flights", "min_times": 2}],
        executions=[execution("search_flights", {"origin": "SEA"})],
    )
    assert result.passed is False
    assert "min_times=2" in result.detail


def test_tool_called_max_times_exceeded_reports_the_count():
    result, _ = only(
        [{"type": "tool_called", "name": "book_flight", "max_times": 1}],
        executions=[
            execution("book_flight", {"flight_id": "UA771"}, turn=0),
            execution("book_flight", {"flight_id": "UA771"}, turn=1),
        ],
    )
    assert result.passed is False
    assert result.detail == "book_flight called 2 times, max_times=1."


def test_tool_called_args_subset_matches_one_of_several_calls():
    executions = [
        execution("search_flights", {"origin": "SEA", "destination": "PDX", "date": "2026-04-12"}),
        execution("search_flights", {"origin": "SEA", "destination": "DEN", "date": "2026-04-12"}, turn=1),
    ]
    check = {
        "type": "tool_called",
        "name": "search_flights",
        "min_times": 2,
        "args_subset": {"destination": "DEN", "date": "2026-04-12"},
    }
    _, passed = only([check], executions=executions)
    assert passed is True


def test_tool_called_args_subset_failure_names_the_calls_seen():
    result, _ = only(
        [{"type": "tool_called", "name": "search_flights", "args_subset": {"origin": "SEA"}}],
        executions=[execution("search_flights", {"origin": "Seattle"})],
    )
    assert result.passed is False
    assert "args_subset" in result.detail
    assert "Seattle" in result.detail


def test_tool_called_args_subset_is_value_exact_not_fuzzy():
    result, _ = only(
        [{"type": "tool_called", "name": "search_flights", "args_subset": {"max_price": 250}}],
        executions=[execution("search_flights", {"max_price": "250"})],
    )
    assert result.passed is False


def test_tool_called_exact_args_pass_and_fail():
    exact = {"flight_id": "UA771", "passenger": "Maria Lopez"}
    ok, _ = only(
        [{"type": "tool_called", "name": "book_flight", "exact_args": exact}],
        executions=[execution("book_flight", dict(exact))],
    )
    assert ok.passed is True

    bad, _ = only(
        [{"type": "tool_called", "name": "book_flight", "exact_args": exact}],
        executions=[execution("book_flight", {**exact, "seat": "12A"})],
    )
    assert bad.passed is False
    assert "exact_args" in bad.detail


# ---------------------------------------------------------------------------
# tool_not_called
# ---------------------------------------------------------------------------


def test_tool_not_called_pass_and_fail():
    ok, _ = only([{"type": "tool_not_called", "name": "book_flight"}])
    assert ok.passed is True

    bad, _ = only(
        [{"type": "tool_not_called", "name": "book_flight"}],
        executions=[execution("book_flight", {"flight_id": "UA771"})],
    )
    assert bad.passed is False
    assert "should not have been" in bad.detail


# ---------------------------------------------------------------------------
# bookings_count
# ---------------------------------------------------------------------------


def test_bookings_count_only_counts_confirmed():
    state = {
        "flights": [],
        "bookings": [
            {"booking_id": "UPS-1001", "flight_id": "A", "passenger": "x", "status": "confirmed"},
            {"booking_id": "UPS-1002", "flight_id": "A", "passenger": "x", "status": "cancelled"},
        ],
    }
    ok, _ = only([{"type": "bookings_count", "equals": 1}], state=state)
    assert ok.passed is True

    bad, _ = only([{"type": "bookings_count", "equals": 2}], state=state)
    assert bad.passed is False
    assert "expected 2" in bad.detail
    assert "UPS-1001" in bad.detail


def test_bookings_count_zero_on_empty_state():
    ok, _ = only([{"type": "bookings_count", "equals": 0}], state={"flights": [], "bookings": []})
    assert ok.passed is True


# ---------------------------------------------------------------------------
# final_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("bookings[0].status", "confirmed"),
        ("bookings[0].booking_id", "UPS-1001"),
        ("flights[0].seats_available", 5),
    ],
)
def test_final_state_resolves_dot_and_bracket_paths(path, expected):
    ok, _ = only([{"type": "final_state", "path": path, "equals": expected}])
    assert ok.passed is True


def test_final_state_wrong_value_reports_both_sides():
    bad, _ = only([{"type": "final_state", "path": "bookings[0].status", "equals": "cancelled"}])
    assert bad.passed is False
    assert "'confirmed'" in bad.detail
    assert "'cancelled'" in bad.detail


@pytest.mark.parametrize(
    "path", ["bookings[5].status", "bookings[0].seat", "nope.status", "flights[0].price[2]"]
)
def test_final_state_unresolvable_path_fails_with_a_reason(path):
    bad, _ = only([{"type": "final_state", "path": path, "equals": "anything"}])
    assert bad.passed is False
    assert "could not be resolved" in bad.detail


# ---------------------------------------------------------------------------
# no_tool_calls_after_success
# ---------------------------------------------------------------------------


def test_no_tool_calls_after_success_passes_when_nothing_follows():
    executions = [
        execution("search_flights", {"origin": "SEA"}, {"results": []}, turn=0),
        execution("book_flight", {"flight_id": "UA771"}, {"booking_id": "UPS-1001"}, turn=1),
    ]
    ok, _ = only([{"type": "no_tool_calls_after_success", "name": "book_flight"}], executions=executions)
    assert ok.passed is True


def test_no_tool_calls_after_success_fails_on_any_later_call():
    executions = [
        execution("book_flight", {"flight_id": "UA771"}, {"booking_id": "UPS-1001"}, turn=0),
        execution("search_flights", {"origin": "SEA"}, {"results": []}, turn=1),
    ]
    bad, _ = only([{"type": "no_tool_calls_after_success", "name": "book_flight"}], executions=executions)
    assert bad.passed is False
    assert "search_flights" in bad.detail
    assert "after book_flight succeeded" in bad.detail


def test_no_tool_calls_after_success_ignores_a_failed_call_of_the_named_tool():
    executions = [
        execution("book_flight", {"flight_id": "ZZ"}, {"error": "unknown flight_id: ZZ"}, turn=0),
        execution("book_flight", {"flight_id": "UA771"}, {"booking_id": "UPS-1001"}, turn=1),
    ]
    ok, _ = only([{"type": "no_tool_calls_after_success", "name": "book_flight"}], executions=executions)
    assert ok.passed is True


def test_no_tool_calls_after_success_is_scoped_to_the_final_segment():
    # A successful book in segment 0 followed by tool calls in segment 1 is fine: the user
    # asked for something else. Only over-acting inside the final segment counts.
    executions = [
        execution("book_flight", {"flight_id": "UA771"}, {"booking_id": "UPS-1001"}, turn=0, segment=0),
        execution("search_flights", {"origin": "SEA"}, {"results": []}, turn=1, segment=1),
        execution("cancel_booking", {"booking_id": "UPS-1001"}, {"status": "cancelled"}, turn=2, segment=1),
    ]
    ok, _ = only([{"type": "no_tool_calls_after_success", "name": "book_flight"}], executions=executions)
    assert ok.passed is True
    assert "never succeeded in the final user segment" in ok.detail

    bad_exec = executions + [
        execution("search_flights", {"origin": "SEA"}, {"results": []}, turn=3, segment=1)
    ]
    bad, _ = only([{"type": "no_tool_calls_after_success", "name": "cancel_booking"}], executions=bad_exec)
    assert bad.passed is False


def test_no_tool_calls_after_success_passes_vacuously_with_no_executions():
    ok, _ = only([{"type": "no_tool_calls_after_success", "name": "book_flight"}])
    assert ok.passed is True


# ---------------------------------------------------------------------------
# confirmation_id_valid
# ---------------------------------------------------------------------------


def test_confirmation_id_valid_passes_for_a_real_id():
    ok, _ = only([{"type": "confirmation_id_valid"}], message="Your confirmation is UPS-1001. Safe travels.")
    assert ok.passed is True


def test_confirmation_id_valid_flags_a_hallucinated_id():
    bad, _ = only([{"type": "confirmation_id_valid"}], message="All set, your number is UPS-4242.")
    assert bad.passed is False
    assert "UPS-4242" in bad.detail
    assert "UPS-1001" in bad.detail


def test_confirmation_id_valid_passes_when_no_id_is_stated():
    ok, _ = only([{"type": "confirmation_id_valid"}], message="Nothing was booked.")
    assert ok.passed is True
    assert "nothing to verify" in ok.detail


def test_confirmation_id_valid_accepts_a_cancelled_booking_id():
    state = {
        "flights": [],
        "bookings": [
            {"booking_id": "UPS-1001", "flight_id": "A", "passenger": "x", "status": "cancelled"}
        ],
    }
    ok, _ = only([{"type": "confirmation_id_valid"}], state=state, message="UPS-1001 is cancelled.")
    assert ok.passed is True


# ---------------------------------------------------------------------------
# response_* checks
# ---------------------------------------------------------------------------


def test_response_contains_is_case_insensitive():
    ok, _ = only([{"type": "response_contains", "text": "ua771"}], message="Booked on UA771.")
    assert ok.passed is True

    bad, _ = only([{"type": "response_contains", "text": "DL220"}], message="Booked on UA771.")
    assert bad.passed is False
    assert "does not contain" in bad.detail


def test_response_not_contains_pass_and_fail():
    ok, _ = only([{"type": "response_not_contains", "text": "hotel"}], message="Flights only.")
    assert ok.passed is True

    bad, _ = only([{"type": "response_not_contains", "text": "hotel"}], message="I booked a Hotel.")
    assert bad.passed is False
    assert "should not" in bad.detail


def test_response_matches_is_dotall_and_ignorecase():
    ok, _ = only(
        [{"type": "response_matches", "regex": r"cheapest.*\$168"}],
        message="The cheapest option\nis UA771\nat $168.",
    )
    assert ok.passed is True

    bad, _ = only([{"type": "response_matches", "regex": r"\?"}], message="No question here.")
    assert bad.passed is False
    assert "does not match" in bad.detail


def test_response_matches_invalid_regex_fails_instead_of_raising():
    bad, _ = only([{"type": "response_matches", "regex": "([unclosed"}], message="anything")
    assert bad.passed is False
    assert "invalid" in bad.detail


# ---------------------------------------------------------------------------
# malformed checks
# ---------------------------------------------------------------------------


def test_unknown_check_type_fails_loudly():
    bad, _ = only([{"type": "vibes_ok"}])
    assert bad.passed is False
    assert "unknown check type" in bad.detail


def test_malformed_check_fails_instead_of_crashing_the_run():
    bad, _ = only([{"type": "tool_called"}])  # no name
    assert bad.passed is False
    assert "could not be evaluated" in bad.detail


def test_every_detail_is_a_short_sentence():
    results, _ = run(
        [
            {"type": "no_api_error"},
            {"type": "tool_called", "name": "book_flight"},
            {"type": "bookings_count", "equals": 1},
            {"type": "confirmation_id_valid"},
        ],
        message="Confirmed: UPS-1001.",
    )
    for result in results:
        assert result.detail
        assert result.detail[-1] in ".!"
        assert len(result.detail) < 300
