"""Running off the end of a recording: `agent.json` `continuation` (DESIGN.md §F).

A capture holds N assistant turns. Turn N+1 has no recorded params, because the framework never
sent any. Before this policy the loop simply kept going on the last recorded turn's values and
scored whatever happened as behaviour — which is how a two-turn recording turned into
"evidence" about turn nine.

The five Anthropic-track cases in `FRAMEWORK_REASSESSMENT.md` §3 Group C (`A-007`, `A-011`,
`A-023`, `A-061`, `A-062`) all live in this region: a `tool_use` with no recorded
`tool_result`, a trailing assistant prefill turn, a batch whose results were reordered. Each is
a conversation that continues past what a capture can supply.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift.agent_loop import run_episode
from upshift.capture import continuation
from upshift.capture.adapt import adapt_from_capture
from upshift.capture.record import CaptureStore
from upshift.native.protocol import CONTINUATION_EXHAUSTED, is_non_behavioural
from upshift.schemas import AgentConfig, Case


def _two_turn_capture(out_dir: Path) -> Path:
    """A recording exactly two assistant turns deep."""
    store = CaptureStore(out_dir, listen="127.0.0.1:0", upstream="x", mode="forward")
    tools = [
        {
            "name": "lookup",
            "description": "look something up",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    messages: list[dict] = [{"role": "user", "content": "who won?"}]
    for turn in (1, 2):
        call = {"type": "tool_use", "id": f"toolu_{turn}", "name": "lookup", "input": {"q": "x"}}
        store.add(
            headers={"user-agent": "anthropic-sdk-python/1.2.0"},
            body={
                "model": "claude-fable-5",
                "max_tokens": 1024,
                "messages": list(messages),
                "system": "Answer with tools.",
                "tools": tools,
            },
            raw_body_bytes=300,
            path="/v1/messages",
            status=200,
            response_body={
                "id": f"msg_{turn}",
                "type": "message",
                "role": "assistant",
                "model": "claude-fable-5",
                "content": [call],
                "stop_reason": "tool_use",
            },
            events=None,
            streamed=False,
            latency_s=0.1,
        )
        messages += [
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                          "content": "ok"}]},
        ]
    store.close()
    return out_dir


class _AlwaysCallsATool:
    """A model that never stops — the shape that runs off the end of every recording."""

    def __init__(self) -> None:
        self.calls = 0

    def call(self, endpoint, request, seed_key, sim_context):
        self.calls += 1
        return {
            "id": "msg", "type": "message", "role": "assistant", "model": "m",
            "content": [{"type": "tool_use", "id": f"t{self.calls}", "name": "lookup",
                         "input": {"q": str(self.calls)}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _Backend:
    def execute(self, name, arguments):
        return {"ok": True}

    def state(self):
        return {}


@pytest.fixture
def agent(tmp_path: Path) -> Path:
    adapt_from_capture(_two_turn_capture(tmp_path / "cap"), tmp_path / "agent")
    return tmp_path / "agent"


def _episode(config: AgentConfig, provider) -> object:
    return run_episode(
        config,
        Case(id="c", description="", initial_state={}, user_messages=["who won?"], checks=[]),
        provider,
        _Backend(),
        rep=1,
        seed=1,
    )


def test_adapt_records_how_deep_the_recording_went(agent: Path) -> None:
    config = json.loads((agent / "agent.json").read_text())
    assert config["recorded_turns"] == 2
    assert config["continuation"] == "fail"  # the default, written explicitly
    assert config["max_turns"] == 3  # one spare turn, so the policy is what stops the episode


def test_fail_is_the_default_and_ends_the_episode_at_the_edge_of_the_recording(agent: Path) -> None:
    config = AgentConfig.load(agent)
    assert config.continuation == "fail"
    provider = _AlwaysCallsATool()
    episode = _episode(config, provider)
    assert provider.calls == 2  # exactly the recorded depth, not max_turns
    assert episode.api_error["error_type"] == CONTINUATION_EXHAUSTED
    assert is_non_behavioural(episode.api_error)  # inconclusive, NOT a regression
    assert "recorded 2" in episode.api_error["message"]
    assert "repeat_last" in episode.api_error["message"]  # the message says what to change
    assert getattr(episode, "continuation_used", False) is False


def test_repeat_last_keeps_going_and_says_so_in_the_record(agent: Path) -> None:
    raw = json.loads((agent / "agent.json").read_text())
    raw["continuation"] = "repeat_last"
    raw["max_turns"] = 5
    (agent / "agent.json").write_text(json.dumps(raw))
    config = AgentConfig.load(agent)
    provider = _AlwaysCallsATool()
    episode = _episode(config, provider)
    assert provider.calls == 5  # ran to max_turns, reusing the last recorded turn's params
    assert episode.api_error is None
    assert episode.continuation_used is True  # never silently


def test_the_policy_is_inert_for_an_agent_that_was_never_a_recording(tmp_path: Path) -> None:
    """A hand-written agent has no recording to run off the end of; nothing changes for it."""
    agent_dir = Path(__file__).resolve().parent / "todo_agent"
    config = AgentConfig.load(agent_dir)
    assert config.recorded_turns is None
    assert continuation.recorded_turns(config) is None
    assert continuation.exhausted(config, 99) is False
    assert continuation.before_turn(config, object(), 99) is None


def test_an_unknown_policy_is_an_authoring_error(agent: Path) -> None:
    raw = json.loads((agent / "agent.json").read_text())
    raw["continuation"] = "keep_going_and_hope"
    (agent / "agent.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="unknown continuation policy"):
        AgentConfig.load(agent)


def test_recorded_turns_must_be_a_positive_integer(agent: Path) -> None:
    raw = json.loads((agent / "agent.json").read_text())
    raw["recorded_turns"] = 0
    (agent / "agent.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="positive integer"):
        AgentConfig.load(agent)


def test_adapt_edits_explains_the_policy_it_wrote(agent: Path) -> None:
    edits = (agent / "ADAPT_EDITS.md").read_text()
    assert "Continuation past the recording" in edits
    assert "continuation_exhausted" in edits
    assert "ran 2 assistant turn(s)" in edits
