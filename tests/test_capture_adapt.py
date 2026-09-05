"""`upshift adapt --from-capture`, and the whole pipeline behind it.

The fixture in `tests/capture_fixtures/cookbook_sms/` is a real capture: `agents/cookbook-sms`
driven through `upshift capture --sim` by the actual `anthropic` SDK
(`make_cookbook_sms_capture.py` regenerates it). So these tests assert against bytes that
really crossed a socket, not against a hand-written stub of what a request looks like.

Offline and deterministic — no key, no network, no cost.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift.agent_loop import build_request, convert_tools_messages
from upshift.capture.adapt import NO_RECORD_ERROR, adapt_from_capture
from upshift.capture.record import CaptureStore, load_capture
from upshift.cli import validate_agent_dir
from upshift.differ import diff_runs
from upshift.providers.sim import SimProvider
from upshift.recorder import run_dir
from upshift.repair.loop import repair
from upshift.runner import load_backend_factory, run_suite
from upshift.schemas import LABEL_REGRESSED, AgentConfig, Case
from upshift.verdict import SAFE_WITH_PATCH, decide

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "capture_fixtures" / "cookbook_sms"
COOKBOOK_SMS = ROOT / "agents" / "cookbook-sms"
N_REPS = 5


@pytest.fixture(scope="module")
def adapted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("capture-adapt") / "agent"
    adapt_from_capture(FIXTURE, out)
    return out


# ---------------------------------------------------------------------------
# The five files
# ---------------------------------------------------------------------------


def test_the_generated_directory_satisfies_the_adapter_contract(adapted: Path) -> None:
    validate_agent_dir(adapted)
    written = sorted(
        p.relative_to(adapted).as_posix()
        for p in adapted.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert written == [
        "ADAPT_EDITS.md",
        "ATTRIBUTION.md",
        "agent.json",
        "backend.py",
        "cases/cases.json",
        "recorded_tools.json",
        "system_prompt.txt",
        "tools.json",
    ]


def test_agent_json_carries_the_parameters_that_were_actually_sent(adapted: Path) -> None:
    config = json.loads((adapted / "agent.json").read_text())
    assert config["endpoint"] == "messages"
    assert config["provider"] == "anthropic"
    assert config["model"] == "sim-fable-5"
    assert config["params"] == {"max_tokens": 4096, "tool_choice": {"type": "any"}}
    assert config["capture"]["framework"] == "anthropic-sdk-python"
    assert config["capture"]["requests"] == 6


def test_the_system_prompt_is_byte_identical_to_the_one_that_crossed_the_wire(
    adapted: Path,
) -> None:
    """The strongest fidelity claim capture mode makes, checked against the source agent."""
    assert (adapted / "system_prompt.txt").read_text() == (
        COOKBOOK_SMS / "system_prompt.txt"
    ).read_text()


def test_tools_round_trip_back_to_the_recorded_anthropic_schemas(adapted: Path) -> None:
    _, conversations = load_capture(FIXTURE)
    sent = (conversations[0]["turns"][0]["request"]["body"])["tools"]
    generated = json.loads((adapted / "tools.json").read_text())
    round_tripped = convert_tools_messages(generated)
    assert {t["name"]: t for t in round_tripped} == {t["name"]: t for t in sent}


def test_cases_use_only_checks_a_recording_can_justify(adapted: Path) -> None:
    cases = Case.load_all(adapted / "cases" / "cases.json")
    assert len(cases) == 3
    kinds = {check["type"] for case in cases for check in case.checks}
    assert kinds == {"no_api_error", "tool_called", "turns_at_most"}
    # nothing asserted about the words the model produced
    assert "response_contains" not in (adapted / "cases" / "cases.json").read_text()


def test_the_user_turns_are_verbatim(adapted: Path) -> None:
    cases = {c.id: c for c in Case.load_all(adapted / "cases" / "cases.json")}
    originals = {c.user_messages[0] for c in Case.load_all(COOKBOOK_SMS / "cases" / "cases.json")}
    for case in cases.values():
        assert case.user_messages[0] in originals


def test_turns_at_most_is_the_recorded_turn_count_plus_one(adapted: Path) -> None:
    _, conversations = load_capture(FIXTURE)
    recorded = {c["id"]: len(c["turns"]) for c in conversations}
    cases = Case.load_all(adapted / "cases" / "cases.json")
    limits = [next(c["n"] for c in case.checks if c["type"] == "turns_at_most") for case in cases]
    assert limits == [count + 1 for count in recorded.values()]


def test_the_oracle_plan_replays_the_recorded_tool_calls(adapted: Path) -> None:
    cases = {c.id: c for c in Case.load_all(adapted / "cases" / "cases.json")}
    greeting = next(c for c in cases.values() if c.user_messages[0].startswith("Hey there"))
    assert greeting.sim["oracle_plan"][0]["tool_calls"][0]["name"] == "send_text_to_user"


def test_the_ledgers_name_the_capture_and_the_deviations(adapted: Path) -> None:
    attribution = (adapted / "ATTRIBUTION.md").read_text()
    assert "capture_fixtures/cookbook_sms" in attribution
    assert "anthropic-sdk-python" in attribution
    edits = (adapted / "ADAPT_EDITS.md").read_text()
    assert "Thinking blocks are not replayed" in edits
    assert NO_RECORD_ERROR in edits


# ---------------------------------------------------------------------------
# The replay backend
# ---------------------------------------------------------------------------


def test_recorded_arguments_replay_the_recorded_result(adapted: Path) -> None:
    backend = load_backend_factory(adapted)({})
    result = backend.execute("get_customer_info", {"username": "jenny76"})
    assert result["email"] == "jenny76@email.com"
    assert backend.state()["unrecorded_calls"] == []


def test_unknown_arguments_fail_honestly_instead_of_passing_quietly(adapted: Path) -> None:
    backend = load_backend_factory(adapted)({})
    result = backend.execute("get_customer_info", {"username": "someone-else"})
    assert result["error"] == NO_RECORD_ERROR
    assert result["tool"] == "get_customer_info"
    assert result["recorded_argument_sets"] == ['{"username":"jenny76"}']
    assert backend.state()["unrecorded_calls"] == [
        {"name": "get_customer_info", "arguments": {"username": "someone-else"}}
    ]


def test_an_unrecorded_tool_name_fails_the_same_way(adapted: Path) -> None:
    backend = load_backend_factory(adapted)({})
    result = backend.execute("delete_everything", {})
    assert result["error"] == NO_RECORD_ERROR
    assert "never recorded a call" in result["detail"]


def test_the_replay_backend_never_raises_on_unserialisable_arguments(adapted: Path) -> None:
    """ADAPTER.md: execute() never raises, whatever the model produced."""
    backend = load_backend_factory(adapted)({})
    result = backend.execute("get_customer_info", {"weird": object()})
    assert result["error"] == NO_RECORD_ERROR


def test_a_replayed_result_cannot_be_mutated_by_the_episode(adapted: Path) -> None:
    backend = load_backend_factory(adapted)({})
    first = backend.execute("get_customer_info", {"username": "jenny76"})
    first["email"] = "tampered"
    assert backend.execute("get_customer_info", {"username": "jenny76"})["email"] != "tampered"


# ---------------------------------------------------------------------------
# End to end: capture -> adapt -> upgrade
# ---------------------------------------------------------------------------


class Upgrade:
    """The pipeline `upshift upgrade` runs, over the capture-derived agent directory."""

    def __init__(self, agent_dir: Path, root: Path) -> None:
        self.runs = root / "runs"
        provider = SimProvider()
        run_suite(agent_dir, provider, "baseline", n_reps=N_REPS,
                  model_override="sim-fable-5", runs_root=self.runs, workers=1)
        run_suite(agent_dir, provider, "candidate", n_reps=N_REPS,
                  model_override="sim-fable-5-1", runs_root=self.runs, workers=1)
        self.diff = diff_runs(run_dir(self.runs, "baseline"), run_dir(self.runs, "candidate"))
        self.outcome = repair(
            original_agent_dir=agent_dir,
            work_dir=root / "patched_agent",
            provider=provider,
            candidate_model="sim-fable-5-1",
            baseline_diff=self.diff,
            n_reps=N_REPS,
            runs_root=self.runs,
            run_prefix="capture",
            budget=6,
            workers=1,
        )
        self.verdict = decide(self.diff, self.outcome, patch_path=str(root / "upgrade.patch"))


@pytest.fixture(scope="module")
def upgrade(adapted: Path, tmp_path_factory: pytest.TempPathFactory) -> Upgrade:
    return Upgrade(adapted, tmp_path_factory.mktemp("capture-upgrade"))


def test_the_capture_derived_agent_passes_on_the_baseline_model(upgrade: Upgrade) -> None:
    summary = json.loads((run_dir(upgrade.runs, "baseline") / "summary.json").read_text())
    assert all(entry["passes"] == N_REPS for entry in summary.values())
    assert len(summary) == 3


def test_the_captured_forced_tool_choice_regresses_every_case(upgrade: Upgrade) -> None:
    regressed = [c for c in upgrade.diff.cases if c.label == LABEL_REGRESSED]
    assert len(regressed) == 3
    assert {sig for c in regressed for sig in c.failure_signatures} == {
        "api_error_forced_tool_choice"
    }


def test_the_repair_restores_it_and_the_verdict_is_safe_with_patch(upgrade: Upgrade) -> None:
    assert [p.id for p in upgrade.outcome.accepted_patches] == ["remove-forced-tool-choice"]
    assert upgrade.verdict["verdict"] == SAFE_WITH_PATCH
    assert upgrade.verdict["restored"] == 3
    assert upgrade.verdict["broken_by_patch"] == 0


# ---------------------------------------------------------------------------
# The volatile per-request suffix
# ---------------------------------------------------------------------------


FACTS = "<facts>\ncurrent_time: 2026-09-05T12:00:0{n}Z\n</facts>"


def _volatile_capture(out_dir: Path) -> Path:
    """A capture in the shape rescue-ops A-015 describes: the trailing user block is rebuilt
    on every request."""
    store = CaptureStore(out_dir, listen="127.0.0.1:0", upstream="x", mode="forward")
    tail: list[dict] = []
    for turn in (1, 2):
        messages = [
            {"role": "user", "content": "what time is it?"},
            {"role": "user", "content": FACTS.format(n=turn)},
            *tail,
        ]
        answered = (
            {"type": "tool_use", "id": "toolu_1", "name": "clock", "input": {}}
            if turn == 1
            else {"type": "text", "text": "It is noon."}
        )
        store.add(
            headers={"user-agent": "framework/1.0"},
            body={"model": "claude-fable-5", "max_tokens": 512, "messages": messages,
                  "system": "You tell the time.",
                  "tools": [{"name": "clock", "description": "clock",
                             "input_schema": {"type": "object", "properties": {}}}]},
            raw_body_bytes=400, path="/v1/messages", status=200,
            response_body={"id": f"msg_{turn}", "type": "message", "role": "assistant",
                           "model": "claude-fable-5", "content": [answered],
                           "stop_reason": "tool_use" if turn == 1 else "end_turn"},
            events=None, streamed=False, latency_s=0.1,
        )
        tail = [
            {"role": "assistant", "content": [answered]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                          "content": '{"now": "12:00"}'}]},
        ]
    store.close()
    return out_dir


def test_a_volatile_suffix_is_detected_and_written_to_agent_json(tmp_path: Path) -> None:
    capture = _volatile_capture(tmp_path / "cap")
    result = adapt_from_capture(capture, tmp_path / "agent")
    config = json.loads((tmp_path / "agent" / "agent.json").read_text())
    assert config["volatile_suffix"] == FACTS.format(n=2)
    assert result.volatile_suffix == FACTS.format(n=2)
    assert any("volatile suffix" in note for note in result.notes)
    assert FACTS.format(n=2) in (tmp_path / "agent" / "ADAPT_EDITS.md").read_text()


def test_the_volatile_block_is_kept_out_of_the_case_input(tmp_path: Path) -> None:
    adapt_from_capture(_volatile_capture(tmp_path / "cap"), tmp_path / "agent")
    cases = Case.load_all(tmp_path / "agent" / "cases" / "cases.json")
    assert cases[0].user_messages == ["what time is it?"]


def test_the_loop_appends_the_volatile_suffix_to_every_request(tmp_path: Path) -> None:
    adapt_from_capture(_volatile_capture(tmp_path / "cap"), tmp_path / "agent")
    config = AgentConfig.load(tmp_path / "agent")
    assert config.volatile_suffix == FACTS.format(n=2)

    items = [{"role": "user", "content": "what time is it?"}]
    request = build_request("messages", config.model, config.params, config.tools, items,
                            system=config.system_prompt,
                            volatile_suffix=config.volatile_suffix)
    assert request["messages"][-1]["content"] == [
        {"type": "text", "text": "what time is it?"},
        {"type": "text", "text": FACTS.format(n=2)},
    ]
    assert items == [{"role": "user", "content": "what time is it?"}]  # caller's list untouched


def test_the_suffix_lands_after_tool_results_not_before_them(tmp_path: Path) -> None:
    """Anthropic requires tool_result blocks first in their user turn."""
    items = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                           "content": "{}"}]}]
    request = build_request("messages", "m", {}, [], items, system="s", volatile_suffix="facts")
    assert [b["type"] for b in request["messages"][-1]["content"]] == ["tool_result", "text"]


def test_no_suffix_means_no_change_at_all() -> None:
    items = [{"role": "user", "content": "hi"}]
    assert build_request("messages", "m", {}, [], items, system="s")["messages"] == items


# ---------------------------------------------------------------------------
# What a capture must not carry into an agent directory
# ---------------------------------------------------------------------------


def test_thinking_blocks_in_the_captured_history_never_reach_a_case(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "cap", listen="127.0.0.1:0", upstream="x", mode="forward")
    store.add(
        headers={},
        body={"model": "claude-fable-5", "max_tokens": 8, "system": "s",
              "messages": [
                  {"role": "user", "content": [
                      {"type": "text", "text": "hello"},
                  ]},
                  {"role": "assistant", "content": [
                      {"type": "thinking", "thinking": "secret reasoning", "signature": "sig"},
                      {"type": "text", "text": "hi"},
                  ]},
                  {"role": "user", "content": "again"},
              ]},
        raw_body_bytes=200, path="/v1/messages", status=200,
        response_body={"id": "m", "type": "message", "role": "assistant",
                       "model": "claude-fable-5",
                       "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
        events=None, streamed=False, latency_s=0.1,
    )
    store.close()
    adapt_from_capture(tmp_path / "cap", tmp_path / "agent")
    cases_text = (tmp_path / "agent" / "cases" / "cases.json").read_text()
    assert "secret reasoning" not in cases_text
    assert "thinking" not in cases_text
    cases = Case.load_all(tmp_path / "agent" / "cases" / "cases.json")
    assert cases[0].user_messages == ["hello", "again"]


def test_a_capture_with_nothing_to_adapt_is_a_clean_error(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "cap", listen="127.0.0.1:0", upstream="x", mode="forward")
    store.add(headers={}, body={"model": "m", "max_tokens": 4,
                                "messages": [{"role": "user", "content": [
                                    {"type": "image", "source": {"data": "..."}}]}]},
              raw_body_bytes=50, path="/v1/messages", status=200,
              response_body={"content": [{"type": "text", "text": "ok"}]},
              events=None, streamed=False, latency_s=0.1)
    store.close()
    with pytest.raises(ValueError, match="no captured conversation produced a case"):
        adapt_from_capture(tmp_path / "cap", tmp_path / "agent")


def test_a_recorded_failure_is_reported_rather_than_hidden(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "cap", listen="127.0.0.1:0", upstream="x", mode="forward")
    store.add(headers={}, body={"model": "m", "max_tokens": 4, "system": "s",
                                "messages": [{"role": "user", "content": "hi"}]},
              raw_body_bytes=50, path="/v1/messages", status=400,
              response_body={"type": "error", "error": {"message": "boom"}},
              events=None, streamed=False, latency_s=0.1)
    store.close()
    result = adapt_from_capture(tmp_path / "cap", tmp_path / "agent")
    assert any("recorded 1 failed response" in note for note in result.notes)
