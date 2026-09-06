"""`upshift adapt --from-capture`, and the whole pipeline behind it.

The fixture in `tests/capture_fixtures/cookbook_sms/` is a real capture: `agents/cookbook-sms`
driven through `upshift capture --sim` by the actual `anthropic` SDK
(`make_cookbook_sms_capture.py` regenerates it). So these tests assert against bytes that
really crossed a socket, not against a hand-written stub of what a request looks like.

Offline and deterministic — no key, no network, no cost.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from upshift.agent_loop import build_request, convert_tools_messages, run_episode
from upshift.capture import mapping as capture_mapping
from upshift.capture.adapt import NO_RECORD_ERROR, adapt_from_capture
from upshift.capture.record import CaptureStore, load_capture
from upshift.cli import validate_agent_dir
from upshift.differ import diff_runs
from upshift.providers.sim import SimProvider
from upshift.recorder import run_dir
from upshift.repair.loop import repair
from upshift.repair.playbook import generate_candidates
from upshift.report import diff_to_markdown
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
        self.framework = capture_mapping.framework_of(agent_dir)
        self.verdict = decide(self.diff, self.outcome, patch_path=str(root / "upgrade.patch"),
                              framework=self.framework)


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


def test_the_report_says_where_the_repair_lives_in_the_framework(upgrade: Upgrade) -> None:
    """The whole point of the capture path: the patch edits an adapter directory, so the
    report has to name the setting in the code the user actually maintains."""
    assert upgrade.framework == "anthropic-sdk-python"
    report = diff_to_markdown(upgrade.diff, verdict=upgrade.verdict)
    assert "## Framework mapping" in report
    assert "`client.messages.create()`" in report
    assert "anthropic/resources/messages/messages.py:137 @1.4.0" in report


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


# ---------------------------------------------------------------------------
# Terminal tools: the framework stopped, so the episode stops
# ---------------------------------------------------------------------------


def _structured_output_capture(out_dir: Path) -> Path:
    """The pydantic-ai shape: one ordinary tool, then a `final_result` call the framework
    never answers because that call IS the output."""
    store = CaptureStore(out_dir, listen="127.0.0.1:0", upstream="x", mode="forward")
    tools = [
        {"name": "temperature_c", "description": "temp",
         "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}},
        {"name": "final_result", "description": "The final response which ends this "
         "conversation", "input_schema": {"type": "object",
                                          "properties": {"celsius": {"type": "integer"}}}},
    ]
    calls = [
        {"type": "tool_use", "id": "toolu_1", "name": "temperature_c", "input": {"city": "Oslo"}},
        {"type": "tool_use", "id": "toolu_2", "name": "final_result", "input": {"celsius": 3}},
    ]
    messages: list[dict] = [{"role": "user", "content": "weather in Oslo?"}]
    for turn, call in enumerate(calls, start=1):
        store.add(
            headers={"user-agent": "pydantic-ai/2.40.0"},
            body={"model": "claude-fable-5", "max_tokens": 4096, "messages": list(messages),
                  "system": "Report the weather.", "tools": tools,
                  "tool_choice": {"type": "any"}},
            raw_body_bytes=400, path="/v1/messages?beta=true", status=200,
            response_body={"id": f"msg_{turn}", "type": "message", "role": "assistant",
                           "model": "claude-fable-5", "content": [call],
                           "stop_reason": "tool_use"},
            events=None, streamed=False, latency_s=0.1,
        )
        messages += [
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                          "content": "3"}]},
        ]
    store.close()
    return out_dir


def test_a_tool_the_capture_never_answered_is_marked_terminal(tmp_path: Path) -> None:
    result = adapt_from_capture(_structured_output_capture(tmp_path / "cap"), tmp_path / "agent")
    assert result.terminal_tools == ["final_result"]
    config = json.loads((tmp_path / "agent" / "agent.json").read_text())
    assert config["terminal_tools"] == ["final_result"]
    assert any("never answered" in note for note in result.notes)


def test_an_answered_tool_is_never_terminal(tmp_path: Path) -> None:
    """temperature_c got a tool_result, so it is an ordinary tool no matter how it looks."""
    result = adapt_from_capture(_structured_output_capture(tmp_path / "cap"), tmp_path / "agent")
    assert "temperature_c" not in result.terminal_tools


def test_the_cookbook_capture_has_no_terminal_tools(adapted: Path) -> None:
    """Every tool in that capture was answered; nothing may be invented."""
    assert "terminal_tools" not in json.loads((adapted / "agent.json").read_text())


class _CallOnce:
    """A provider that calls a terminal tool, then would keep talking if it were allowed to."""

    def __init__(self) -> None:
        self.calls = 0

    def call(self, endpoint, request, seed_key, sim_context):
        self.calls += 1
        block = {"type": "tool_use", "id": f"t{self.calls}", "name": "final_result",
                 "input": {"answer": self.calls}}
        return {"id": "msg", "type": "message", "role": "assistant", "model": "m",
                "content": [block], "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1}}


class _NeverCalled:
    def execute(self, name, arguments):  # pragma: no cover - the point is that it is not
        raise AssertionError(f"the replay backend was asked for {name!r}")

    def state(self):
        return {}


def test_the_episode_ends_on_a_terminal_tool_and_the_backend_is_never_asked(
    tmp_path: Path,
) -> None:
    adapt_from_capture(_structured_output_capture(tmp_path / "cap"), tmp_path / "agent")
    config = AgentConfig.load(tmp_path / "agent")
    assert config.terminal_tools == ["final_result"]
    provider = _CallOnce()
    episode = run_episode(
        config,
        Case(id="c", description="", initial_state={}, user_messages=["weather in Oslo?"],
             checks=[]),
        provider,
        _NeverCalled(),
        rep=1,
        seed=1,
    )
    assert provider.calls == 1  # the loop stopped instead of feeding a result back
    assert [e.name for e in episode.tool_executions] == ["final_result"]
    assert episode.final_message == '{"answer": 1}'  # the framework's own output
    assert episode.api_error is None


def test_without_terminal_tools_the_same_episode_keeps_going(tmp_path: Path) -> None:
    """The control: this is the extra assistant turn the real framework never took."""
    adapt_from_capture(_structured_output_capture(tmp_path / "cap"), tmp_path / "agent")
    config = AgentConfig.load(tmp_path / "agent")
    provider = _CallOnce()
    episode = run_episode(
        replace(config, terminal_tools=[]),
        Case(id="c", description="", initial_state={}, user_messages=["weather in Oslo?"],
             checks=[]),
        provider,
        load_backend_factory(Path(config.agent_dir))({}),
        rep=1,
        seed=1,
    )
    assert provider.calls == config.max_turns
    assert len(episode.tool_executions) == config.max_turns


# ---------------------------------------------------------------------------
# A sampling param seen on the wire: capture -> agent.json -> wire -> repair
# ---------------------------------------------------------------------------


_ABSENT = object()


def _sampling_capture(out_dir: Path) -> Path:
    """A framework that sets `temperature` on every request — the shape that makes the
    Anthropic sampling 400 reachable, recorded the way the recorder really writes it."""
    store = CaptureStore(out_dir, listen="127.0.0.1:0", upstream="x", mode="forward")
    store.add(
        headers={"user-agent": "framework/1.0"},
        body={"model": "claude-fable-5", "max_tokens": 512, "temperature": 0.2,
              "messages": [{"role": "user", "content": "say hi"}],
              "system": "Be brief.",
              "tools": [{"name": "clock", "description": "clock",
                         "input_schema": {"type": "object", "properties": {}}}]},
        raw_body_bytes=400, path="/v1/messages", status=200,
        response_body={"id": "msg_1", "type": "message", "role": "assistant",
                       "model": "claude-fable-5",
                       "content": [{"type": "text", "text": "Hi."}],
                       "stop_reason": "end_turn"},
        events=None, streamed=False, latency_s=0.1,
    )
    store.close()
    return out_dir


@pytest.fixture()
def sampling_agent(tmp_path: Path) -> Path:
    adapt_from_capture(_sampling_capture(tmp_path / "cap"), tmp_path / "agent")
    return tmp_path / "agent"


def test_a_captured_sampling_param_is_declared_under_params(sampling_agent: Path) -> None:
    """What the agent DECLARES is `params.temperature`, whatever the installed SDK will take
    as a keyword: how it travels is the loop's business (ADAPTER.md, "Params")."""
    params = json.loads((sampling_agent / "agent.json").read_text())["params"]
    assert params["temperature"] == 0.2
    assert "extra_body" not in params


@pytest.mark.parametrize("sdk_takes_it", [True, False])
def test_it_reaches_the_wire_exactly_once(
    sampling_agent: Path, monkeypatch: pytest.MonkeyPatch, sdk_takes_it: bool
) -> None:
    """Whichever way `map_params` routes it, the request carries ONE temperature — a value in
    both the top level and `extra_body` would be sent twice and could disagree."""
    monkeypatch.setattr(
        "upshift.agent_loop.messages_create_accepts", lambda name: sdk_takes_it
    )
    config = AgentConfig.load(sampling_agent)
    request = build_request(
        "messages", config.model, config.params, config.tools,
        [{"role": "user", "content": "say hi"}], system=config.system_prompt,
    )
    places = [request.get("temperature", _ABSENT),
              (request.get("extra_body") or {}).get("temperature", _ABSENT)]
    assert [p for p in places if p is not _ABSENT] == [0.2]
    assert ("temperature" in request) is sdk_takes_it


def test_the_capture_derived_param_is_dropped_by_the_repair(sampling_agent: Path) -> None:
    """The declaration is what the repair edits, so the candidate fires on a capture-derived
    agent exactly as it does on a hand-written one — and the request is then clean."""
    candidates = generate_candidates(sampling_agent, ["api_error_unsupported_sampling_params"])
    assert [c.id for c in candidates] == ["drop-sampling-params"]
    edited = [e for e in candidates[0].edits if e.file == "agent.json"]
    params = json.loads(edited[0].new_content)["params"]
    assert "temperature" not in params
    assert "extra_body" not in params
    request = build_request("messages", "claude-fable-5-1", params, [], [], system="Be brief.")
    assert "temperature" not in request
    assert "temperature" not in (request.get("extra_body") or {})


# ---------------------------------------------------------------------------
# Per-turn params: the shape a framework actually sends, turn by turn
# ---------------------------------------------------------------------------


def _text_response(text: str, model: str = "claude-fable-5") -> dict:
    return {
        "id": "msg_x", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }


def _tool_response(name: str, call_id: str, model: str = "claude-fable-5") -> dict:
    return {
        "id": "msg_x", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {"city": "Oslo"}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }


WEATHER_TOOL = {
    "name": "get_weather", "description": "Weather for a city.",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
}


def _two_turn_capture(
    out_dir: Path, *, shapes: list[list[dict]], question: str = "weather in Oslo?"
) -> Path:
    """One conversation per entry in `shapes`; one recorded request per tool_choice in it.

    Turn 1 calls the tool, turn 2 answers in text — the force-then-`auto` shape pydantic-ai
    and litellm both produce, and the one an episode-level `tool_choice` cannot express.
    """
    store = CaptureStore(out_dir, listen="127.0.0.1:0", upstream="x", mode="forward")
    for conversation, choices in enumerate(shapes, start=1):
        messages: list[dict] = [{"role": "user", "content": f"{question} ({conversation})"}]
        for turn, choice in enumerate(choices):
            last = turn == len(choices) - 1
            call_id = f"toolu_{conversation}_{turn}"
            body = {
                "model": "claude-fable-5", "max_tokens": 512,
                "system": "You are a weather assistant.",
                "messages": [dict(m) for m in messages],
                "tools": [dict(WEATHER_TOOL)],
            }
            if choice is not None:
                body["tool_choice"] = choice
            response = (
                _text_response("It is cold.") if last else _tool_response("get_weather", call_id)
            )
            store.add(headers={"user-agent": "litellm/1.83.9"}, body=body, raw_body_bytes=400,
                      path="/v1/messages", status=200, response_body=response,
                      events=None, streamed=False, latency_s=0.1)
            if not last:
                messages.append({"role": "assistant", "content": response["content"]})
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": call_id,
                                 "content": '{"temp_c": 3}'}],
                })
    store.close()
    return out_dir


FORCE_THEN_AUTO = [{"type": "any"}, {"type": "auto"}]


def test_a_forced_then_auto_capture_becomes_a_per_turn_sequence(tmp_path: Path) -> None:
    """The false-SAFE bug, at its cause (rescue-ops `A-075` §6.3(a)).

    One episode-level `tool_choice` chosen by frequency turns a framework that forces a tool
    on turn 1 and then goes `auto` into one that forces on EVERY turn. The model can then
    never answer in text, so it calls the tool until `max_turns` and the case fails its own
    `turns_at_most` on the BASELINE model — and with a 0-passing baseline the differ printed
    `SAFE`: "the candidate model is a drop-in replacement", over a suite that never worked.
    """
    _two_turn_capture(tmp_path / "cap", shapes=[FORCE_THEN_AUTO, FORCE_THEN_AUTO])
    adapt_from_capture(tmp_path / "cap", tmp_path / "agent")

    config = json.loads((tmp_path / "agent" / "agent.json").read_text())
    assert config["turn_params"] == [
        {"tool_choice": {"type": "any"}},
        {"tool_choice": {"type": "auto"}},
    ]
    # A param that varies by turn belongs to the sequence alone: leaving a frequency-picked
    # copy in `params` would be the same wrong single value, one level down.
    assert "tool_choice" not in config["params"]
    assert config["params"]["max_tokens"] == 512
    edits = (tmp_path / "agent" / "ADAPT_EDITS.md").read_text()
    assert "turn_params" in edits


def test_a_param_the_framework_stopped_sending_is_unset_not_guessed(tmp_path: Path) -> None:
    """Omitting `tool_choice` and sending `"auto"` are different requests; a capture knows
    which happened, so the sequence records `null` rather than inventing a value."""
    _two_turn_capture(tmp_path / "cap", shapes=[[{"type": "any"}, None]])
    adapt_from_capture(tmp_path / "cap", tmp_path / "agent")

    config = json.loads((tmp_path / "agent" / "agent.json").read_text())
    assert config["turn_params"] == [{"tool_choice": {"type": "any"}}, {"tool_choice": None}]


def test_the_replayed_episode_sends_the_recorded_shape_turn_by_turn(tmp_path: Path) -> None:
    """End of the chain: the adapter's sequence has to reach the wire, or it is decoration."""
    _two_turn_capture(tmp_path / "cap", shapes=[FORCE_THEN_AUTO])
    agent_dir = tmp_path / "agent"
    adapt_from_capture(tmp_path / "cap", agent_dir)

    runs = tmp_path / "runs"
    run_suite(agent_dir, SimProvider(), "shape", n_reps=1, model_override="sim-fable-5",
              runs_root=runs, workers=1)
    record = json.loads(
        next((run_dir(runs, "shape") / "cases").glob("*/rep_01.json")).read_text()
    )
    assert [call["request"].get("tool_choice") for call in record["api_calls"]] == [
        {"type": "any"},
        {"type": "auto"},
    ]
    assert record["passed"] is True


def test_a_capture_that_never_varies_writes_no_sequence(adapted: Path) -> None:
    """The one-shot shape (`A-075`, the pydantic-ai smoke, the cookbook fixture) is unchanged:
    no `turn_params` key at all, and `params` exactly as before."""
    config = json.loads((adapted / "agent.json").read_text())
    assert "turn_params" not in config
    assert config["params"] == {"max_tokens": 4096, "tool_choice": {"type": "any"}}


def test_conversations_that_disagree_at_a_turn_are_reported_not_silently_resolved(
    tmp_path: Path,
) -> None:
    """A disagreement the sequence cannot express is a finding, not something to average."""
    _two_turn_capture(
        tmp_path / "cap",
        shapes=[[{"type": "any"}, {"type": "auto"}], [{"type": "auto"}, {"type": "auto"}]],
    )
    result = adapt_from_capture(tmp_path / "cap", tmp_path / "agent")

    assert result.conflicts, "a disagreement between conversations must be recorded"
    edits = (tmp_path / "agent" / "ADAPT_EDITS.md").read_text()
    assert "CONFLICT" in edits
    # Both variants are named, with their counts: the reader decides, not the tool.
    assert '{"type":"any"}' in edits
    assert '{"type":"auto"}' in edits


def test_a_tie_is_not_broken_by_the_order_the_capture_started_in(tmp_path: Path) -> None:
    """`Counter.most_common` broke a 3-3 tie by insertion order, so the same traffic recorded
    in a different order produced a different agent — silently unreproducible either way
    (`A-075` §6.3(a)). The choice must not depend on which turn the recorder started on."""
    first = [{"type": "any"}, {"type": "auto"}]
    second = [{"type": "auto"}, {"type": "auto"}]
    _two_turn_capture(tmp_path / "a", shapes=[first, second])
    _two_turn_capture(tmp_path / "b", shapes=[second, first])
    a = adapt_from_capture(tmp_path / "a", tmp_path / "agent-a")
    b = adapt_from_capture(tmp_path / "b", tmp_path / "agent-b")

    def shape(out: Path) -> tuple:
        config = json.loads((out / "agent.json").read_text())
        return config["params"], config.get("turn_params")

    assert shape(tmp_path / "agent-a") == shape(tmp_path / "agent-b")
    assert a.conflicts and b.conflicts


def test_strict_refuses_a_capture_the_adapter_could_not_reproduce(tmp_path: Path) -> None:
    """`--strict` is the gate the note was not: a capture whose variants were discarded is
    not something to build a run on without a human looking at it first."""
    from upshift import cli

    _two_turn_capture(
        tmp_path / "cap",
        shapes=[[{"type": "any"}, {"type": "auto"}], [{"type": "auto"}, {"type": "auto"}]],
    )
    argv = ["adapt", "--from-capture", str(tmp_path / "cap"), "--out", str(tmp_path / "agent")]
    assert cli.main(argv) == 0
    assert cli.main([*argv[:-1], str(tmp_path / "agent-strict"), "--strict"]) == 3


def test_strict_is_happy_with_a_capture_that_reproduces_exactly(tmp_path: Path) -> None:
    from upshift import cli

    _two_turn_capture(tmp_path / "cap", shapes=[FORCE_THEN_AUTO, FORCE_THEN_AUTO])
    assert cli.main([
        "adapt", "--from-capture", str(tmp_path / "cap"),
        "--out", str(tmp_path / "agent"), "--strict",
    ]) == 0
