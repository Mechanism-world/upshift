"""Agent-loop tests. Everything is stubbed: a scripted Provider returning canned wire-format
responses plus a trivial backend. No network, no victim agent, no checks."""

from __future__ import annotations

import copy
import json

from upshift.agent_loop import run_episode
from upshift.providers.base import Provider, ProviderAPIError
from upshift.schemas import AgentConfig, Case

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": "Search for flights.",
            "parameters": {"type": "object", "properties": {"origin": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_flight",
            "description": "Book a flight.",
            "parameters": {"type": "object", "properties": {"flight_id": {"type": "string"}}},
        },
    },
]


def make_config(**overrides) -> AgentConfig:
    base = {
        "name": "booking",
        "endpoint": "chat_completions",
        "model": "m-base",
        "params": {"reasoning_effort": "medium"},
        "system_prompt": "You are a booking agent.",
        "tools": copy.deepcopy(TOOLS),
        "max_turns": 12,
        "agent_dir": "/nonexistent/agent",
    }
    base.update(overrides)
    return AgentConfig(**base)


def make_case(**overrides) -> Case:
    base = {
        "id": "case-1",
        "description": "",
        "initial_state": {},
        "user_messages": ["Book me a flight."],
        "checks": [],
        "sim": {},
    }
    base.update(overrides)
    return Case(**base)


class ScriptedProvider(Provider):
    """Returns canned responses in order; the last entry repeats. Exception entries raise."""

    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def call(self, endpoint, request, seed_key, sim_context=None):
        self.calls.append(
            {
                "endpoint": endpoint,
                "request": copy.deepcopy(request),
                "seed_key": seed_key,
                "sim_context": copy.deepcopy(sim_context),
            }
        )
        index = len(self.calls) - 1
        item = self.script[index] if index < len(self.script) else self.script[-1]
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


class StubBackend:
    def __init__(self):
        self.executed = []

    def execute(self, name, arguments):
        self.executed.append((name, arguments))
        return {"ok": True, "tool": name, "seq": len(self.executed)}

    def state(self):
        return {"executed": [name for name, _ in self.executed]}


# -- canned wire-format responses -------------------------------------------


def chat_tools(calls, model="m-base", usage=None, ids=None):
    tool_calls = []
    for index, (name, arguments) in enumerate(calls):
        call_id = ids[index] if ids else f"call_{index}"
        payload = arguments if isinstance(arguments, str) else json.dumps(arguments)
        tool_calls.append(
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": payload}}
        )
    out = {
        "id": "chatcmpl-x",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
            }
        ],
    }
    if usage is not None:
        out["usage"] = usage
    return out


def chat_text(text, model="m-base", usage=None):
    out = {
        "id": "chatcmpl-y",
        "model": model,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
    }
    if usage is not None:
        out["usage"] = usage
    return out


def resp_tools(calls, model="m-base", usage=None, ids=None):
    output = []
    for index, (name, arguments) in enumerate(calls):
        call_id = ids[index] if ids else f"call_{index}"
        payload = arguments if isinstance(arguments, str) else json.dumps(arguments)
        output.append(
            {
                "type": "function_call",
                "id": f"fc_{index}",
                "call_id": call_id,
                "name": name,
                "arguments": payload,
                "status": "completed",
            }
        )
    out = {"id": "resp-x", "model": model, "output": output}
    if usage is not None:
        out["usage"] = usage
    return out


def resp_text(text, model="m-base", usage=None, with_reasoning=True):
    output = []
    if with_reasoning:
        output.append({"type": "reasoning", "id": "rs_0", "summary": []})
    output.append(
        {
            "type": "message",
            "id": "msg_0",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    )
    out = {"id": "resp-y", "model": model, "output": output}
    if usage is not None:
        out["usage"] = usage
    return out


# -- tests ------------------------------------------------------------------


def test_multi_segment_conversation_flow():
    provider = ScriptedProvider(
        [
            chat_tools([("search_flights", {"origin": "SFO"})]),
            chat_text("Here are two options."),
            chat_tools([("book_flight", {"flight_id": "F1"})]),
            chat_text("Booked, confirmation UPS-1."),
        ]
    )
    backend = StubBackend()
    case = make_case(user_messages=["Find flights.", "Book the first one."])

    result = run_episode(make_config(), case, provider, backend, rep=3, seed=7)

    assert len(result.api_calls) == 4
    assert result.final_message == "Booked, confirmation UPS-1."
    assert result.api_error is None
    assert [c["seed_key"] for c in provider.calls] == [
        "case-1:3:0",
        "case-1:3:1",
        "case-1:3:2",
        "case-1:3:3",
    ]
    assert provider.calls[0]["sim_context"] == {"case_id": "case-1", "rep": 3, "sim": {}}

    # the second segment's request carries the whole prior conversation
    messages = provider.calls[3]["request"]["messages"]
    roles = [m.get("role") for m in messages]
    assert roles == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]
    assert messages[0]["content"] == "You are a booking agent."
    assert messages[1]["content"] == "Find flights."
    assert messages[4] == {"role": "assistant", "content": "Here are two options."}
    assert messages[5]["content"] == "Book the first one."
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call_0",
        "content": json.dumps({"ok": True, "tool": "search_flights", "seq": 1}),
    }


def test_tool_executions_record_turn_and_segment():
    provider = ScriptedProvider(
        [
            chat_tools(
                [("search_flights", {"origin": "SFO"}), ("search_flights", {"origin": "LAX"})]
            ),
            chat_text("segment one done"),
            chat_tools([("book_flight", {"flight_id": "F1"})]),
            chat_text("segment two done"),
        ]
    )
    backend = StubBackend()
    case = make_case(user_messages=["one", "two"])

    result = run_episode(make_config(), case, provider, backend, rep=0, seed=0)

    assert [(t.turn, t.segment, t.name) for t in result.tool_executions] == [
        (0, 0, "search_flights"),
        (0, 0, "search_flights"),
        (2, 1, "book_flight"),
    ]
    assert result.tool_executions[0].arguments == {"origin": "SFO"}
    assert result.tool_executions[2].result == {"ok": True, "tool": "book_flight", "seq": 3}
    assert backend.executed[1] == ("search_flights", {"origin": "LAX"})
    assert result.final_state == {"executed": ["search_flights", "search_flights", "book_flight"]}


def test_chat_request_shape_and_param_mapping():
    provider = ScriptedProvider([chat_text("done")])
    config = make_config(
        params={"reasoning_effort": "low", "temperature": 0.2, "service_tier": "flex"}
    )

    run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    request = provider.calls[0]["request"]
    assert provider.calls[0]["endpoint"] == "chat_completions"
    assert request["model"] == "m-base"
    assert request["tools"] == TOOLS
    assert request["reasoning_effort"] == "low"
    assert request["temperature"] == 0.2
    assert request["service_tier"] == "flex"  # unknown params pass through
    assert "store" not in request
    assert "reasoning" not in request
    assert request["messages"] == [
        {"role": "system", "content": "You are a booking agent."},
        {"role": "user", "content": "Book me a flight."},
    ]


def test_responses_request_shape_tool_conversion_and_param_mapping():
    provider = ScriptedProvider(
        [resp_tools([("search_flights", {"origin": "SFO"})], ids=["fc_call_a"]), resp_text("done")]
    )
    config = make_config(
        endpoint="responses", params={"reasoning_effort": "high", "temperature": 1.0, "top_p": 0.5}
    )

    result = run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    first = provider.calls[0]["request"]
    assert provider.calls[0]["endpoint"] == "responses"
    assert first["store"] is False
    assert first["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in first
    assert first["temperature"] == 1.0
    assert first["top_p"] == 0.5
    assert first["tools"] == [
        {
            "type": "function",
            "name": "search_flights",
            "description": "Search for flights.",
            "parameters": {"type": "object", "properties": {"origin": {"type": "string"}}},
        },
        {
            "type": "function",
            "name": "book_flight",
            "description": "Book a flight.",
            "parameters": {"type": "object", "properties": {"flight_id": {"type": "string"}}},
        },
    ]
    assert first["input"] == [
        {"role": "system", "content": "You are a booking agent."},
        {"role": "user", "content": "Book me a flight."},
    ]

    # the full input list is resent, with the function call and its output appended
    second = provider.calls[1]["request"]["input"]
    assert second[2] == {
        "type": "function_call",
        "call_id": "fc_call_a",
        "name": "search_flights",
        "arguments": json.dumps({"origin": "SFO"}),
    }
    assert second[3] == {
        "type": "function_call_output",
        "call_id": "fc_call_a",
        "output": json.dumps({"ok": True, "tool": "search_flights", "seq": 1}),
    }
    assert result.final_message == "done"  # "reasoning" output items are ignored


def test_malformed_tool_arguments_do_not_crash():
    provider = ScriptedProvider(
        [chat_tools([("book_flight", "{not json")]), chat_text("sorry, retrying later")]
    )
    backend = StubBackend()

    result = run_episode(make_config(), make_case(), provider, backend, rep=0, seed=0)

    assert backend.executed == []  # nothing executed
    execution = result.tool_executions[0]
    assert execution.name == "book_flight"
    assert execution.arguments == {"_raw": "{not json"}
    assert execution.result == {"error": "invalid JSON in tool call arguments"}
    # ... and the error is fed back to the model as the tool result
    follow_up = provider.calls[1]["request"]["messages"][-1]
    assert follow_up["role"] == "tool"
    assert json.loads(follow_up["content"]) == {"error": "invalid JSON in tool call arguments"}
    assert result.final_message == "sorry, retrying later"


def test_api_error_records_call_and_ends_episode():
    error = ProviderAPIError(message="boom", status_code=400, error_type="invalid_request_error")
    provider = ScriptedProvider(
        [chat_tools([("search_flights", {"origin": "SFO"})]), error, chat_text("never reached")]
    )
    backend = StubBackend()

    result = run_episode(make_config(), make_case(), provider, backend, rep=1, seed=0)

    assert len(provider.calls) == 2
    assert len(result.api_calls) == 2
    failed = result.api_calls[1]
    assert failed.response is None
    assert failed.error == {"status_code": 400, "message": "boom", "type": "invalid_request_error"}
    assert failed.request["model"] == "m-base"  # the request is still recorded verbatim
    assert result.api_error == failed.error
    assert result.final_message == ""
    assert result.resolved_model == "m-base"  # from the last successful response
    assert result.final_state == {"executed": ["search_flights"]}


def test_max_turns_cap():
    provider = ScriptedProvider([chat_tools([("search_flights", {"origin": "SFO"})])])
    config = make_config(max_turns=3)

    result = run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert len(result.api_calls) == 3
    assert len(result.tool_executions) == 3
    assert result.final_message == ""
    assert result.api_error is None


def test_max_turns_keeps_last_text_seen():
    provider = ScriptedProvider(
        [
            chat_tools([("search_flights", {})]),
            chat_text("partial answer"),
            chat_tools([("search_flights", {})]),
            chat_tools([("search_flights", {})]),
        ]
    )
    config = make_config(max_turns=4)
    case = make_case(user_messages=["one", "two"])

    result = run_episode(config, case, provider, StubBackend(), rep=0, seed=0)

    assert len(result.api_calls) == 4
    assert result.final_message == "partial answer"


def test_usage_accumulation_chat_and_missing_usage():
    provider = ScriptedProvider(
        [
            chat_tools(
                [("search_flights", {})],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            ),
            chat_text("no usage on this one"),
        ]
    )

    result = run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert result.usage == {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 0}


def test_usage_accumulation_responses():
    provider = ScriptedProvider(
        [
            resp_tools([("search_flights", {})], usage={"input_tokens": 50, "output_tokens": 5}),
            resp_text("done", usage={"input_tokens": 70, "output_tokens": 9}),
        ]
    )
    config = make_config(endpoint="responses")

    result = run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert result.usage == {"input_tokens": 120, "output_tokens": 14, "cached_input_tokens": 0}


def test_resolved_model_is_last_successful_response_model():
    provider = ScriptedProvider(
        [
            chat_tools([("search_flights", {})], model="gpt-5.6-sol-2026-08-01"),
            chat_text("done", model="gpt-5.6-sol-2026-08-02"),
        ]
    )

    result = run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert result.resolved_model == "gpt-5.6-sol-2026-08-02"


def test_resolved_model_none_when_nothing_succeeded():
    provider = ScriptedProvider([ProviderAPIError(message="down", status_code=503)])

    result = run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert result.resolved_model is None
    assert result.api_error == {"status_code": 503, "message": "down", "type": "api_error"}


def test_overrides_take_precedence_over_config():
    provider = ScriptedProvider([resp_text("done")])
    config = make_config(params={"reasoning_effort": "medium"})

    run_episode(
        config,
        make_case(),
        provider,
        StubBackend(),
        rep=0,
        seed=0,
        model_override="m-candidate",
        params_override={"reasoning_effort": "none"},
        endpoint_override="responses",
    )

    request = provider.calls[0]["request"]
    assert provider.calls[0]["endpoint"] == "responses"
    assert request["model"] == "m-candidate"
    assert request["reasoning"] == {"effort": "none"}


def test_recorded_requests_are_snapshots_not_aliases():
    provider = ScriptedProvider([chat_tools([("search_flights", {})]), chat_text("done")])

    result = run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert len(result.api_calls[0].request["messages"]) == 2
    assert len(result.api_calls[1].request["messages"]) == 4


def test_empty_user_messages_is_a_no_op():
    provider = ScriptedProvider([chat_text("never")])
    backend = StubBackend()

    result = run_episode(
        make_config(), make_case(user_messages=[]), provider, backend, rep=0, seed=0
    )

    assert provider.calls == []
    assert result.api_calls == []
    assert result.final_message == ""
    assert result.final_state == {"executed": []}


# ---------------------------------------------------------------------------
# Per-turn params (agent.json `turn_params`)
# ---------------------------------------------------------------------------


def test_without_turn_params_every_turn_sends_the_same_params():
    """The default, and what every hand-written agent does."""
    provider = ScriptedProvider([chat_tools([("search_flights", {})]), chat_text("done")])

    run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert [c["request"]["reasoning_effort"] for c in provider.calls] == ["medium", "medium"]


def test_turn_params_are_applied_by_turn_index_and_the_last_entry_repeats():
    """A framework that forces a tool on turn 1 and goes `auto` afterwards (pydantic-ai,
    litellm) is a DIFFERENT agent from one that forces on every turn: under a forced choice
    the model can never answer in text, so the episode runs to max_turns and fails its own
    `turns_at_most` on the BASELINE model. One episode-level value cannot express it."""
    provider = ScriptedProvider(
        [
            chat_tools([("search_flights", {})]),
            chat_tools([("book_flight", {})]),
            chat_tools([("book_flight", {"again": True})]),
            chat_text("done"),
        ]
    )
    config = make_config(
        params={"max_tokens": 512, "tool_choice": "required"},
        turn_params=[{"tool_choice": "required"}, {"tool_choice": "auto"}],
    )

    run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert [c["request"]["tool_choice"] for c in provider.calls] == [
        "required",
        "auto",
        "auto",  # the last entry repeats for every later turn
        "auto",
    ]
    # A param the sequence does not mention keeps its `params` value on every turn.
    assert {c["request"]["max_tokens"] for c in provider.calls} == {512}


def test_a_turn_param_of_null_unsets_the_param_for_that_turn():
    """A framework that sends `tool_choice` on the forced turn and OMITS the field afterwards
    is not the same as one that sends `"auto"`; `null` is how the sequence says "not sent"."""
    provider = ScriptedProvider([chat_tools([("search_flights", {})]), chat_text("done")])
    config = make_config(
        params={"tool_choice": "required"},
        turn_params=[{"tool_choice": "required"}, {"tool_choice": None}],
    )

    run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert provider.calls[0]["request"]["tool_choice"] == "required"
    assert "tool_choice" not in provider.calls[1]["request"]


def test_turn_params_ride_on_top_of_a_params_override():
    """The repair loop and `--endpoint` overrides replace `params`; the per-turn shape is a
    property of the agent and must still apply over whatever params are in force."""
    provider = ScriptedProvider([chat_tools([("search_flights", {})]), chat_text("done")])
    config = make_config(
        params={"tool_choice": "required", "max_tokens": 512},
        turn_params=[{"tool_choice": "required"}, {"tool_choice": "auto"}],
    )

    run_episode(
        config, make_case(), provider, StubBackend(), rep=0, seed=0,
        params_override={"max_tokens": 99},
    )

    assert provider.calls[0]["request"]["max_tokens"] == 99
    assert provider.calls[0]["request"]["tool_choice"] == "required"
    assert provider.calls[1]["request"]["tool_choice"] == "auto"


def test_turn_params_never_mutate_the_agents_own_params():
    provider = ScriptedProvider([chat_tools([("search_flights", {})]), chat_text("done")])
    config = make_config(
        params={"tool_choice": "required"},
        turn_params=[{"tool_choice": "required"}, {"tool_choice": {"type": "auto"}}],
    )

    run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert config.params == {"tool_choice": "required"}
    assert config.turn_params == [{"tool_choice": "required"}, {"tool_choice": {"type": "auto"}}]
