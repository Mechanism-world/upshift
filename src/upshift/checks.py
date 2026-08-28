"""Deterministic check engine. See DESIGN.md "Checks"; no LLM judge in v1.

``evaluate_checks`` turns one episode (its API error, tool executions, final backend state and
final assistant message) into a list of :class:`~upshift.schemas.CheckResult` plus an overall
pass bool. A case passes a rep iff every one of its checks passes.

Every result carries a short human-readable ``detail`` because these strings end up verbatim in
the terminal report an engineer uses to decide whether to ship a model upgrade.
"""

from __future__ import annotations

import re
from typing import Any

from upshift.schemas import Case, CheckResult, ToolExecution

CONFIRMATION_RE = re.compile(r"UPS-\d+")

CHECK_TYPES = (
    "no_api_error",
    "tool_called",
    "tool_not_called",
    "bookings_count",
    "final_state",
    "no_tool_calls_after_success",
    "confirmation_id_valid",
    "response_contains",
    "response_not_contains",
    "response_matches",
)


def evaluate_checks(
    case: Case,
    *,
    api_error: dict[str, Any] | str | None,
    tool_executions: list[ToolExecution],
    final_state: dict[str, Any],
    final_message: str,
) -> tuple[list[CheckResult], bool]:
    """Evaluate every check on a case against one episode.

    Hard rule: an episode that ended in an API error is reported as a single failed
    ``no_api_error`` check. Nothing else is evaluated, because tool executions and the final
    message of an errored episode are not evidence about behavior.
    """
    if api_error is not None:
        detail = f"API call failed: {_api_error_message(api_error)}"
        return [CheckResult(check={"type": "no_api_error"}, passed=False, detail=detail)], False

    executions = list(tool_executions or [])
    state = final_state or {}
    message = final_message or ""

    results = [_evaluate_one(check, executions, state, message) for check in case.checks]
    return results, all(r.passed for r in results)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _evaluate_one(
    check: dict[str, Any],
    executions: list[ToolExecution],
    state: dict[str, Any],
    message: str,
) -> CheckResult:
    check_type = check.get("type")
    handler = _HANDLERS.get(check_type)
    if handler is None:
        return CheckResult(
            check=check,
            passed=False,
            detail=f"unknown check type {check_type!r}; known types are {', '.join(CHECK_TYPES)}.",
        )
    try:
        passed, detail = handler(check, executions, state, message)
    except Exception as exc:  # noqa: BLE001 - a malformed check fails loudly, never crashes a run
        return CheckResult(check=check, passed=False, detail=f"check could not be evaluated: {exc}")
    return CheckResult(check=check, passed=passed, detail=detail)


def _check_no_api_error(check, executions, state, message) -> tuple[bool, str]:
    # Reached only when api_error is None (the hard rule short-circuits otherwise).
    return True, "No API error: the episode completed against the model endpoint."


def _check_tool_called(check, executions, state, message) -> tuple[bool, str]:
    name = check["name"]
    min_times = check.get("min_times", 1)
    max_times = check.get("max_times")
    matching = [e for e in executions if e.name == name]
    count = len(matching)

    if count < min_times:
        return False, f"{name} called {_times(count)}, min_times={min_times}."
    if max_times is not None and count > max_times:
        return False, f"{name} called {_times(count)}, max_times={max_times}."

    if "args_subset" in check:
        subset = check["args_subset"]
        if not any(_contains_subset(e.arguments, subset) for e in matching):
            return False, (
                f"no {name} call matched args_subset {_fmt(subset)}; "
                f"calls were {_fmt([e.arguments for e in matching])}."
            )
    if "exact_args" in check:
        exact = check["exact_args"]
        if not any(e.arguments == exact for e in matching):
            return False, (
                f"no {name} call had exact_args {_fmt(exact)}; "
                f"calls were {_fmt([e.arguments for e in matching])}."
            )

    bounds = f"min_times={min_times}" + (f", max_times={max_times}" if max_times is not None else "")
    return True, f"{name} called {_times(count)} with the expected arguments ({bounds})."


def _check_tool_not_called(check, executions, state, message) -> tuple[bool, str]:
    name = check["name"]
    matching = [e for e in executions if e.name == name]
    if matching:
        return False, (
            f"{name} was called {_times(len(matching))} but should not have been; "
            f"first call was {_fmt(matching[0].arguments)}."
        )
    return True, f"{name} was never called, as required."


def _check_bookings_count(check, executions, state, message) -> tuple[bool, str]:
    expected = check["equals"]
    bookings = state.get("bookings") or []
    confirmed = [b for b in bookings if b.get("status") == "confirmed"]
    if len(confirmed) != expected:
        ids = ", ".join(str(b.get("booking_id")) for b in confirmed) or "none"
        return False, (
            f"final state holds {len(confirmed)} confirmed booking(s) ({ids}), expected {expected}."
        )
    return True, f"final state holds {len(confirmed)} confirmed booking(s), as expected."


def _check_final_state(check, executions, state, message) -> tuple[bool, str]:
    path = check["path"]
    expected = check["equals"]
    ok, value, why = _resolve_path(state, path)
    if not ok:
        return False, f"final_state path {path!r} could not be resolved: {why}."
    if value != expected:
        return False, f"final_state {path} is {value!r}, expected {expected!r}."
    return True, f"final_state {path} is {value!r}, as expected."


def _check_no_tool_calls_after_success(check, executions, state, message) -> tuple[bool, str]:
    name = check["name"]
    if not executions:
        return True, f"no tools ran at all, so nothing followed a successful {name}."

    final_segment = max(e.segment for e in executions)
    in_segment = [e for e in executions if e.segment == final_segment]

    successes = [e for e in in_segment if e.name == name and "error" not in e.result]
    first_success = successes[0] if successes else None
    if first_success is None:
        return True, (
            f"{name} never succeeded in the final user segment, so the over-acting rule "
            "does not apply here."
        )

    after = [e for e in in_segment if e.turn > first_success.turn]
    if after:
        names = ", ".join(sorted({e.name for e in after}))
        return False, (
            f"{len(after)} tool call(s) ({names}) ran after {name} succeeded on turn "
            f"{first_success.turn} of the final user segment."
        )
    return True, f"no tool calls followed the successful {name} in the final user segment."


def _check_confirmation_id_valid(check, executions, state, message) -> tuple[bool, str]:
    mentioned = CONFIRMATION_RE.findall(message)
    if not mentioned:
        return True, "final message states no confirmation number, so there is nothing to verify."
    known = {str(b.get("booking_id")) for b in (state.get("bookings") or [])}
    bogus = [m for m in dict.fromkeys(mentioned) if m not in known]
    if bogus:
        return False, (
            f"final message states confirmation number(s) {', '.join(bogus)} that do not exist "
            f"in backend state (known: {', '.join(sorted(known)) or 'none'})."
        )
    return True, (
        f"every confirmation number stated ({', '.join(dict.fromkeys(mentioned))}) exists in "
        "backend state."
    )


def _check_response_contains(check, executions, state, message) -> tuple[bool, str]:
    text = check["text"]
    if text.lower() not in message.lower():
        return False, f"final message does not contain {text!r}."
    return True, f"final message contains {text!r}, as expected."


def _check_response_not_contains(check, executions, state, message) -> tuple[bool, str]:
    text = check["text"]
    if text.lower() in message.lower():
        return False, f"final message contains {text!r} but should not."
    return True, f"final message avoids {text!r}, as expected."


def _check_response_matches(check, executions, state, message) -> tuple[bool, str]:
    pattern = check["regex"]
    try:
        compiled = re.compile(pattern, re.DOTALL | re.IGNORECASE)
    except re.error as exc:
        return False, f"regex {pattern!r} is invalid: {exc}."
    match = compiled.search(message)
    if not match:
        return False, f"final message does not match regex {pattern!r}."
    return True, f"final message matches regex {pattern!r} (at {match.group(0)!r})."


_HANDLERS = {
    "no_api_error": _check_no_api_error,
    "tool_called": _check_tool_called,
    "tool_not_called": _check_tool_not_called,
    "bookings_count": _check_bookings_count,
    "final_state": _check_final_state,
    "no_tool_calls_after_success": _check_no_tool_calls_after_success,
    "confirmation_id_valid": _check_confirmation_id_valid,
    "response_contains": _check_response_contains,
    "response_not_contains": _check_response_not_contains,
    "response_matches": _check_response_matches,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PART_RE = re.compile(r"^([^\[\]]*)((?:\[-?\d+\])*)$")
_INDEX_RE = re.compile(r"\[(-?\d+)\]")


def _parse_path(path: str) -> list[str | int]:
    """"bookings[0].status" -> ["bookings", 0, "status"]."""
    tokens: list[str | int] = []
    for part in str(path).split("."):
        if not part:
            continue
        match = _PART_RE.match(part)
        if match is None:
            raise ValueError(f"malformed path segment {part!r}")
        key, indexes = match.group(1), match.group(2)
        if key:
            tokens.append(key)
        tokens.extend(int(i) for i in _INDEX_RE.findall(indexes))
    if not tokens:
        raise ValueError("path is empty")
    return tokens


def _resolve_path(root: Any, path: str) -> tuple[bool, Any, str]:
    """Resolve a dot/bracket path. Returns (ok, value, reason_if_not_ok)."""
    try:
        tokens = _parse_path(path)
    except ValueError as exc:
        return False, None, str(exc)
    current = root
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list):
                return False, None, f"index [{token}] applied to {type(current).__name__}"
            if not -len(current) <= token < len(current):
                return False, None, f"index [{token}] out of range for a list of {len(current)}"
            current = current[token]
        else:
            if not isinstance(current, dict):
                return False, None, f"key {token!r} applied to {type(current).__name__}"
            if token not in current:
                return False, None, f"key {token!r} is not present"
            current = current[token]
    return True, current, ""


def _contains_subset(arguments: dict[str, Any], subset: dict[str, Any]) -> bool:
    if not isinstance(arguments, dict):
        return False
    return all(key in arguments and arguments[key] == value for key, value in subset.items())


def _api_error_message(api_error: dict[str, Any] | str) -> str:
    if isinstance(api_error, dict):
        message = api_error.get("message") or api_error.get("error") or ""
        status = api_error.get("status_code")
        text = str(message) if message else str(api_error)
        return f"{text} (status {status})" if status else text
    return str(api_error)


def _times(count: int) -> str:
    return "1 time" if count == 1 else f"{count} times"


def _fmt(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."
