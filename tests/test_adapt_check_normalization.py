"""Regression: the first live adapt run (shell_gpt, 2026-09-01) emitted checks keyed
`value` instead of `text`; the engine failed every rep with "could not be evaluated" and
the sim oracle alignment never fired. Generated checks must use checks.py's exact
parameter vocabulary or be dropped with a reason — never passed through."""

from __future__ import annotations

from upshift.adapt.generate import _clean_checks, _normalize_check
from upshift.checks import evaluate_checks
from upshift.schemas import Case, ToolExecution


def test_value_synonym_normalizes_to_text():
    check, reason = _normalize_check({"type": "response_contains", "value": "4"})
    assert reason is None
    assert check == {"type": "response_contains", "text": "4"}


def test_pattern_synonym_normalizes_to_regex():
    check, reason = _normalize_check({"type": "response_matches", "pattern": r"\b4\b"})
    assert reason is None
    assert check == {"type": "response_matches", "regex": r"\b4\b"}


def test_unknown_param_drops_check_with_reason():
    check, reason = _normalize_check({"type": "response_contains", "flavor": "spicy"})
    assert check is None
    assert "unknown parameter" in reason


def test_missing_required_param_drops_check():
    check, reason = _normalize_check({"type": "final_state", "path": "x"})
    assert check is None
    assert "missing required" in reason


def test_clean_checks_normalizes_and_engine_evaluates(tmp_path):
    checks, dropped = _clean_checks(
        [
            {"type": "response_contains", "value": "hello"},
            {"type": "tool_called", "tool": "greet"},
            {"type": "response_matches", "flavor": "nope"},
        ],
        tool_names={"greet"},
        expected=[],
    )
    assert dropped == ["response_matches has unknown parameter 'flavor'"]
    # Every surviving check must be evaluable by the real engine (no "could not be
    # evaluated" details), which is the property the live run violated.
    case = Case(
        id="c",
        description="",
        initial_state={},
        user_messages=["hi"],
        checks=checks,
    )
    results, passed = evaluate_checks(
        case,
        api_error=None,
        tool_executions=[ToolExecution(name="greet", arguments={}, result={},
                                       turn=0, segment=0)],
        final_state={},
        final_message="hello",
    )
    assert passed, [r.detail for r in results]
    assert not any("could not be evaluated" in (r.detail or "") for r in results)
