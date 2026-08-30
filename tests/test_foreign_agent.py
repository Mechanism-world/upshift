"""A second, non-booking agent driven through the whole machinery without special-casing.

``tests/todo_agent`` is a foreign agent by every axis the pipeline could accidentally depend
on: a different domain, two tools with different names, ``TSK-<n>`` identifiers instead of
``UPS-<n>``, and a ``{"tasks": [...]}`` state instead of ``{"flights"/"bookings"}``. Nothing in
src/upshift is told about it beyond the ADAPTER.md contract: the five files in the agent dir.

The pipeline exercised here is run_suite (both sim models) -> diff -> failure signatures ->
playbook -> repair loop -> verdict -> patch. See ADAPTER.md, "What the sim provider can and
cannot do for a foreign agent".
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from upshift.checks import evaluate_checks
from upshift.differ import (
    SIG_ACTING_PAST_GOAL,
    SIG_API_ERROR_TOOLS_REASONING,
    SIG_DUPLICATE_TOOL_CALLS,
    SIG_SKIPPED_TOOL_HALLUCINATION,
    SIG_WRONG_OR_MISSING_TOOL_CALL,
    diff_runs,
    failure_signatures,
)
from upshift.patch import make_patch
from upshift.providers.sim import SimProvider
from upshift.recorder import load_case_reps, run_dir
from upshift.repair.loop import repair
from upshift.repair.playbook import generate_candidates
from upshift.runner import run_suite
from upshift.schemas import (
    LABEL_REGRESSED,
    LABEL_STABLE_PASS,
    Case,
    CheckResult,
    RepRecord,
    ToolExecution,
)
from upshift.verdict import SAFE_WITH_PATCH, decide

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "tests" / "todo_agent"
CASES = Case.load_all(AGENT_DIR / "cases" / "cases.json")
CASE_IDS = sorted(c.id for c in CASES)
N_REPS = 5

ALL_SIGNATURES = [
    "api_error_tools_reasoning",
    "api_error_other",
    "duplicate_tool_calls",
    "acting_past_goal",
    "skipped_tool_hallucination",
    "wrong_or_missing_tool_call",
    "other_behavioral",
]


# ---------------------------------------------------------------------------
# The pipeline, run once for the whole module
# ---------------------------------------------------------------------------


class Pipeline:
    def __init__(self, root: Path) -> None:
        self.runs = root / "runs"
        self.work_dir = root / "patched_agent"
        provider = SimProvider()

        run_suite(
            AGENT_DIR, provider, "baseline", n_reps=N_REPS, model_override="sim-5.5",
            runs_root=self.runs, workers=1, notes="foreign agent baseline",
        )
        run_suite(
            AGENT_DIR, provider, "candidate", n_reps=N_REPS, model_override="sim-5.6-sol",
            runs_root=self.runs, workers=1, notes="foreign agent candidate",
        )
        # The same candidate model with the endpoint break already routed away, so the
        # behavioral corruptions (not just the 400) are visible to the differ.
        run_suite(
            AGENT_DIR, provider, "candidate-responses", n_reps=N_REPS,
            model_override="sim-5.6-sol", endpoint_override="responses",
            runs_root=self.runs, workers=1, notes="foreign agent candidate on /v1/responses",
        )

        self.diff = diff_runs(run_dir(self.runs, "baseline"), run_dir(self.runs, "candidate"))
        self.behavioral_diff = diff_runs(
            run_dir(self.runs, "baseline"), run_dir(self.runs, "candidate-responses")
        )
        self.outcome = repair(
            original_agent_dir=AGENT_DIR,
            work_dir=self.work_dir,
            provider=provider,
            candidate_model="sim-5.6-sol",
            baseline_diff=self.diff,
            n_reps=N_REPS,
            runs_root=self.runs,
            run_prefix="foreign",
            budget=6,
            workers=1,
        )
        self.verdict = decide(self.diff, self.outcome, patch_path=str(root / "upgrade.patch"))

    def case(self, diff, case_id):
        return next(c for c in diff.cases if c.case_id == case_id)


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Pipeline:
    return Pipeline(tmp_path_factory.mktemp("foreign-agent"))


# ---------------------------------------------------------------------------
# run_suite + checks
# ---------------------------------------------------------------------------


def test_every_case_passes_on_the_baseline_model(pipeline: Pipeline) -> None:
    """If the foreign fixture cannot pass its own checks on sim-5.5, nothing below means
    anything: the machinery, not the model, would be producing the failures."""
    for case_id in CASE_IDS:
        reps = load_case_reps(run_dir(pipeline.runs, "baseline"), case_id)
        failures = [
            f"{case_id} rep {r.rep}: "
            + "; ".join(c.detail for c in r.check_results if not c.passed)
            for r in reps
            if not r.passed
        ]
        assert len(reps) == N_REPS
        assert not failures, failures


def test_the_documented_api_break_regresses_every_case(pipeline: Pipeline) -> None:
    assert {c.case_id for c in pipeline.diff.by_label(LABEL_REGRESSED)} == set(CASE_IDS)
    for case in pipeline.diff.cases:
        assert case.baseline_passes == N_REPS
        assert case.candidate_passes == 0
        assert case.failure_signatures == [SIG_API_ERROR_TOOLS_REASONING]


def test_state_count_and_final_state_checks_run_against_a_foreign_state_shape() -> None:
    """The generic replacement for the victim-only bookings_count check, on {"tasks": [...]}."""
    case = next(c for c in CASES if c.id == "todo_list_then_add")
    state = {
        "tasks": [
            {"task_id": "TSK-1001", "title": "water the plants", "due": "", "status": "open"},
            {"task_id": "TSK-1002", "title": "buy milk", "due": "", "status": "open"},
        ]
    }
    executions = [
        ToolExecution(0, 0, "list_tasks", {"status": "open"}, {"results": []}),
        ToolExecution(1, 1, "add_task", {"title": "buy milk"}, {"task_id": "TSK-1002"}),
    ]
    _, passed = evaluate_checks(
        case, api_error=None, tool_executions=executions, final_state=state, final_message="done"
    )
    assert passed

    state["tasks"].append({"task_id": "TSK-1003", "title": "buy milk", "status": "open"})
    results, passed = evaluate_checks(
        case, api_error=None, tool_executions=executions, final_state=state, final_message="done"
    )
    assert not passed
    assert any("holds 3 entries at 'tasks'" in r.detail for r in results if not r.passed)


# ---------------------------------------------------------------------------
# Failure signatures on a foreign toolset
# ---------------------------------------------------------------------------


def test_behavioral_signatures_are_detected_without_victim_tool_names(pipeline: Pipeline) -> None:
    """With the 400 routed away, the sim's three corruptions land on add_task/list_tasks and
    are still classified correctly — no signature keys off book_flight or UPS-<n>."""
    diff = pipeline.behavioral_diff
    assert SIG_DUPLICATE_TOOL_CALLS in pipeline.case(diff, "todo_add_one").failure_signatures
    assert SIG_ACTING_PAST_GOAL in pipeline.case(diff, "todo_list_then_add").failure_signatures
    assert pipeline.case(diff, "todo_report_id").failure_signatures == [
        SIG_SKIPPED_TOOL_HALLUCINATION
    ]
    # The one case with no declared vulnerability is unaffected by the candidate model.
    assert pipeline.case(diff, "todo_skip_existing").label == LABEL_STABLE_PASS


def test_the_case_declared_id_pattern_drives_fabrication_detection(pipeline: Pipeline) -> None:
    """todo_report_id declares pattern TSK-\\d+; the fabricated id is caught by that pattern,
    not by the built-in UPS-<n> one."""
    reps = load_case_reps(run_dir(pipeline.runs, "candidate-responses"), "todo_report_id")
    details = [c.detail for r in reps if not r.passed for c in r.check_results if not c.passed]
    assert any("TSK-9" in d for d in details), details
    assert not any("UPS-" in d for d in details), details


def _record(**kwargs: Any) -> RepRecord:
    base: dict[str, Any] = {
        "case_id": "c", "rep": 1, "seed": 1, "model_requested": "sim-5.6-sol",
        "resolved_model": "sim-5.6-sol", "endpoint": "responses", "params": {}, "api_calls": [],
        "tool_executions": [], "final_state": {}, "final_message": "", "check_results": [],
        "passed": False, "api_error": None, "latency_s": 0.1,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return RepRecord(**{**base, **kwargs})


def test_skipped_tool_hallucination_does_not_require_a_tool_named_book_flight() -> None:
    record = _record(
        check_results=[
            CheckResult({"type": "tool_called", "name": "add_task"}, False, "add_task not called"),
            CheckResult(
                {"type": "confirmation_id_valid", "pattern": r"TSK-\d+"}, False, "TSK-9042 unknown"
            ),
        ],
        final_message="Added it. The id is TSK-9042.",
    )
    assert failure_signatures([record]) == [SIG_SKIPPED_TOOL_HALLUCINATION]


def test_a_failed_tool_check_for_a_tool_that_did_run_is_not_a_hallucination() -> None:
    """Precision: the tool succeeded, so a stated identifier is not evidence of fabrication."""
    record = _record(
        check_results=[
            CheckResult(
                {"type": "tool_called", "name": "add_task", "max_times": 1}, False, "called twice"
            )
        ],
        tool_executions=[
            ToolExecution(0, 0, "add_task", {"title": "x"}, {"task_id": "TSK-1001"}),
        ],
        final_message="Added it. The id is TSK-1001.",
    )
    assert failure_signatures([record]) == [SIG_WRONG_OR_MISSING_TOOL_CALL]


# ---------------------------------------------------------------------------
# Playbook: graceful degradation on a foreign toolset
# ---------------------------------------------------------------------------


def test_playbook_generates_candidates_for_a_foreign_toolset_without_crashing() -> None:
    patches = generate_candidates(AGENT_DIR, ALL_SIGNATURES)
    ids = [p.id for p in patches]
    assert "route-to-responses" in ids
    assert "prompt-execution-discipline" in ids
    # The two tool-schema candidates target a tool literally called book_flight; for an agent
    # without one they are skipped, never emitted as an empty or nonsensical edit.
    assert "tool-schema-book-once" not in ids
    assert "tool-schema-book-required" not in ids
    assert all(edit.file != "tools.json" for p in patches for edit in p.edits)
    assert all(p.edits for p in patches)


def test_prompt_repair_blocks_name_no_tools_and_no_domain() -> None:
    """A repair block is appended verbatim to a stranger's system prompt, so it may not name a
    victim tool or the booking domain."""
    forbidden = ("book_flight", "search_flights", "cancel_booking", "flight", "traveler")
    original = (AGENT_DIR / "system_prompt.txt").read_text().rstrip("\n")
    for patch in generate_candidates(AGENT_DIR, ALL_SIGNATURES):
        for edit in patch.edits:
            if edit.file != "system_prompt.txt":
                continue
            assert edit.new_content.startswith(original)
            appended = edit.new_content[len(original) :]
            for word in forbidden:
                assert word not in appended.lower(), (patch.id, word)


# ---------------------------------------------------------------------------
# Repair loop, verdict, patch
# ---------------------------------------------------------------------------


def test_repair_restores_every_regressed_case_of_the_foreign_agent(pipeline: Pipeline) -> None:
    assert pipeline.outcome.restored == CASE_IDS
    assert pipeline.outcome.unrestored == []
    assert [p.repair_type for p in pipeline.outcome.accepted_patches] == [
        "endpoint_routing",
        "prompt_edit",
        "prompt_edit",
        "prompt_edit",
    ]


def test_verdict_is_safe_with_patch_for_the_foreign_agent(pipeline: Pipeline) -> None:
    assert pipeline.verdict["verdict"] == SAFE_WITH_PATCH
    assert pipeline.verdict["restored"] == len(CASE_IDS)
    assert pipeline.verdict["broken_by_patch"] == 0
    assert pipeline.verdict["provider"] == "sim"


def test_patch_touches_only_the_three_patchable_files(pipeline: Pipeline) -> None:
    patch_text = make_patch(AGENT_DIR, pipeline.work_dir, rel_prefix="tests/todo_agent")
    touched = {line.split()[-1][2:] for line in patch_text.splitlines() if line.startswith("diff ")}
    assert touched == {"tests/todo_agent/agent.json", "tests/todo_agent/system_prompt.txt"}
    assert '"endpoint": "responses"' in patch_text
    # The repair loop must never have written into the agent dir under test.
    assert json.loads((AGENT_DIR / "agent.json").read_text())["endpoint"] == "chat_completions"


# ---------------------------------------------------------------------------
# The sim's one hard requirement on a foreign agent
# ---------------------------------------------------------------------------


def test_sim_refuses_an_agent_whose_cases_have_no_oracle_plan(tmp_path: Path) -> None:
    agent_dir = tmp_path / "planless_agent"
    shutil.copytree(AGENT_DIR, agent_dir, ignore=shutil.ignore_patterns("__pycache__"))
    cases_path = agent_dir / "cases" / "cases.json"
    raw = json.loads(cases_path.read_text())
    for case in raw:
        case["sim"] = {}
    cases_path.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="requires sim.oracle_plan"):
        run_suite(
            agent_dir, SimProvider(), "planless", n_reps=1, model_override="sim-5.5",
            runs_root=tmp_path / "runs", workers=1,
        )


def test_agent_dir_problems_are_reported_as_one_clear_error(tmp_path: Path) -> None:
    agent_dir = tmp_path / "broken_agent"
    shutil.copytree(AGENT_DIR, agent_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (agent_dir / "backend.py").unlink()
    with pytest.raises(ValueError, match="no backend.py"):
        run_suite(
            agent_dir, SimProvider(), "broken", n_reps=1, model_override="sim-5.5",
            runs_root=tmp_path / "runs", workers=1,
        )
