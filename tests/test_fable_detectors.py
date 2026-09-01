"""Claude Fable 5 -> 5.1 detectors and repairs (DESIGN.md, "Anthropic provider (v0.3)").

Everything here is offline: records are hand-built RepRecord objects and candidates are
generated against agent dirs written into tmp_path. No provider, no network, no run dirs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from upshift import differ
from upshift.checks import assistant_turns, evaluate_checks
from upshift.differ import (
    SIG_API_ERROR_FORCED_TOOL_CHOICE,
    SIG_API_ERROR_OTHER,
    SIG_API_ERROR_TOOLS_REASONING,
    SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS,
    SIG_OTHER_BEHAVIORAL,
    SIG_REDUCED_RETRIEVAL_CALLS,
    SIG_SERIALIZED_TOOL_CALLS,
    SIG_THINKING_BLOCK_INVALID,
    SIG_WRONG_OR_MISSING_TOOL_CALL,
    failure_signatures,
)
from upshift.repair import playbook
from upshift.repair.loop import THINKING_REFUSAL, thinking_refusal_lines
from upshift.repair.playbook import generate_candidates
from upshift.schemas import Case, CheckResult, RepRecord, ToolExecution

EXAMPLE_AGENT = Path(__file__).resolve().parents[1] / "src" / "upshift" / "example_agent"


# ---------------------------------------------------------------------------
# Fixture builders (hand-built records, per the differ's own test style)
# ---------------------------------------------------------------------------


def check(ctype: str, passed: bool, detail: str = "", **extra: Any) -> CheckResult:
    return CheckResult(check={"type": ctype, **extra}, passed=passed, detail=detail)


def rep(
    *,
    passed: bool,
    k: int = 1,
    checks: list[CheckResult] | None = None,
    api_error: dict[str, Any] | None = None,
    tool_executions: list[ToolExecution] | None = None,
    final_message: str = "",
) -> RepRecord:
    if checks is None:
        checks = [check("no_api_error", passed)]
    return RepRecord(
        case_id="case",
        rep=k,
        seed=k,
        model_requested="claude-fable-5-1",
        resolved_model="claude-fable-5-1",
        endpoint="messages",
        params={},
        api_calls=[],
        tool_executions=tool_executions or [],
        final_state={},
        final_message=final_message,
        check_results=checks,
        passed=passed,
        api_error=api_error,
        usage={"input_tokens": 10, "output_tokens": 5},
        latency_s=0.5,
    )


def error_rep(message: str, status: int = 400) -> RepRecord:
    err = {"status_code": status, "message": message, "type": "api_error"}
    return rep(
        passed=False,
        api_error=err,
        checks=[check("no_api_error", False, f"API call failed: {message}")],
    )


def calls(turns: list[list[str]]) -> list[ToolExecution]:
    """One ToolExecution per name, grouped by assistant turn: [["a","b"],["c"]] = 2 turns."""
    out = []
    for turn, names in enumerate(turns):
        for name in names:
            out.append(ToolExecution(turn, 0, name, {"q": name}, {"ok": True}))
    return out


# ---------------------------------------------------------------------------
# checks.py: turns_at_most and the retrieval flag
# ---------------------------------------------------------------------------


def evaluate(checks: list[dict[str, Any]], executions: list[ToolExecution]):
    case = Case(
        id="c", description="", initial_state={}, user_messages=["hi"], checks=checks
    )
    return evaluate_checks(
        case, api_error=None, tool_executions=executions, final_state={}, final_message="done"
    )


def test_assistant_turns_is_distinct_tool_turns_plus_the_final_answer() -> None:
    assert assistant_turns([]) == 1
    assert assistant_turns(calls([["a", "b"]])) == 2  # one tool turn + the answer
    assert assistant_turns(calls([["a"], ["b"], ["c"]])) == 4


@pytest.mark.parametrize(
    ("turns", "n", "expected"),
    [
        ([["a", "b"]], 2, True),
        ([["a"], ["b"]], 2, False),  # 3 turns > 2
        ([["a"], ["b"]], 3, True),
        ([], 1, True),
    ],
)
def test_turns_at_most_passes_exactly_at_the_bound(turns, n, expected) -> None:
    results, passed = evaluate([{"type": "turns_at_most", "n": n}], calls(turns))
    assert passed is expected
    assert str(assistant_turns(calls(turns))) in results[0].detail


def test_retrieval_flag_changes_nothing_about_tool_called_pass_fail() -> None:
    executions = calls([["search_flights"]])
    plain, plain_ok = evaluate([{"type": "tool_called", "name": "search_flights"}], executions)
    marked, marked_ok = evaluate(
        [{"type": "tool_called", "name": "search_flights", "retrieval": True}], executions
    )
    assert plain_ok is marked_ok is True
    assert plain[0].detail == marked[0].detail
    missing, missing_ok = evaluate(
        [{"type": "tool_called", "name": "absent", "retrieval": True}], executions
    )
    assert missing_ok is False and "absent" in missing[0].detail


# ---------------------------------------------------------------------------
# differ.py: the documented 400s
# ---------------------------------------------------------------------------


def test_forced_tool_choice_400_fires_and_a_different_400_does_not() -> None:
    hit = error_rep(
        'messages.tool_choice: type "tool" and "any" are not supported for this model.'
    )
    assert failure_signatures([hit]) == [SIG_API_ERROR_FORCED_TOOL_CHOICE]

    near_miss = error_rep('tool_choice: type "tool" is not supported for this model.')
    assert failure_signatures([near_miss]) == [SIG_API_ERROR_OTHER]

    not_a_400 = error_rep(differ.FORCED_TOOL_CHOICE_400, status=500)
    assert failure_signatures([not_a_400]) == [SIG_API_ERROR_OTHER]


def test_thinking_block_400_fires_alone_and_a_plain_thinking_error_does_not() -> None:
    hit = error_rep("messages.1.content.0: Invalid `signature` in `thinking` block")
    assert failure_signatures([hit]) == [SIG_THINKING_BLOCK_INVALID]

    near_miss = error_rep("`thinking` blocks are not supported for this model")
    assert failure_signatures([near_miss]) == [SIG_API_ERROR_OTHER]


def test_sampling_params_400_fires_only_when_a_sampling_param_is_named() -> None:
    for param in ("temperature", "top_p", "top_k"):
        hit = error_rep(f"`{param}` may not be set to a non-default value for this model")
        assert failure_signatures([hit]) == [SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS], param

    near_miss = error_rep("max_tokens is required for this model")
    assert failure_signatures([near_miss]) == [SIG_API_ERROR_OTHER]


def test_the_existing_openai_400_signature_is_unchanged() -> None:
    hit = error_rep(
        "Function tools with reasoning_effort are not supported with this model. "
        "Use /v1/responses."
    )
    assert failure_signatures([hit]) == [SIG_API_ERROR_TOOLS_REASONING]


# ---------------------------------------------------------------------------
# differ.py: serialized_tool_calls (needs a baseline view)
# ---------------------------------------------------------------------------


def test_serialized_fires_when_batching_collapses_to_one_call_per_turn() -> None:
    baseline = [rep(passed=True, tool_executions=calls([["a", "b", "c"]]))]
    candidate = [
        rep(
            passed=False,
            tool_executions=calls([["a"], ["b"], ["c"]]),
            checks=[check("turns_at_most", False, "4 turns", n=2)],
        )
    ]
    assert SIG_SERIALIZED_TOOL_CALLS in failure_signatures(candidate, baseline)
    # Without the baseline the comparison is undecidable, so it must not fire.
    assert SIG_SERIALIZED_TOOL_CALLS not in failure_signatures(candidate)


def test_serialized_does_not_fire_when_parallelism_is_unchanged() -> None:
    baseline = [rep(passed=True, tool_executions=calls([["a", "b"]]))]
    candidate = [
        rep(
            passed=False,
            tool_executions=calls([["a", "b"]]),
            checks=[check("response_contains", False, "missing text", text="x")],
        )
    ]
    assert SIG_SERIALIZED_TOOL_CALLS not in failure_signatures(candidate, baseline)


def test_serialized_does_not_fire_when_the_baseline_never_batched() -> None:
    """Baseline mean 1.0 call/turn: there is no parallelism to lose (threshold is >= 2)."""
    baseline = [rep(passed=True, tool_executions=calls([["a"], ["b"]]))]
    candidate = [
        rep(
            passed=False,
            tool_executions=calls([["a"]]),
            checks=[check("response_contains", False, "missing", text="x")],
        )
    ]
    assert SIG_SERIALIZED_TOOL_CALLS not in failure_signatures(candidate, baseline)


def test_a_failed_turns_check_alone_fires_serialized_only_when_turns_grew() -> None:
    baseline = [rep(passed=True, tool_executions=calls([["a"], ["b"]]))]
    more_turns = [
        rep(
            passed=False,
            tool_executions=calls([["a"], ["b"], ["c"]]),
            checks=[check("turns_at_most", False, "too many turns", n=3)],
        )
    ]
    assert SIG_SERIALIZED_TOOL_CALLS in failure_signatures(more_turns, baseline)

    same_turns = [
        rep(
            passed=False,
            tool_executions=calls([["a"], ["b"]]),
            checks=[check("turns_at_most", False, "too many turns", n=2)],
        )
    ]
    sigs = failure_signatures(same_turns, baseline)
    assert SIG_SERIALIZED_TOOL_CALLS not in sigs
    assert sigs == [SIG_OTHER_BEHAVIORAL]


# ---------------------------------------------------------------------------
# differ.py: reduced_retrieval_calls
# ---------------------------------------------------------------------------


def failed_tool_called(name: str, **extra: Any) -> RepRecord:
    return rep(
        passed=False,
        checks=[check("tool_called", False, f"{name} called 0 times", name=name, **extra)],
    )


def test_reduced_retrieval_fires_for_a_marked_tool_the_baseline_called() -> None:
    baseline = [rep(passed=True, tool_executions=calls([["fetch_docs"]]))]
    candidate = [failed_tool_called("fetch_docs", retrieval=True)]
    assert failure_signatures(candidate, baseline) == [SIG_REDUCED_RETRIEVAL_CALLS]


@pytest.mark.parametrize(
    "name", ["search_flights", "retrieve_doc", "lookup_user", "query_db", "fetch_page", "find_it"]
)
def test_reduced_retrieval_fires_on_a_retrieval_shaped_name_without_the_flag(name) -> None:
    baseline = [rep(passed=True, tool_executions=calls([[name]]))]
    assert failure_signatures([failed_tool_called(name)], baseline) == [
        SIG_REDUCED_RETRIEVAL_CALLS
    ]


def test_reduced_retrieval_does_not_fire_when_the_baseline_never_called_the_tool() -> None:
    baseline = [rep(passed=True, tool_executions=calls([["book_flight"]]))]
    candidate = [failed_tool_called("search_flights", retrieval=True)]
    assert failure_signatures(candidate, baseline) == [SIG_WRONG_OR_MISSING_TOOL_CALL]


def test_a_non_retrieval_tool_stays_a_wrong_or_missing_tool_call() -> None:
    baseline = [rep(passed=True, tool_executions=calls([["book_flight"]]))]
    assert failure_signatures([failed_tool_called("book_flight")], baseline) == [
        SIG_WRONG_OR_MISSING_TOOL_CALL
    ]


def test_every_signature_has_a_one_line_description() -> None:
    assert set(differ.SIGNATURE_DESCRIPTIONS) == set(differ.SIGNATURE_PRIORITY)
    assert all(v.strip() for v in differ.SIGNATURE_DESCRIPTIONS.values())


# ---------------------------------------------------------------------------
# playbook.py
# ---------------------------------------------------------------------------


def write_agent(
    tmp_path: Path,
    *,
    endpoint: str = "messages",
    params: dict[str, Any] | None = None,
    prompt: str = "You are a helpful assistant.",
    one_line: bool = True,
) -> Path:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "name": "a",
        "endpoint": endpoint,
        "model": "claude-fable-5-1",
        "params": params if params is not None else {},
        "system_prompt_file": "system_prompt.txt",
        "tools_file": "tools.json",
        "max_turns": 12,
    }
    text = json.dumps(raw) if one_line else json.dumps(raw, indent=2) + "\n"
    (agent_dir / "agent.json").write_text(text)
    (agent_dir / "system_prompt.txt").write_text(prompt + "\n")
    (agent_dir / "tools.json").write_text(
        json.dumps(
            [{"type": "function", "function": {"name": "search_docs", "description": "d"}}]
        )
    )
    return agent_dir


def only(patches, patch_id):
    matches = [p for p in patches if p.id == patch_id]
    assert matches, f"{patch_id} not generated (got {[p.id for p in patches]})"
    return matches[0]


def appended(agent_dir: Path, edit) -> str:
    original = (agent_dir / edit.file).read_text().rstrip("\n")
    assert edit.new_content.startswith(original)
    return edit.new_content[len(original) :]


@pytest.mark.parametrize(
    ("tool_choice", "sentence"),
    [
        ({"type": "tool", "name": "search_docs"}, playbook.FORCED_TOOL_INSTRUCTION),
        ({"type": "any"}, playbook.FORCED_ANY_INSTRUCTION),
        ("required", playbook.FORCED_ANY_INSTRUCTION),
        (
            {"type": "function", "function": {"name": "search_docs"}},
            playbook.FORCED_TOOL_INSTRUCTION,
        ),
    ],
)
def test_forced_tool_choice_candidate_has_exactly_two_edits(tmp_path, tool_choice, sentence):
    agent_dir = write_agent(tmp_path, params={"tool_choice": tool_choice})
    patch = only(
        generate_candidates(agent_dir, ["api_error_forced_tool_choice"]),
        "remove-forced-tool-choice",
    )
    assert [e.file for e in patch.edits] == ["agent.json", "system_prompt.txt"]

    raw = json.loads(patch.edits[0].new_content)
    assert raw["params"] == {}
    before = json.loads((agent_dir / "agent.json").read_text())
    before["params"].pop("tool_choice")
    assert raw == before  # nothing but tool_choice changed

    expected = sentence.format(tool="search_docs") if "{tool}" in sentence else sentence
    assert appended(agent_dir, patch.edits[1]) == "\n\n" + expected + "\n"


def test_forced_tool_choice_edit_touches_only_the_tool_choice_lines(tmp_path):
    """A pretty-printed agent.json loses the tool_choice lines and nothing else."""
    agent_dir = write_agent(
        tmp_path,
        params={"tool_choice": {"type": "any"}, "reasoning_effort": "high"},
        one_line=False,
    )
    patch = only(
        generate_candidates(agent_dir, ["api_error_forced_tool_choice"]),
        "remove-forced-tool-choice",
    )
    old = (agent_dir / "agent.json").read_text().splitlines()
    new = patch.edits[0].new_content.splitlines()
    removed = [line for line in old if line not in new]
    assert removed == ['    "tool_choice": {', '      "type": "any"', '    },']
    assert [line for line in new if line not in old] == []


def test_removing_a_trailing_tool_choice_only_drops_the_previous_comma(tmp_path):
    agent_dir = write_agent(
        tmp_path,
        params={"reasoning_effort": "high", "tool_choice": {"type": "any"}},
        one_line=False,
    )
    patch = only(
        generate_candidates(agent_dir, ["api_error_forced_tool_choice"]),
        "remove-forced-tool-choice",
    )
    new = patch.edits[0].new_content
    # The only line the removal touches besides tool_choice's own is the one before it,
    # which loses its trailing comma because tool_choice was the last entry.
    assert '"reasoning_effort": "high"\n' in new
    assert "tool_choice" not in new
    assert json.loads(new)["params"] == {"reasoning_effort": "high"}


def test_no_forced_tool_choice_candidate_when_the_param_is_auto_or_absent(tmp_path):
    for params in ({}, {"tool_choice": "auto"}, {"tool_choice": {"type": "auto"}}):
        agent_dir = write_agent(tmp_path, params=params)
        ids = [p.id for p in generate_candidates(agent_dir, ["api_error_forced_tool_choice"])]
        assert "remove-forced-tool-choice" not in ids, params


def test_the_two_edit_patch_applies_to_a_copy_of_the_example_agent(tmp_path):
    agent_dir = tmp_path / "example"
    shutil.copytree(EXAMPLE_AGENT, agent_dir)
    raw = json.loads((agent_dir / "agent.json").read_text())
    raw["params"] = {"tool_choice": {"type": "tool", "name": "book_flight"}}
    (agent_dir / "agent.json").write_text(json.dumps(raw))
    prompt_before = (agent_dir / "system_prompt.txt").read_text()

    patch = only(
        generate_candidates(agent_dir, ["api_error_forced_tool_choice"]),
        "remove-forced-tool-choice",
    )
    for edit in patch.edits:
        (agent_dir / edit.file).write_text(edit.new_content)

    patched = json.loads((agent_dir / "agent.json").read_text())
    assert patched["params"] == {}
    assert patched["model"] == raw["model"] and patched["max_turns"] == raw["max_turns"]
    prompt_after = (agent_dir / "system_prompt.txt").read_text()
    assert prompt_after.startswith(prompt_before.rstrip("\n"))
    assert prompt_after.endswith(
        "Use the `book_flight` tool to answer; call it rather than replying in text.\n"
    )


def test_drop_sampling_params_removes_only_those_params(tmp_path):
    agent_dir = write_agent(
        tmp_path,
        params={"temperature": 0.2, "top_p": 0.9, "top_k": 5, "reasoning_effort": "high"},
    )
    patch = only(
        generate_candidates(agent_dir, ["api_error_unsupported_sampling_params"]),
        "drop-sampling-params",
    )
    assert [e.file for e in patch.edits] == ["agent.json"]
    assert json.loads(patch.edits[0].new_content)["params"] == {"reasoning_effort": "high"}


def test_drop_sampling_params_is_skipped_when_no_such_param_exists(tmp_path):
    agent_dir = write_agent(tmp_path, params={"reasoning_effort": "high"})
    ids = [
        p.id for p in generate_candidates(agent_dir, ["api_error_unsupported_sampling_params"])
    ]
    assert "drop-sampling-params" not in ids


def test_batching_sentence_is_appended_verbatim_and_only_once(tmp_path):
    agent_dir = write_agent(tmp_path)
    patch = only(
        generate_candidates(agent_dir, ["serialized_tool_calls"]), "prompt-batch-tool-calls"
    )
    assert appended(agent_dir, patch.edits[0]) == (
        "\n\nFirst privately list what you need next; then request every item that doesn't "
        "depend on another's result in this one response.\n"
    )
    already = write_agent(tmp_path / "b", prompt="x" + playbook.BATCH_BLOCK)
    ids = [p.id for p in generate_candidates(already, ["serialized_tool_calls"])]
    assert "prompt-batch-tool-calls" not in ids


def test_verification_nudge_is_appended_verbatim_after_the_effort_candidate(tmp_path):
    agent_dir = write_agent(tmp_path)
    patches = generate_candidates(agent_dir, ["reduced_retrieval_calls"])
    assert [p.id for p in patches] == ["raise-effort-one-rung", "prompt-verification-nudge"]
    nudge = only(patches, "prompt-verification-nudge")
    assert appended(agent_dir, nudge.edits[0]) == playbook.VERIFICATION_NUDGE_BLOCK + "\n"
    assert playbook.VERIFICATION_NUDGE_BLOCK.startswith(
        "\n\nWhen a query centers on a name you do not confidently recognize, "
    )

    already = write_agent(tmp_path / "b", prompt="x" + playbook.VERIFICATION_NUDGE_BLOCK)
    ids = [p.id for p in generate_candidates(already, ["reduced_retrieval_calls"])]
    assert "prompt-verification-nudge" not in ids


@pytest.mark.parametrize(
    ("endpoint", "current", "expected"),
    [
        ("messages", None, "xhigh"),  # unset means high on the Fables
        ("messages", "low", "medium"),
        ("messages", "xhigh", "max"),
        ("messages", "max", None),  # already at the top rung
        ("chat_completions", None, "high"),  # unset means medium on the OpenAI endpoints
        ("chat_completions", "none", "low"),
        ("responses", "medium", "high"),
        ("responses", "high", None),
        ("messages", "nonsense", None),  # off-ladder value: no guess
    ],
)
def test_effort_is_raised_one_rung_on_the_endpoint_ladder(tmp_path, endpoint, current, expected):
    params = {} if current is None else {"reasoning_effort": current}
    agent_dir = write_agent(tmp_path, endpoint=endpoint, params=params)
    patches = generate_candidates(agent_dir, ["reduced_retrieval_calls"])
    rungs = [p for p in patches if p.id == "raise-effort-one-rung"]
    if expected is None:
        assert rungs == []
    else:
        raised = json.loads(rungs[0].edits[0].new_content)["params"]["reasoning_effort"]
        assert raised == expected


def test_effort_is_never_lowered(tmp_path):
    for endpoint, ladder in playbook.EFFORT_LADDERS.items():
        for current in ladder:
            agent_dir = write_agent(
                tmp_path / f"{endpoint}-{current}",
                endpoint=endpoint,
                params={"reasoning_effort": current},
            )
            for patch in generate_candidates(agent_dir, ["reduced_retrieval_calls"]):
                if patch.id != "raise-effort-one-rung":
                    continue
                new = json.loads(patch.edits[0].new_content)["params"]["reasoning_effort"]
                assert ladder.index(new) > ladder.index(current)


def test_existing_candidates_are_unchanged_for_the_old_signatures(tmp_path):
    """The OpenAI-era candidates keep their ids and their exact blocks."""
    agent_dir = write_agent(tmp_path, endpoint="chat_completions")
    ids = [
        p.id
        for p in generate_candidates(
            agent_dir,
            [
                "api_error_tools_reasoning",
                "duplicate_tool_calls",
                "acting_past_goal",
                "skipped_tool_hallucination",
                "wrong_or_missing_tool_call",
            ],
        )
    ]
    for expected in (
        "route-to-responses",
        "reasoning-effort-none",
        "prompt-execution-discipline",
        "prompt-stop-after-goal",
        "prompt-no-fabrication",
        "prompt-execute-dont-ask",
        "prompt-ground-in-results",
        "reasoning-effort-high",
    ):
        assert expected in ids


# ---------------------------------------------------------------------------
# loop.py: the refusal path
# ---------------------------------------------------------------------------


def test_the_loop_refuses_thinking_block_invalid_once_with_the_design_pointer() -> None:
    per_case = {"a": [SIG_THINKING_BLOCK_INVALID], "b": [SIG_WRONG_OR_MISSING_TOOL_CALL]}
    already: set[str] = set()
    lines = thinking_refusal_lines(per_case, {"a", "b"}, already)
    assert lines == [f"REFUSAL a: {THINKING_REFUSAL}"]
    assert already == {"a"}
    # Logged once per repair run, not once per iteration.
    assert thinking_refusal_lines(per_case, {"a", "b"}, already) == []

    for pointer in ("drop_block", "thinking-binding-controls-2026-08-01", "DESIGN.md"):
        assert pointer in THINKING_REFUSAL


def test_a_restored_case_is_not_refused() -> None:
    per_case = {"a": [SIG_THINKING_BLOCK_INVALID]}
    assert thinking_refusal_lines(per_case, set(), set()) == []


def test_no_candidate_is_generated_for_thinking_block_invalid(tmp_path) -> None:
    agent_dir = write_agent(tmp_path, params={"tool_choice": {"type": "any"}})
    assert generate_candidates(agent_dir, [SIG_THINKING_BLOCK_INVALID]) == []
