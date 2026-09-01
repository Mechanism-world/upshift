"""Deterministic check engine. See DESIGN.md "Checks"; no LLM judge in v1.

``evaluate_checks`` turns one episode (its API error, tool executions, final backend state and
final assistant message) into a list of :class:`~upshift.schemas.CheckResult` plus an overall
pass bool. A case passes a rep iff every one of its checks passes.

Every result carries a short human-readable ``detail`` because these strings end up verbatim in
the terminal report an engineer uses to decide whether to ship a model upgrade.
"""

from __future__ import annotations

import json
import re
from typing import Any

from upshift.schemas import Case, CheckResult, ToolExecution

#: Identifier shape looked for by ``confirmation_id_valid`` when a case declares no
#: ``pattern``. It is the victim booking agent's confirmation format; a foreign agent passes
#: its own (e.g. ``{"type": "confirmation_id_valid", "pattern": "TSK-\\d+"}``).
DEFAULT_CONFIRMATION_PATTERN = r"UPS-\d+"
CONFIRMATION_RE = re.compile(DEFAULT_CONFIRMATION_PATTERN)

CHECK_TYPES = (
    "no_api_error",
    "tool_called",
    "tool_not_called",
    "state_count",
    "bookings_count",
    "final_state",
    "no_tool_calls_after_success",
    "confirmation_id_valid",
    "response_contains",
    "response_not_contains",
    "response_matches",
    "turns_at_most",
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
    """Count/argument assertion on one named tool.

    Optional ``retrieval: true`` marks the tool as a retrieval tool. It changes NOTHING about
    pass/fail here; it is read only by differ.py, which uses it to tell a dropped retrieval
    call (``reduced_retrieval_calls``) apart from a generic missing tool call.
    """
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


def assistant_turns(executions: list[ToolExecution]) -> int:
    """Number of assistant turns in an episode.

    Exact definition (contract with ``turns_at_most`` and differ.py's
    ``serialized_tool_calls``): the number of DISTINCT ``ToolExecution.turn`` values in the
    episode — every assistant turn that issued at least one tool call — PLUS ONE for the final
    assistant turn, which produces the final message and issues no tool call. An episode with
    no tool calls at all is therefore 1 turn.

    Consequence worth knowing before writing the check: in a multi-segment case (several
    ``user_messages``) the intermediate answer turns are counted only when they also called a
    tool, so ``turns_at_most`` counts tool-calling turns plus one, not wall-clock replies.
    """
    return len({e.turn for e in executions}) + 1


def _check_turns_at_most(check, executions, state, message) -> tuple[bool, str]:
    limit = check["n"]
    turns = assistant_turns(executions)
    if turns > limit:
        return False, (
            f"episode used {turns} assistant turn(s) (distinct tool-calling turns plus the "
            f"final answer), more than n={limit}."
        )
    return True, f"episode used {turns} assistant turn(s), within n={limit}."


def _check_tool_not_called(check, executions, state, message) -> tuple[bool, str]:
    name = check["name"]
    matching = [e for e in executions if e.name == name]
    if matching:
        return False, (
            f"{name} was called {_times(len(matching))} but should not have been; "
            f"first call was {_fmt(matching[0].arguments)}."
        )
    return True, f"{name} was never called, as required."


def _check_state_count(check, executions, state, message) -> tuple[bool, str]:
    path = check.get("path", "")
    expected = check["equals"]
    where = check.get("where") or {}
    actual = count_state_entries(state, path, where)
    if actual is None:
        return False, (
            f"state_count path {path!r} does not resolve to a list or object in the final state."
        )
    where_text = f" matching {_fmt(where)}" if where else ""
    held = f"final state holds {actual} entr{'y' if actual == 1 else 'ies'} at {path!r}{where_text}"
    if actual != expected:
        return False, f"{held}, expected {expected}."
    return True, f"{held}, as expected."


def _check_bookings_count(check, executions, state, message) -> tuple[bool, str]:
    """Victim-flavored alias of ``state_count`` (path "bookings", where status=confirmed).

    Kept because the committed booking-agent suite and its run records use it; a foreign agent
    should write the equivalent ``state_count`` check instead.
    """
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
    """Id-fabrication detector: every identifier the final message states must be real.

    Optional params (defaults reproduce the victim booking agent exactly):
    ``pattern`` (id shape, default ``UPS-\\d+``), ``state_path`` (where the real ids live in
    the backend state, default ``bookings``), ``id_field`` (default ``booking_id``) and
    ``known_from`` — ``state`` (default), ``tool_results`` or ``both``.
    """
    pattern = str(check.get("pattern", DEFAULT_CONFIRMATION_PATTERN))
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return False, f"confirmation id pattern {pattern!r} is invalid: {exc}."
    mentioned = _matches_in(compiled, message)
    if not mentioned:
        return True, "final message states no confirmation number, so there is nothing to verify."

    known_from = str(check.get("known_from", "state"))
    known: set[str] = set()
    if known_from in ("state", "both"):
        state_path = str(check.get("state_path", "bookings"))
        id_field = str(check.get("id_field", "booking_id"))
        known |= _ids_in_state(state, state_path, id_field)
    if known_from in ("tool_results", "both"):
        known |= _ids_in_tool_results(compiled, executions)
    source = {"state": "backend state", "tool_results": "tool results"}.get(
        known_from, "backend state or tool results"
    )

    bogus = [m for m in mentioned if m not in known]
    if bogus:
        return False, (
            f"final message states confirmation number(s) {', '.join(bogus)} that do not exist "
            f"in {source} (known: {', '.join(sorted(known)) or 'none'})."
        )
    return True, (
        f"every confirmation number stated ({', '.join(mentioned)}) exists in {source}."
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
    "state_count": _check_state_count,
    "bookings_count": _check_bookings_count,
    "final_state": _check_final_state,
    "no_tool_calls_after_success": _check_no_tool_calls_after_success,
    "confirmation_id_valid": _check_confirmation_id_valid,
    "response_contains": _check_response_contains,
    "response_not_contains": _check_response_not_contains,
    "response_matches": _check_response_matches,
    "turns_at_most": _check_turns_at_most,
}


# ---------------------------------------------------------------------------
# Backend-state helpers (shared with differ.py)
# ---------------------------------------------------------------------------


def _state_node(state: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Node at `path` in a backend state; an empty path means the whole state."""
    if not path:
        return True, state
    ok, value, _ = _resolve_path(state, path)
    return ok, value


def _entries_at(state: dict[str, Any], path: str) -> list[Any] | None:
    """Entries of the collection at `path`: a list as-is, an object as its values."""
    ok, node = _state_node(state, path)
    if not ok:
        return None
    if isinstance(node, dict):
        return list(node.values())
    if isinstance(node, list):
        return list(node)
    return None


def count_state_entries(
    state: dict[str, Any], path: str = "", where: dict[str, Any] | None = None
) -> int | None:
    """Entries at `path` matching every key/value in `where`; None if `path` is not a
    collection. Used by the ``state_count`` check and by differ.py's duplicate detector."""
    entries = _entries_at(state or {}, path)
    if entries is None:
        return None
    if not where:
        return len(entries)
    return sum(
        1
        for e in entries
        if isinstance(e, dict) and all(e.get(k) == v for k, v in where.items())
    )


def _matches_in(compiled: re.Pattern[str], text: str) -> list[str]:
    """Distinct whole-match strings, in order of first appearance."""
    return list(dict.fromkeys(m.group(0) for m in compiled.finditer(text or "")))


def _ids_in_state(state: dict[str, Any], path: str, id_field: str) -> set[str]:
    """Identifiers held in the backend state: an object's keys plus each entry's `id_field`
    (or the entry itself when the collection holds bare strings/numbers)."""
    ok, node = _state_node(state or {}, path)
    if not ok:
        return set()
    out: set[str] = set()
    entries: list[Any]
    if isinstance(node, dict):
        out.update(str(key) for key in node)
        entries = list(node.values())
    elif isinstance(node, list):
        entries = list(node)
    else:
        return set()
    for entry in entries:
        if isinstance(entry, dict):
            if id_field in entry:
                out.add(str(entry[id_field]))
        elif isinstance(entry, (str, int)):
            out.add(str(entry))
    return out


def _ids_in_tool_results(compiled: re.Pattern[str], executions: list[ToolExecution]) -> set[str]:
    """Identifiers that some tool actually returned in this episode."""
    out: set[str] = set()
    for execution in executions:
        try:
            blob = json.dumps(execution.result, default=str)
        except (TypeError, ValueError):
            blob = str(execution.result)
        out.update(_matches_in(compiled, blob))
    return out


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
