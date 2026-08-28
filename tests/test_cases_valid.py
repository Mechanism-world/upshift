"""Validation of the eval suite itself.

Two jobs. First, structural: 38 cases, well-formed checks, declared sim vulnerabilities that
the case's own checks can actually detect, no repair marker phrases leaking into the base
prompt or tool schemas. Second, and more important, an end-to-end smoke test that executes
every case's oracle plan against the real backend and asserts the case's own checks pass. That
last test is what proves the plans, the backend and the check engine are mutually consistent:
if a sim run of the baseline model ever fails a case, the fault is in the sim, not the fixture.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift.checks import evaluate_checks
from upshift.schemas import AgentConfig, Case, ToolExecution
from victim.booking_agent.backend import create_backend

AGENT_DIR = ROOT / "victim" / "booking_agent"
CASES_PATH = AGENT_DIR / "cases" / "cases.json"

EXPECTED_CASE_COUNT = 38
EXPECTED_FLAKY_CASES = 2

# Reserved for repair patches: if any of these already sit in the base prompt or tool schemas,
# the corresponding repair candidate is a no-op and the repair loop proves nothing.
RESERVED_MARKERS = (
    "exactly once",
    "at most once",
    "stop once the task is complete",
    "never state a confirmation number",
    "must be called",
)

VULNERABILITIES = ("duplicate_call", "over_acting", "skip_tool")

CASES = Case.load_all(CASES_PATH)
TOOL_NAMES = {t["function"]["name"] for t in json.loads((AGENT_DIR / "tools.json").read_text())}

REF_ARG_RE = re.compile(r"^\$ref:(.+)$")
REF_TEXT_RE = re.compile(r"\{ref:([^}]+)\}")
REF_HEAD_RE = re.compile(r"^(?P<tool>[A-Za-z_][A-Za-z0-9_]*)\[(?P<index>\d+)\](?:\.(?P<path>.+))?$")


def ids(cases):
    return [c.id for c in cases]


@pytest.fixture(params=CASES, ids=ids(CASES))
def case(request) -> Case:
    return request.param


# ---------------------------------------------------------------------------
# Suite shape
# ---------------------------------------------------------------------------


def test_suite_has_the_expected_number_of_cases():
    assert len(CASES) == EXPECTED_CASE_COUNT


def test_case_ids_are_unique_and_slug_shaped():
    assert len(set(ids(CASES))) == len(CASES)
    for case_id in ids(CASES):
        assert re.fullmatch(r"[a-z0-9_]+", case_id), case_id


def test_suite_covers_happy_edge_and_exact_argument_families():
    families = {"happy": 0, "edge": 0, "exact": 0}
    for case_id in ids(CASES):
        families[case_id.split("_", 1)[0]] += 1
    assert families == {"happy": 14, "edge": 10, "exact": 14}


def test_agent_config_loads_with_this_case_suite():
    config = AgentConfig.load(AGENT_DIR)
    assert config.name == "booking-agent"
    assert config.endpoint == "chat_completions"
    assert {t["function"]["name"] for t in config.tools} == {
        "search_flights",
        "book_flight",
        "cancel_booking",
    }


# ---------------------------------------------------------------------------
# Per-case structure
# ---------------------------------------------------------------------------


def test_case_has_description_and_one_to_three_user_messages(case):
    assert case.description.strip()
    assert 1 <= len(case.user_messages) <= 3
    assert all(m.strip() for m in case.user_messages)


def test_case_initial_state_is_a_small_realistic_fixture(case):
    flights = case.initial_state["flights"]
    bookings = case.initial_state["bookings"]
    assert 3 <= len(flights) <= 8, case.id
    assert 0 <= len(bookings) <= 2, case.id
    seen = set()
    for flight in flights:
        assert set(flight) == {
            "flight_id",
            "origin",
            "destination",
            "date",
            "depart_time",
            "price",
            "stops",
            "seats_available",
        }
        assert re.fullmatch(r"[A-Z]{3}", flight["origin"])
        assert re.fullmatch(r"[A-Z]{3}", flight["destination"])
        assert re.fullmatch(r"2026-\d{2}-\d{2}", flight["date"])
        assert re.fullmatch(r"\d{2}:\d{2}", flight["depart_time"])
        assert flight["price"] > 0 and flight["stops"] >= 0 and flight["seats_available"] >= 0
        assert flight["flight_id"] not in seen
        seen.add(flight["flight_id"])
    for booking in bookings:
        assert set(booking) == {"booking_id", "flight_id", "passenger", "status"}
        assert booking["status"] in {"confirmed", "cancelled"}


def test_first_check_is_always_no_api_error(case):
    assert case.checks, case.id
    assert case.checks[0] == {"type": "no_api_error"}, case.id


def test_checks_reference_only_real_tools(case):
    for check in case.checks:
        if "name" in check and check["type"] != "final_state":
            assert check["name"] in TOOL_NAMES, (case.id, check)


# ---------------------------------------------------------------------------
# sim block
# ---------------------------------------------------------------------------


def test_oracle_plan_is_non_empty_and_ends_with_a_message(case):
    plan = case.sim["oracle_plan"]
    assert plan, case.id
    assert "final_message" in plan[-1], case.id
    assert plan[-1]["final_message"].strip()
    for step in plan:
        assert ("tool_calls" in step) ^ ("final_message" in step), (case.id, step)
        for tool_call in step.get("tool_calls", []):
            assert tool_call["name"] in TOOL_NAMES, (case.id, tool_call)
            assert isinstance(tool_call["arguments"], dict)


def test_oracle_plan_has_one_message_turn_per_user_message(case):
    message_turns = [s for s in case.sim["oracle_plan"] if "final_message" in s]
    assert len(message_turns) == len(case.user_messages), case.id


def test_oracle_plan_references_point_at_real_tools(case):
    for tool, index, path in _refs_in_plan(case.sim["oracle_plan"]):
        assert tool in TOOL_NAMES, (case.id, tool)
        assert index >= 0
        assert path is None or path.strip()


def test_declared_vulnerabilities_are_known_and_at_most_one(case):
    vulnerable_to = case.sim["vulnerable_to"]
    assert isinstance(vulnerable_to, list)
    assert all(v in VULNERABILITIES for v in vulnerable_to), (case.id, vulnerable_to)
    assert len(vulnerable_to) <= 1, case.id


def test_declared_vulnerabilities_are_detectable_by_this_cases_checks(case):
    checks = case.checks
    for vulnerability in case.sim["vulnerable_to"]:
        if vulnerability == "duplicate_call":
            detectable = any(c["type"] == "bookings_count" for c in checks) or any(
                c["type"] == "tool_called" and "max_times" in c for c in checks
            )
        elif vulnerability == "over_acting":
            detectable = any(c["type"] == "no_tool_calls_after_success" for c in checks)
        elif vulnerability == "skip_tool":
            detectable = any(c["type"] == "confirmation_id_valid" for c in checks) and any(
                c["type"] == "tool_called" and c.get("name") == "book_flight" for c in checks
            )
        else:  # pragma: no cover - guarded by the previous test
            detectable = False
        assert detectable, f"{case.id} declares {vulnerability} but no check can detect it"


def test_vulnerability_distribution_is_deliberate():
    counts = {v: 0 for v in VULNERABILITIES}
    for case_ in CASES:
        for vulnerability in case_.sim["vulnerable_to"]:
            counts[vulnerability] += 1
    assert counts == {"duplicate_call": 10, "over_acting": 4, "skip_tool": 4}


def test_exactly_two_flaky_cases_and_they_declare_no_vulnerability():
    flaky = [c for c in CASES if "flaky" in c.sim]
    assert len(flaky) == EXPECTED_FLAKY_CASES, ids(flaky)
    for case_ in flaky:
        assert case_.sim["flaky"] == {"rate": 0.4, "mode": "skip_final_tool"}
        assert case_.sim["vulnerable_to"] == []
        assert case_.id.startswith("happy_")


# ---------------------------------------------------------------------------
# Repair markers must not already be present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", RESERVED_MARKERS)
def test_system_prompt_is_free_of_reserved_repair_markers(marker):
    prompt = (AGENT_DIR / "system_prompt.txt").read_text().lower()
    assert marker not in prompt


@pytest.mark.parametrize("marker", RESERVED_MARKERS)
def test_tool_descriptions_are_free_of_reserved_repair_markers(marker):
    for description in _tool_descriptions():
        assert marker not in description.lower(), description


def test_tool_schemas_are_well_formed():
    tools = json.loads((AGENT_DIR / "tools.json").read_text())
    assert len(tools) == 3
    required_by_tool = {
        "search_flights": ["origin", "destination", "date"],
        "book_flight": ["flight_id", "passenger"],
        "cancel_booking": ["booking_id"],
    }
    for tool in tools:
        assert tool["type"] == "function"
        function = tool["function"]
        assert function["description"].strip()
        params = function["parameters"]
        assert params["type"] == "object"
        assert params["required"] == required_by_tool[function["name"]]
        for name in params["required"]:
            assert name in params["properties"]
        for schema in params["properties"].values():
            assert schema["description"].strip()


# ---------------------------------------------------------------------------
# End-to-end: oracle plan -> backend -> checks
# ---------------------------------------------------------------------------


def test_oracle_plan_satisfies_its_own_checks(case):
    executions, final_state, final_message = _run_oracle_plan(case)
    results, passed = evaluate_checks(
        case,
        api_error=None,
        tool_executions=executions,
        final_state=final_state,
        final_message=final_message,
    )
    failures = [f"{r.check['type']}: {r.detail}" for r in results if not r.passed]
    assert passed, f"{case.id} oracle plan fails its own checks:\n  " + "\n  ".join(failures)


def test_oracle_plan_never_hits_an_unexpected_backend_error(case):
    """Backend errors in an oracle plan are deliberate (sold-out, unknown id, already
    cancelled). Any other failure means the fixture and the plan have drifted apart."""
    expected_error_cases = {
        "edge_sold_out_flight",
        "edge_cancel_nonexistent",
        "edge_cancel_already_cancelled",
        "edge_book_unknown_flight",
    }
    executions, _, _ = _run_oracle_plan(case)
    errored = [e for e in executions if "error" in e.result]
    if case.id in expected_error_cases:
        assert errored, f"{case.id} is supposed to exercise a backend error path"
    else:
        assert not errored, [(e.name, e.arguments, e.result) for e in errored]


# ---------------------------------------------------------------------------
# Oracle plan execution (the same semantics the sim provider implements)
# ---------------------------------------------------------------------------


def _run_oracle_plan(case: Case) -> tuple[list[ToolExecution], dict, str]:
    backend = create_backend(case.initial_state)
    executions: list[ToolExecution] = []
    final_message = ""
    segment = 0
    for turn, step in enumerate(case.sim["oracle_plan"]):
        if "tool_calls" in step:
            for tool_call in step["tool_calls"]:
                arguments = {
                    key: _resolve_argument(value, executions)
                    for key, value in tool_call["arguments"].items()
                }
                result = backend.execute(tool_call["name"], arguments)
                executions.append(
                    ToolExecution(
                        turn=turn,
                        segment=segment,
                        name=tool_call["name"],
                        arguments=arguments,
                        result=result,
                    )
                )
        else:
            final_message = _interpolate(step["final_message"], executions)
            segment += 1
    return executions, backend.state(), final_message


def _resolve_argument(value, executions):
    if isinstance(value, str):
        match = REF_ARG_RE.match(value)
        if match:
            return _resolve_ref(match.group(1), executions)
    return value


def _interpolate(text: str, executions) -> str:
    return REF_TEXT_RE.sub(lambda m: str(_resolve_ref(m.group(1), executions)), text)


def _resolve_ref(ref: str, executions):
    head = REF_HEAD_RE.match(ref.strip())
    assert head, f"malformed ref {ref!r}"
    tool, index, path = head.group("tool"), int(head.group("index")), head.group("path")
    matching = [e for e in executions if e.name == tool]
    assert index < len(matching), f"ref {ref!r} points past the {len(matching)} {tool} call(s) so far"
    current = matching[index].result
    for token in _path_tokens(path or ""):
        current = current[token]
    return current


def _path_tokens(path: str):
    for part in path.split("."):
        if not part:
            continue
        head, *indexes = re.split(r"\[(\d+)\]", part)
        if head:
            yield head
        for index in indexes:
            if index.isdigit():
                yield int(index)


def _refs_in_plan(plan):
    for step in plan:
        for tool_call in step.get("tool_calls", []):
            for value in tool_call["arguments"].values():
                if isinstance(value, str) and (match := REF_ARG_RE.match(value)):
                    yield _split_ref(match.group(1))
        for match in REF_TEXT_RE.finditer(step.get("final_message", "")):
            yield _split_ref(match.group(1))


def _split_ref(ref: str):
    head = REF_HEAD_RE.match(ref.strip())
    assert head, f"malformed ref {ref!r}"
    return head.group("tool"), int(head.group("index")), head.group("path")


def _tool_descriptions():
    for tool in json.loads((AGENT_DIR / "tools.json").read_text()):
        yield tool["function"]["description"]
        for schema in tool["function"]["parameters"]["properties"].values():
            yield schema["description"]
