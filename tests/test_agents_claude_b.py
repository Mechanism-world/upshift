"""Mechanical validation of the two Claude-agent proof targets.

`agents/quickstart-agent` (anthropics/claude-quickstarts `agents/`, MIT) and
`agents/claudette-orders` (AnswerDotAI/claudette's documented `toolloop` example, Apache-2.0).
No API key is read and no API call is made anywhere in this file: everything below runs on the
deterministic sim provider or on the backends directly.

What is proved here:

1. Both directories satisfy the ADAPTER.md contract — they load through `validate_agent_dir`,
   `AgentConfig.load` and `runner.load_backend_factory`, and neither carries a sampling
   parameter (both Fables 400 on those) or a repair marker that would make a candidate a no-op.
2. Each backend is deterministic (same `initial_state` + same call sequence -> identical
   results and identical state) and never raises, whatever it is handed.
3. Every case's checks are evaluable by the real engine, and each case's own oracle plan,
   replayed against the real backend, satisfies its own checks.
4. `run_suite` on `sim-fable-5` passes every case of both suites.
5. `sim-fable-5-1` behaves as designed: the batching cases regress, every failure carries a
   `turns_at_most` detail, and the differ labels them `serialized_tool_calls` — while the cases
   with no batched plan step are untouched. claudette additionally shows the *absence* of the
   forced-`tool_choice` 400: its config never sends one, so its exposure is behavioral only.
6. The sim repair loop closes the quickstart target: `prompt-batch-tool-calls` is accepted and
   the verdict is SAFE WITH PATCH.

Sim results validate the MACHINERY, never the thesis (DESIGN.md, "Sim provider").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift.checks import CHECK_TYPES, evaluate_checks
from upshift.cli import validate_agent_dir
from upshift.differ import SIG_API_ERROR_FORCED_TOOL_CHOICE, SIG_SERIALIZED_TOOL_CALLS, diff_runs
from upshift.providers.sim import SimProvider
from upshift.recorder import load_case_reps, run_dir
from upshift.repair.loop import repair
from upshift.repair.playbook import BATCH_BLOCK, SAMPLING_PARAMS
from upshift.runner import load_backend_factory, run_suite
from upshift.schemas import LABEL_REGRESSED, LABEL_STABLE_PASS, AgentConfig, Case, ToolExecution
from upshift.verdict import SAFE_WITH_PATCH, decide

N_REPS = 5
BASELINE_MODEL = "sim-fable-5"
CANDIDATE_MODEL = "sim-fable-5-1"

QUICKSTART_DIR = ROOT / "agents" / "quickstart-agent"
CLAUDETTE_DIR = ROOT / "agents" / "claudette-orders"

# Repair-candidate marker phrases. If one of these already sat in a base prompt or tool schema,
# the corresponding repair would be a no-op and the sim loop below would prove nothing.
RESERVED_MARKERS = (
    "request every item that doesn't depend on another's result",
    "search before answering",
    "exactly once",
    "at most once",
    "stop once the task is complete",
    "never state a confirmation number",
    "must be called",
)

#: The cases whose oracle plan batches >1 tool call in a single step — the ones the documented
#: `serialize` corruption is expected to break on sim-fable-5-1, and only those.
QUICKSTART_BATCHED = {
    "parallel_three_calculations",
    "parallel_read_three_files",
    "parallel_read_two_then_sum",
    "parallel_read_three_then_write_report",
}
CLAUDETTE_BATCHED = {
    "cancel_all_orders_c1",
    "cancel_then_check_o2_status",
    "parallel_order_and_customer_lookup",
}

TARGETS = {
    "quickstart-agent": (QUICKSTART_DIR, QUICKSTART_BATCHED, 8),
    "claudette-orders": (CLAUDETTE_DIR, CLAUDETTE_BATCHED, 6),
}


def _cases(agent_dir: Path) -> list[Case]:
    return Case.load_all(agent_dir / "cases" / "cases.json")


def _tool_names(agent_dir: Path) -> set[str]:
    tools = json.loads((agent_dir / "tools.json").read_text())
    return {t["function"]["name"] for t in tools}


ALL_CASES = [(name, case) for name, (d, _, _) in TARGETS.items() for case in _cases(d)]
ALL_CASE_IDS = [f"{name}:{case.id}" for name, case in ALL_CASES]


# ---------------------------------------------------------------------------
# The agent directory contract (ADAPTER.md)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_directory_validates_through_the_cli_preflight(name: str) -> None:
    agent_dir, _, expected_cases = TARGETS[name]
    raw = validate_agent_dir(agent_dir)
    assert raw["endpoint"] == "messages"
    assert raw["model"] == "claude-fable-5"
    assert len(_cases(agent_dir)) == expected_cases


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_config_loads_and_carries_no_sampling_params(name: str) -> None:
    """Both upstreams send a sampling parameter (quickstart temperature=1.0, claudette
    temperature=0). Both Fables 400 on non-default sampling params, on baseline AND candidate,
    so keeping one would measure the wrong thing (ATTRIBUTION.md)."""
    agent_dir, _, _ = TARGETS[name]
    config = AgentConfig.load(agent_dir)
    assert config.endpoint == "messages"
    assert config.params.get("max_tokens") == 4096
    for param in SAMPLING_PARAMS:
        assert param not in config.params
    assert "tool_choice" not in config.params


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_no_repair_marker_leaks_into_the_base_prompt_or_schemas(name: str) -> None:
    agent_dir, _, _ = TARGETS[name]
    haystack = (
        (agent_dir / "system_prompt.txt").read_text() + (agent_dir / "tools.json").read_text()
    ).lower()
    for marker in RESERVED_MARKERS:
        assert marker.lower() not in haystack, marker


def test_quickstart_prompt_is_the_notebook_cell_minus_the_web_search_line() -> None:
    """agents/agent_demo.ipynb code cell 5, verbatim, with `1. Web search (...)` deleted and
    nothing else touched — including the numbering and the trailing space after `(calculate)`
    (ATTRIBUTION.md, "What was dropped")."""
    expected = (
        "\n"
        "You are a helpful assistant with access to:\n"
        "2. Mathematical calculator (calculate) \n"
        "3. A tool to think and reason (think)\n"
        "\n"
        "Always use the most appropriate tool for each task.\n"
    )
    assert (QUICKSTART_DIR / "system_prompt.txt").read_bytes() == expected.encode()


def test_claudette_prompt_is_empty_because_the_notebook_passes_no_sp() -> None:
    """`Chat(model, tools=tools)` with claudette's `sp=''` default puts `system: ""` on the
    wire (claudette/core.py:460, 475, 264). An empty file is the extraction result, not a
    placeholder — so assert it is empty AND that no TODO survived."""
    text = (CLAUDETTE_DIR / "system_prompt.txt").read_text()
    assert text == ""
    assert AgentConfig.load(CLAUDETTE_DIR).system_prompt == ""


def test_quickstart_tool_schemas_match_upstreams_own_rendering() -> None:
    """The descriptions are what `Tool.to_dict()` / FastMCP actually emit, indentation and all
    (ATTRIBUTION.md, "The schemas were generated by running upstream's code")."""
    tools = {
        t["function"]["name"]: t["function"] for t in json.loads((QUICKSTART_DIR / "tools.json").read_text())
    }
    assert set(tools) == {"file_read", "file_write", "think", "calculator"}
    # upstream's triple-quoted strings are sent undedented
    assert tools["file_read"]["description"].startswith("\n            Read files")
    assert tools["file_write"]["description"].startswith("\n            Write or edit files")
    # FastMCP hands over the whole docstring and pydantic's generated schema
    assert "Only these exact symbols are supported, not words" in tools["calculator"]["description"]
    assert tools["calculator"]["parameters"]["title"] == "calculatorArguments"
    assert tools["think"]["description"].startswith("Use the tool to think about something.")


def test_claudette_tool_schemas_match_get_schema_output() -> None:
    """Derived by running claudette's own `get_schema` (the call `_precall` makes at
    claudette/core.py:252), so the argument descriptions and cancel_order's return block are
    present (ATTRIBUTION.md)."""
    tools = {
        t["function"]["name"]: t["function"] for t in json.loads((CLAUDETTE_DIR / "tools.json").read_text())
    }
    assert set(tools) == {"get_customer_info", "get_order_details", "cancel_order"}
    assert (
        tools["get_customer_info"]["parameters"]["properties"]["customer_id"]["description"]
        == "ID of the customer"
    )
    assert tools["cancel_order"]["description"] == (
        "Cancels an order based on the provided order ID\n\n"
        "Returns:\n- True if the cancellation is successful (type: boolean)"
    )


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_backend_loads_through_the_runners_own_loader(name: str) -> None:
    agent_dir, _, _ = TARGETS[name]
    factory = load_backend_factory(agent_dir)
    backend = factory({})
    assert isinstance(backend.state(), dict)
    assert callable(backend.execute)


# ---------------------------------------------------------------------------
# Backend: deterministic, never raises
# ---------------------------------------------------------------------------


QUICKSTART_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("file_read", {"operation": "read", "path": "data/a.txt"}),
    ("file_read", {"operation": "read", "path": "data/a.txt", "max_lines": 1}),
    ("file_read", {"operation": "list", "path": "data"}),
    ("file_read", {"operation": "list", "path": "data", "pattern": "*.md"}),
    ("file_read", {"operation": "read", "path": "nope.txt"}),
    ("file_read", {"operation": "read", "path": "data"}),
    ("file_read", {"operation": "sniff", "path": "data/a.txt"}),
    ("file_write", {"operation": "write", "path": "out.txt", "content": "hello"}),
    ("file_write", {"operation": "write", "path": "out.txt"}),
    ("file_write", {"operation": "edit", "path": "out.txt", "old_text": "hello", "new_text": "bye"}),
    ("file_write", {"operation": "edit", "path": "out.txt", "old_text": "zzz", "new_text": "x"}),
    ("file_write", {"operation": "edit", "path": "missing.txt", "old_text": "a", "new_text": "b"}),
    ("calculator", {"number1": 12, "number2": 7, "operator": "*"}),
    ("calculator", {"number1": 1, "number2": 0, "operator": "/"}),
    ("calculator", {"number1": -4, "number2": 0, "operator": "sqrt"}),
    ("calculator", {"number1": 2, "number2": 3, "operator": "%"}),
    ("calculator", {"number1": 2}),
    ("think", {"thought": "hmm"}),
    ("think", {}),
    ("nonexistent_tool", {"x": 1}),
]
QUICKSTART_STATE = {
    "files": {"data/a.txt": "12\n30\n", "data/b.txt": "30\n", "notes.txt": "hello there\n"}
}

CLAUDETTE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("get_customer_info", {"customer_id": "C1"}),
    ("get_customer_info", {"customer_id": "C9"}),
    ("get_customer_info", {}),
    ("get_order_details", {"order_id": "O1"}),
    ("get_order_details", {"order_id": "O9"}),
    ("cancel_order", {"order_id": "O1"}),
    ("cancel_order", {"order_id": "O1"}),
    ("cancel_order", {"order_id": "O9"}),
    ("get_customer_info", {"customer_id": "C1"}),
    ("cancel_order", {"order_id": 17}),
    ("unknown", {}),
]
CLAUDETTE_STATE = json.loads(
    (CLAUDETTE_DIR / "cases" / "cases.json").read_text()
)[0]["initial_state"]

SEQUENCES = {
    "quickstart-agent": (QUICKSTART_DIR, QUICKSTART_STATE, QUICKSTART_CALLS),
    "claudette-orders": (CLAUDETTE_DIR, CLAUDETTE_STATE, CLAUDETTE_CALLS),
}


@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_backend_is_deterministic_over_the_same_call_sequence(name: str) -> None:
    """ADAPTER.md requirement 3: same initial_state + same calls -> same results, same state."""
    agent_dir, initial_state, calls = SEQUENCES[name]
    factory = load_backend_factory(agent_dir)

    def once():
        backend = factory(json.loads(json.dumps(initial_state)))
        results = [backend.execute(n, dict(a)) for n, a in calls]
        return json.dumps(results, sort_keys=True), json.dumps(backend.state(), sort_keys=True)

    first, second = once(), once()
    assert first == second


@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_backend_never_raises(name: str) -> None:
    """Unknown tools, wrong types, missing arguments and non-dict arguments all come back as
    results, never as exceptions (ADAPTER.md requirement 2)."""
    agent_dir, initial_state, calls = SEQUENCES[name]
    backend = load_backend_factory(agent_dir)(json.loads(json.dumps(initial_state)))
    hostile: list[tuple[str, Any]] = [
        *calls,
        ("file_read", None),
        ("get_customer_info", ["not", "a", "dict"]),
        ("", {}),
        ("file_write", {"operation": "write", "path": None, "content": 3}),
        ("cancel_order", {"order_id": None}),
        ("calculator", {"number1": "x", "number2": "y", "operator": "+"}),
    ]
    for tool, arguments in hostile:
        result = backend.execute(tool, arguments)
        assert isinstance(result, dict)
        json.dumps(result)  # must be JSON-encodable: the loop feeds it back verbatim
    assert isinstance(backend.state(), dict)


def test_quickstart_backend_mirrors_upstream_strings() -> None:
    backend = load_backend_factory(QUICKSTART_DIR)(json.loads(json.dumps(QUICKSTART_STATE)))
    assert backend.execute("file_read", {"operation": "read", "path": "data/a.txt"}) == {
        "output": "12\n30\n"
    }
    assert backend.execute(
        "file_read", {"operation": "read", "path": "data/a.txt", "max_lines": 1}
    ) == {"output": "12\n"}
    assert backend.execute("file_read", {"operation": "list", "path": "data"}) == {
        "output": "📄 a.txt\n📄 b.txt"
    }
    assert backend.execute("file_read", {"operation": "read", "path": "gone.txt"}) == {
        "output": "Error: File not found at gone.txt"
    }
    assert backend.execute("calculator", {"number1": 12, "number2": 7, "operator": "*"}) == {
        "output": "Result: 84"
    }
    assert backend.execute("calculator", {"number1": 1, "number2": 0, "operator": "/"}) == {
        "output": "Error: Division by zero"
    }
    assert backend.execute("think", {"thought": "x"}) == {"output": "Thinking complete!"}
    assert backend.state()["write_count"] == 0
    backend.execute("file_write", {"operation": "write", "path": "out.txt", "content": "hi"})
    assert backend.state()["write_count"] == 1
    assert backend.state()["writes"] == ["out.txt"]


def test_claudette_backend_reproduces_upstream_aliasing() -> None:
    """Cell 9 shares the order dicts between `orders` and `customers`, so a cancellation is
    visible through get_customer_info (ATTRIBUTION.md, "Aliasing is reconstructed")."""
    backend = load_backend_factory(CLAUDETTE_DIR)(json.loads(json.dumps(CLAUDETTE_STATE)))
    assert backend.execute("cancel_order", {"order_id": "O1"}) == {"result": True}
    customer = backend.execute("get_customer_info", {"customer_id": "C1"})["result"]
    assert [o["status"] for o in customer["orders"]] == ["Cancelled", "Processing"]
    assert backend.state()["orders"]["O1"]["status"] == "Cancelled"
    assert backend.execute("cancel_order", {"order_id": "O9"}) == {"result": False}
    assert backend.execute("get_customer_info", {"customer_id": "C9"}) == {
        "result": "Customer not found"
    }
    assert backend.execute("get_order_details", {"order_id": "O9"}) == {
        "result": "Order not found"
    }


# ---------------------------------------------------------------------------
# Cases: evaluable checks, and oracle plans that satisfy them against the backend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "case"), ALL_CASES, ids=ALL_CASE_IDS)
def test_case_checks_are_known_and_reference_real_tools(name: str, case: Case) -> None:
    agent_dir, _, _ = TARGETS[name]
    tools = _tool_names(agent_dir)
    assert case.checks, f"{case.id} asserts nothing"
    for check in case.checks:
        assert check["type"] in CHECK_TYPES, check
        if check["type"] in ("tool_called", "tool_not_called"):
            assert check["name"] in tools, check
    assert any(c["type"] == "no_api_error" for c in case.checks)
    assert any(c["type"] == "turns_at_most" for c in case.checks), (
        f"{case.id}: every case here asserts a turn budget — that is the contract this pair of "
        f"targets exists to measure"
    )
    assert case.sim.get("oracle_plan"), f"{case.id} has no sim.oracle_plan"


@pytest.mark.parametrize(("name", "case"), ALL_CASES, ids=ALL_CASE_IDS)
def test_oracle_plan_satisfies_the_cases_own_checks_against_the_real_backend(
    name: str, case: Case
) -> None:
    """Replay each plan straight through `backend.execute` and evaluate the case's checks on
    the result. If this fails, the fixture is inconsistent with itself and no sim run below
    means anything."""
    agent_dir, _, _ = TARGETS[name]
    backend = load_backend_factory(agent_dir)(json.loads(json.dumps(case.initial_state)))
    executions: list[ToolExecution] = []
    final_message = ""
    turn = 0
    for step in case.sim["oracle_plan"]:
        calls = step.get("tool_calls")
        if calls:
            for call in calls:
                result = backend.execute(call["name"], dict(call.get("arguments") or {}))
                executions.append(
                    ToolExecution(turn, len(executions), call["name"], call["arguments"], result)
                )
            turn += 1
        elif "final_message" in step:
            final_message = step["final_message"]
    results, passed = evaluate_checks(
        case,
        api_error=None,
        tool_executions=executions,
        final_state=backend.state(),
        final_message=final_message,
    )
    assert passed, [r.detail for r in results if not r.passed]


# ---------------------------------------------------------------------------
# The sim pipeline, run once per target for the whole module
# ---------------------------------------------------------------------------


class Pipeline:
    def __init__(self, agent_dir: Path, root: Path, tag: str) -> None:
        self.agent_dir = agent_dir
        self.runs = root / "runs"
        provider = SimProvider()
        run_suite(
            agent_dir, provider, f"{tag}-baseline", n_reps=N_REPS,
            model_override=BASELINE_MODEL, runs_root=self.runs, workers=1,
        )
        run_suite(
            agent_dir, provider, f"{tag}-candidate", n_reps=N_REPS,
            model_override=CANDIDATE_MODEL, runs_root=self.runs, workers=1,
        )
        self.baseline_dir = run_dir(self.runs, f"{tag}-baseline")
        self.candidate_dir = run_dir(self.runs, f"{tag}-candidate")
        self.diff = diff_runs(self.baseline_dir, self.candidate_dir)
        self.outcome = repair(
            original_agent_dir=agent_dir,
            work_dir=root / "patched",
            provider=provider,
            candidate_model=CANDIDATE_MODEL,
            baseline_diff=self.diff,
            n_reps=N_REPS,
            runs_root=self.runs,
            run_prefix=tag,
            budget=6,
            workers=1,
        )
        self.verdict = decide(self.diff, self.outcome, patch_path=str(root / "upgrade.patch"))


@pytest.fixture(scope="module")
def quickstart(tmp_path_factory: pytest.TempPathFactory) -> Pipeline:
    return Pipeline(QUICKSTART_DIR, tmp_path_factory.mktemp("quickstart"), "qs")


@pytest.fixture(scope="module")
def claudette(tmp_path_factory: pytest.TempPathFactory) -> Pipeline:
    return Pipeline(CLAUDETTE_DIR, tmp_path_factory.mktemp("claudette"), "cl")


def _assert_baseline_all_pass(pipeline: Pipeline) -> None:
    for case in _cases(pipeline.agent_dir):
        reps = load_case_reps(pipeline.baseline_dir, case.id)
        assert len(reps) == N_REPS
        failures = [
            "; ".join(c.detail for c in r.check_results if not c.passed) for r in reps if not r.passed
        ]
        assert not failures, f"{case.id}: {failures}"


def test_quickstart_baseline_passes_every_case_on_sim_fable_5(quickstart: Pipeline) -> None:
    _assert_baseline_all_pass(quickstart)


def test_claudette_baseline_passes_every_case_on_sim_fable_5(claudette: Pipeline) -> None:
    _assert_baseline_all_pass(claudette)


def _assert_serialization_regression(pipeline: Pipeline, batched: set[str]) -> None:
    """Exactly the cases whose plan batches >1 call in a step regress, each classified
    `serialized_tool_calls` and each failing on its own `turns_at_most` budget."""
    regressed = {c.case_id for c in pipeline.diff.by_label(LABEL_REGRESSED)}
    assert regressed == batched
    for case in pipeline.diff.cases:
        if case.case_id in batched:
            assert case.baseline_passes == N_REPS
            assert case.candidate_passes == 0
            assert case.failure_signatures == [SIG_SERIALIZED_TOOL_CALLS]
            reps = load_case_reps(pipeline.candidate_dir, case.case_id)
            details = [c.detail for r in reps for c in r.check_results if not c.passed]
            assert details and all("assistant turn(s)" in d for d in details), details
        else:
            assert case.label == LABEL_STABLE_PASS


def test_quickstart_candidate_loses_exactly_the_batched_cases(quickstart: Pipeline) -> None:
    _assert_serialization_regression(quickstart, QUICKSTART_BATCHED)


def test_claudette_candidate_loses_exactly_the_batched_cases(claudette: Pipeline) -> None:
    """claudette's config sends no `tool_choice` (nbs/01_toolloop.ipynb passes none;
    claudette/core.py:253 only emits the parameter when truthy), so its 5.1 exposure is
    behavioral only: no forced-tool-choice 400 anywhere in the run."""
    _assert_serialization_regression(claudette, CLAUDETTE_BATCHED)
    for case in claudette.diff.cases:
        assert SIG_API_ERROR_FORCED_TOOL_CHOICE not in case.failure_signatures
    for case in _cases(CLAUDETTE_DIR):
        for rep in load_case_reps(claudette.candidate_dir, case.id):
            assert rep.api_error is None


def test_quickstart_sim_repair_loop_ends_safe_with_patch(quickstart: Pipeline) -> None:
    """The documented batching sentence, appended to the system prompt, restores every
    regressed case with no collateral damage. Sim only: its response to a repair is true by
    construction (DESIGN.md)."""
    outcome = quickstart.outcome
    assert [p.id for p in outcome.accepted_patches] == ["prompt-batch-tool-calls"]
    assert [p.repair_type for p in outcome.accepted_patches] == ["prompt_edit"]
    assert set(outcome.restored) == QUICKSTART_BATCHED
    assert not outcome.unrestored
    assert quickstart.verdict["verdict"] == SAFE_WITH_PATCH
    edit = outcome.accepted_patches[0].edits[0]
    assert edit.file == "system_prompt.txt"
    assert BATCH_BLOCK.strip() in edit.new_content


def test_claudette_sim_repair_loop_ends_safe_with_patch(claudette: Pipeline) -> None:
    outcome = claudette.outcome
    assert [p.id for p in outcome.accepted_patches] == ["prompt-batch-tool-calls"]
    assert set(outcome.restored) == CLAUDETTE_BATCHED
    assert not outcome.unrestored
    assert claudette.verdict["verdict"] == SAFE_WITH_PATCH
