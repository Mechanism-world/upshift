"""The volatile suffix (ADAPTER.md, "Volatile suffix"): a fixed message an adapter declares in
agent.json that upshift appends to EVERY outgoing request, after the whole conversation.

Motivating case: everruns (rescue lab A-015) appends a live-facts block as a trailing user-role
message on every request, and the block carries the current time — so upstream's model is told
the time and then asked what time it is. Without a way to send that block the adapter's model
had no clock, called the tool 15/15 on both models, and the case closed NO_REGRESSION.

Invariants pinned here:
- the suffix is the LAST item of every request, on all three endpoints, and appears exactly once
  per request (it never enters the replayed history);
- it sits behind every cache breakpoint on `messages` and never carries one itself;
- it is a literal from agent.json — validated at load time and in the preflight — and the sim
  provider and the repair playbook are indifferent to it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_agent_loop import (
    ScriptedProvider,
    StubBackend,
    chat_text,
    chat_tools,
    make_case,
    make_config,
    resp_text,
    resp_tools,
)
from test_messages_endpoint import msg_text, msg_tools

from upshift import cli
from upshift.agent_loop import build_request, run_episode, volatile_suffix_item
from upshift.providers.sim import SimProvider
from upshift.recorder import load_case_reps, run_dir
from upshift.repair import playbook
from upshift.runner import run_suite
from upshift.schemas import AgentConfig, load_volatile_suffix

TODO_AGENT = Path(__file__).resolve().parent / "todo_agent"

SUFFIX = "<dynamic_facts>\ncurrent_time: 2026-09-02T12:00:00Z\n</dynamic_facts>"
SUFFIX_ITEM = {"role": "user", "content": SUFFIX}


def _items(endpoint: str, request: dict) -> list[dict]:
    return request["input"] if endpoint == "responses" else request["messages"]


def _count_suffix(items: list[dict]) -> int:
    return sum(1 for item in items if item == SUFFIX_ITEM)


# ---------------------------------------------------------------------------
# Placement: last on every request, once, on every endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "script"),
    [
        ("chat_completions", [chat_tools([("search_flights", {"origin": "SFO"})]), chat_text("ok")]),
        ("responses", [resp_tools([("search_flights", {"origin": "SFO"})]), resp_text("ok")]),
        ("messages", [msg_tools([("search_flights", {"origin": "SFO"})]), msg_text("ok")]),
    ],
)
def test_suffix_is_the_last_item_of_every_request_and_never_enters_history(endpoint, script):
    provider = ScriptedProvider(script)
    config = make_config(endpoint=endpoint, volatile_suffix=SUFFIX)

    run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert len(provider.calls) == 2
    for call in provider.calls:
        items = _items(endpoint, call["request"])
        assert items[-1] == SUFFIX_ITEM
        assert _count_suffix(items) == 1
    # The second request replays the whole first turn (user, assistant tool call, tool result)
    # and THEN the suffix — the copy from request one was not carried into the history.
    second = _items(endpoint, provider.calls[1]["request"])
    assert second[-2] != SUFFIX_ITEM
    # (+2 on every endpoint: the assistant tool-call item and the tool-result item.)
    assert len(second) == len(_items(endpoint, provider.calls[0]["request"])) + 2


def test_suffix_follows_the_newest_user_segment_in_a_multi_segment_case():
    provider = ScriptedProvider([chat_text("first answer"), chat_text("second answer")])
    config = make_config(volatile_suffix=SUFFIX)
    case = make_case(user_messages=["Book me a flight.", "Actually, cancel that."])

    result = run_episode(config, case, provider, StubBackend(), rep=0, seed=0)

    assert result.final_message == "second answer"
    second = provider.calls[1]["request"]["messages"]
    assert second == [
        {"role": "system", "content": "You are a booking agent."},
        {"role": "user", "content": "Book me a flight."},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "Actually, cancel that."},
        SUFFIX_ITEM,
    ]


def test_no_suffix_means_the_request_is_byte_identical_to_before():
    items = [{"role": "user", "content": "hi"}]
    for endpoint in ("chat_completions", "responses", "messages"):
        without = build_request(endpoint, "m", {}, [], items, system="s")
        explicit_none = build_request(
            endpoint, "m", {}, [], items, system="s", volatile_suffix=None
        )
        assert without == explicit_none
        assert _items(endpoint, without) == items
    assert make_config().volatile_suffix is None


def test_build_request_never_mutates_the_callers_history():
    items = [{"role": "user", "content": "hi"}]
    request = build_request("chat_completions", "m", {}, [], items, volatile_suffix=SUFFIX)
    assert items == [{"role": "user", "content": "hi"}]
    request["messages"][0]["content"] = "mutated"
    assert items[0]["content"] == "hi"


def test_the_wire_item_is_a_plain_user_text_message():
    assert volatile_suffix_item("x") == {"role": "user", "content": "x"}


# ---------------------------------------------------------------------------
# Caching: the tail sits behind every breakpoint and is never marked
# ---------------------------------------------------------------------------


def test_messages_breakpoints_stay_on_system_and_last_tool_ahead_of_the_volatile_tail():
    provider = ScriptedProvider(
        [msg_tools([("search_flights", {"origin": "SFO"})]), msg_text("done")]
    )
    config = make_config(endpoint="messages", volatile_suffix=SUFFIX)

    run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    for call in provider.calls:
        request = call["request"]
        # Both breakpoints are where they were before the suffix existed …
        assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in request["tools"][0]
        # … and nothing in `messages`, the volatile tail included, carries one.
        for message in request["messages"]:
            assert "cache_control" not in json.dumps(message)
        assert request["messages"][-1] == SUFFIX_ITEM
    # On the tool-result turn the suffix follows the tool_result user message; Anthropic
    # combines consecutive user turns, tool_result blocks first, which is the required order.
    second = provider.calls[1]["request"]["messages"]
    assert second[-2]["role"] == "user"
    assert second[-2]["content"][0]["type"] == "tool_result"


def test_messages_with_empty_prompt_and_no_tools_still_puts_the_suffix_last():
    request = build_request(
        "messages", "m", {}, [], [{"role": "user", "content": "q"}], system="",
        volatile_suffix=SUFFIX,
    )
    assert "system" not in request
    assert request["tools"] == []
    assert request["messages"] == [{"role": "user", "content": "q"}, SUFFIX_ITEM]


# ---------------------------------------------------------------------------
# Declaration: a literal in agent.json, validated on load and in the preflight
# ---------------------------------------------------------------------------


def _agent_copy(tmp_path: Path, **agent_json_overrides) -> Path:
    agent_dir = tmp_path / "agent"
    shutil.copytree(TODO_AGENT, agent_dir, ignore=shutil.ignore_patterns("__pycache__"))
    path = agent_dir / "agent.json"
    raw = json.loads(path.read_text())
    raw.update(agent_json_overrides)
    path.write_text(json.dumps(raw, indent=2) + "\n")
    return agent_dir


def test_agent_json_suffix_loads_verbatim(tmp_path: Path):
    agent_dir = _agent_copy(tmp_path, volatile_suffix=SUFFIX)
    config = AgentConfig.load(agent_dir)
    assert config.volatile_suffix == SUFFIX
    # It lives in agent.json, so it is part of the recorded, hashed, patchable surface.
    assert "agent.json" in config.file_hashes()


def test_absent_and_null_both_mean_not_sent(tmp_path: Path):
    assert AgentConfig.load(_agent_copy(tmp_path)).volatile_suffix is None
    assert load_volatile_suffix({"volatile_suffix": None}, "agent.json") is None
    assert load_volatile_suffix({}, "agent.json") is None


@pytest.mark.parametrize("bad", [42, ["a", "b"], {"text": "a"}, True])
def test_a_non_string_suffix_is_an_authoring_error(tmp_path: Path, bad):
    agent_dir = _agent_copy(tmp_path, volatile_suffix=bad)
    with pytest.raises(ValueError, match="volatile_suffix must be a string"):
        AgentConfig.load(agent_dir)
    with pytest.raises(ValueError, match="volatile_suffix must be a string"):
        cli.validate_agent_dir(agent_dir)


@pytest.mark.parametrize("empty", ["", "   \n"])
def test_an_empty_suffix_is_rejected_not_silently_dropped(tmp_path: Path, empty):
    agent_dir = _agent_copy(tmp_path, volatile_suffix=empty)
    with pytest.raises(ValueError, match="volatile_suffix is empty"):
        AgentConfig.load(agent_dir)
    with pytest.raises(ValueError, match="volatile_suffix is empty"):
        cli.validate_agent_dir(agent_dir)


def test_preflight_accepts_a_valid_suffix(tmp_path: Path):
    raw = cli.validate_agent_dir(_agent_copy(tmp_path, volatile_suffix=SUFFIX))
    assert raw["volatile_suffix"] == SUFFIX


# ---------------------------------------------------------------------------
# End to end: the sim provider tolerates the trailing message and the run records it
# ---------------------------------------------------------------------------


def test_sim_suite_passes_with_a_suffix_and_every_recorded_request_ends_with_it(tmp_path: Path):
    agent_dir = _agent_copy(tmp_path, volatile_suffix=SUFFIX)
    runs_root = tmp_path / "runs"

    run_suite(
        agent_dir, SimProvider(), "with-suffix", n_reps=2, model_override="sim-5.5",
        runs_root=runs_root, workers=1,
    )

    directory = run_dir(runs_root, "with-suffix")
    case_ids = sorted(p.name for p in (directory / "cases").iterdir())
    assert case_ids
    seen_requests = 0
    for case_id in case_ids:
        reps = load_case_reps(directory, case_id)
        assert len(reps) == 2
        for rep in reps:
            assert rep.passed, (case_id, [c.detail for c in rep.check_results if not c.passed])
            for call in rep.api_calls:
                seen_requests += 1
                assert call.request["messages"][-1] == SUFFIX_ITEM
                assert _count_suffix(call.request["messages"]) == 1
    assert seen_requests > 0


def test_the_same_agent_without_the_suffix_sends_none(tmp_path: Path):
    agent_dir = _agent_copy(tmp_path)
    runs_root = tmp_path / "runs"
    run_suite(
        agent_dir, SimProvider(), "plain", n_reps=1, model_override="sim-5.5",
        runs_root=runs_root, workers=1,
    )
    directory = run_dir(runs_root, "plain")
    for case_dir in (directory / "cases").iterdir():
        for rep in load_case_reps(directory, case_dir.name):
            for call in rep.api_calls:
                assert _count_suffix(call.request["messages"]) == 0


# ---------------------------------------------------------------------------
# Repair: the playbook's agent.json edits leave the suffix untouched
# ---------------------------------------------------------------------------


def test_playbook_edits_preserve_the_suffix_on_both_edit_paths(tmp_path: Path):
    agent_dir = _agent_copy(
        tmp_path, volatile_suffix=SUFFIX, params={"reasoning_effort": "medium", "top_p": 0.9}
    )
    # Minimal textual edit of an existing key.
    edit = playbook._agent_json_edit(agent_dir, ["params", "reasoning_effort"], "high")
    raw = json.loads(edit.new_content)
    assert raw["volatile_suffix"] == SUFFIX
    assert raw["params"]["reasoning_effort"] == "high"
    # Full re-serialize path (a key that did not exist yet).
    edit = playbook._agent_json_edit(agent_dir, ["params", "brand_new"], 1)
    raw = json.loads(edit.new_content)
    assert raw["volatile_suffix"] == SUFFIX
    # Param removal.
    edit = playbook._agent_json_remove(agent_dir, ["top_p"])
    raw = json.loads(edit.new_content)
    assert raw["volatile_suffix"] == SUFFIX
    assert "top_p" not in raw["params"]
