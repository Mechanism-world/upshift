"""Anthropic `messages` endpoint: request building, conversation replay, response parsing,
usage, the provider's error mapping, pricing and the CLI's provider/model guards.

Everything is stubbed — a scripted provider and a fake anthropic client. No network, no key.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from upshift import agent_loop, cli, differ
from upshift.agent_loop import (
    DEFAULT_MAX_TOKENS,
    build_request,
    convert_tools_messages,
    map_params,
    run_episode,
)
from upshift.differ import failure_signatures
from upshift.pricing import price
from upshift.providers.anthropic_provider import (
    SAMPLING_PARAMS,
    AnthropicProvider,
    messages_create_accepts,
)
from upshift.providers.base import Provider, ProviderAPIError, get_provider
from upshift.recorder import load_case_reps
from upshift.runner import run_suite
from upshift.schemas import AgentConfig, Case

TODO_AGENT = Path(__file__).resolve().parent / "todo_agent"

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
        "endpoint": "messages",
        "model": "claude-fable-5",
        "params": {},
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
    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def call(self, endpoint, request, seed_key, sim_context=None):
        self.calls.append({"endpoint": endpoint, "request": copy.deepcopy(request)})
        index = len(self.calls) - 1
        item = self.script[index] if index < len(self.script) else self.script[-1]
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


class StubBackend:
    def __init__(self, results=None):
        self.executed = []
        self.results = results or {}

    def execute(self, name, arguments):
        self.executed.append((name, arguments))
        if name in self.results:
            return self.results[name]
        return {"ok": True, "tool": name, "seq": len(self.executed)}

    def state(self):
        return {"executed": [name for name, _ in self.executed]}


def msg_tools(calls, model="claude-fable-5", usage=None, thinking=None):
    content = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking, "signature": "sig-abc"})
    for index, (name, arguments) in enumerate(calls):
        content.append(
            {"type": "tool_use", "id": f"toolu_{index}", "name": name, "input": arguments}
        )
    out = {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use",
    }
    if usage is not None:
        out["usage"] = usage
    return out


def msg_text(text, model="claude-fable-5", usage=None, extra_blocks=()):
    out = {
        "id": "msg_y",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [*extra_blocks, {"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }
    if usage is not None:
        out["usage"] = usage
    return out


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def test_tool_conversion_to_input_schema():
    assert convert_tools_messages(TOOLS) == [
        {
            "name": "search_flights",
            "description": "Search for flights.",
            "input_schema": {"type": "object", "properties": {"origin": {"type": "string"}}},
        },
        {
            "name": "book_flight",
            "description": "Book a flight.",
            "input_schema": {"type": "object", "properties": {"flight_id": {"type": "string"}}},
        },
    ]


def test_request_shape_system_is_a_field_not_a_turn():
    provider = ScriptedProvider([msg_text("done")])

    run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    request = provider.calls[0]["request"]
    assert provider.calls[0]["endpoint"] == "messages"
    assert request["model"] == "claude-fable-5"
    assert request["system"] == [
        {
            "type": "text",
            "text": "You are a booking agent.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert request["max_tokens"] == DEFAULT_MAX_TOKENS
    assert request["messages"] == [{"role": "user", "content": "Book me a flight."}]

    expected_tools = convert_tools_messages(TOOLS)
    expected_tools[-1]["cache_control"] = {"type": "ephemeral"}
    assert request["tools"] == expected_tools


def test_only_the_last_tool_carries_a_cache_breakpoint():
    provider = ScriptedProvider([msg_text("done")])

    run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    tools = provider.calls[0]["request"]["tools"]
    assert len(tools) == 2
    assert "cache_control" not in tools[0]
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    # everything else about the definition is untouched
    assert tools[-1]["name"] == "book_flight"
    assert tools[-1]["input_schema"] == TOOLS[1]["function"]["parameters"]


def test_cache_control_is_never_placed_on_messages():
    provider = ScriptedProvider(
        [msg_tools([("search_flights", {"origin": "SFO"})]), msg_text("done")]
    )

    run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    for call in provider.calls:
        for message in call["request"]["messages"]:
            blob = json.dumps(message)
            assert "cache_control" not in blob


def test_empty_system_prompt_omits_the_system_key_entirely():
    provider = ScriptedProvider([msg_text("done")])

    run_episode(
        make_config(system_prompt=""), make_case(), provider, StubBackend(), rep=0, seed=0
    )

    assert "system" not in provider.calls[0]["request"]


def test_empty_tool_list_gets_no_cache_breakpoint():
    provider = ScriptedProvider([msg_text("done")])

    run_episode(make_config(tools=[]), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert provider.calls[0]["request"]["tools"] == []


def test_build_request_marks_a_fresh_copy_not_the_agents_own_tools():
    tools = copy.deepcopy(TOOLS)
    build_request("messages", "claude-fable-5", {}, tools, [], system="hi")

    assert tools == TOOLS  # the config's tool list is never mutated


def test_max_tokens_comes_from_params_when_present():
    provider = ScriptedProvider([msg_text("done")])
    config = make_config(params={"max_tokens": 1024})

    run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert provider.calls[0]["request"]["max_tokens"] == 1024


def test_reasoning_effort_becomes_output_config_effort():
    assert map_params("messages", {"reasoning_effort": "xhigh"}) == {
        "output_config": {"effort": "xhigh"}
    }
    # an explicit output_config is more specific and wins
    assert map_params(
        "messages", {"reasoning_effort": "low", "output_config": {"effort": "max"}}
    ) == {"output_config": {"effort": "max"}}


def test_thinking_passes_through():
    assert map_params("messages", {"thinking": {"type": "enabled"}}) == {
        "thinking": {"type": "enabled"}
    }


# ---------------------------------------------------------------------------
# Sampling params: top-level on an SDK that takes them, `extra_body` on one that
# does not (anthropic >= 1.1.0 removed them from Messages.create).
# ---------------------------------------------------------------------------


@pytest.fixture
def sdk_takes_sampling_params(monkeypatch):
    """Pretend the installed SDK still lists temperature/top_p/top_k (anthropic < 1.1.0)."""
    monkeypatch.setattr(agent_loop, "messages_create_accepts", lambda name: True)


@pytest.fixture
def sdk_dropped_sampling_params(monkeypatch):
    """Pretend the installed SDK dropped them (anthropic >= 1.1.0), whatever is installed."""
    monkeypatch.setattr(
        agent_loop, "messages_create_accepts", lambda name: name not in SAMPLING_PARAMS
    )


def test_sampling_params_stay_top_level_on_an_sdk_that_accepts_them(sdk_takes_sampling_params):
    assert map_params("messages", {"temperature": 0.4, "top_p": 0.9, "top_k": 20}) == {
        "temperature": 0.4,
        "top_p": 0.9,
        "top_k": 20,
    }


def test_sampling_params_are_routed_into_extra_body_when_the_sdk_dropped_them(
    sdk_dropped_sampling_params,
):
    assert map_params("messages", {"temperature": 0.4, "top_p": 0.9, "top_k": 20}) == {
        "extra_body": {"temperature": 0.4, "top_p": 0.9, "top_k": 20}
    }


def test_routing_leaves_the_other_endpoints_and_the_other_params_alone(
    sdk_dropped_sampling_params,
):
    assert map_params("chat_completions", {"temperature": 0.4}) == {"temperature": 0.4}
    assert map_params("responses", {"top_p": 0.9}) == {"top_p": 0.9}
    assert map_params("messages", {"max_tokens": 32, "temperature": 0.4}) == {
        "max_tokens": 32,
        "extra_body": {"temperature": 0.4},
    }


def test_an_explicit_extra_body_is_merged_and_wins_over_the_canonical_param(
    sdk_dropped_sampling_params,
):
    """An agent that already writes the escape hatch by hand keeps its own value — the same
    rule `output_config.effort` follows — and its other extra_body keys survive."""
    assert map_params(
        "messages",
        {"temperature": 0.4, "top_k": 20, "extra_body": {"temperature": 0.9, "beta": True}},
    ) == {"extra_body": {"temperature": 0.9, "beta": True, "top_k": 20}}


def test_an_explicit_extra_body_is_left_alone_on_an_older_sdk(sdk_takes_sampling_params):
    assert map_params("messages", {"extra_body": {"temperature": 0.9}}) == {
        "extra_body": {"temperature": 0.9}
    }


def test_the_installed_sdk_is_what_decides(monkeypatch):
    """No monkeypatching: whichever anthropic is installed, the built request must put the
    param where THAT SDK's Messages.create will take it."""
    import inspect as _inspect

    from anthropic.resources.messages import Messages

    accepted = "temperature" in _inspect.signature(Messages.create).parameters
    request = build_request("messages", "claude-fable-5", {"temperature": 0.2}, [], [])

    if accepted:
        assert request["temperature"] == 0.2
        assert "extra_body" not in request
    else:
        assert request["extra_body"] == {"temperature": 0.2}
        assert "temperature" not in request


def test_the_recorded_request_shows_the_param_where_it_was_actually_sent(
    sdk_dropped_sampling_params,
):
    """The recorder writes the request the loop built, and the provider sends it verbatim, so
    the two must agree — a record that said `temperature` top-level while the wire carried it
    in extra_body would be a lie about what was measured."""
    provider = ScriptedProvider([msg_text("done")])

    result = run_episode(
        make_config(params={"temperature": 0.2}), make_case(), provider, StubBackend(),
        rep=0, seed=0,
    )

    recorded = result.api_calls[0].request
    assert recorded["extra_body"] == {"temperature": 0.2}
    assert "temperature" not in recorded
    assert provider.calls[0]["request"] == recorded


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        ("required", {"type": "any"}),
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
        ("any", {"type": "any"}),
        ({"type": "function", "function": {"name": "book_flight"}},
         {"type": "tool", "name": "book_flight"}),
        ({"type": "tool", "name": "book_flight"}, {"type": "tool", "name": "book_flight"}),
        ({"type": "any", "disable_parallel_tool_use": True},
         {"type": "any", "disable_parallel_tool_use": True}),
    ],
)
def test_tool_choice_translation(canonical, expected):
    assert map_params("messages", {"tool_choice": canonical})["tool_choice"] == expected


def test_other_endpoints_are_untouched_by_the_messages_mapping():
    assert map_params("chat_completions", {"reasoning_effort": "low", "tool_choice": "required"}) \
        == {"reasoning_effort": "low", "tool_choice": "required"}
    assert map_params("responses", {"reasoning_effort": "low"}) == {"reasoning": {"effort": "low"}}


def test_build_request_rejects_an_unknown_endpoint():
    with pytest.raises(ValueError, match="unknown endpoint"):
        build_request("telepathy", "m", {}, [], [])


# ---------------------------------------------------------------------------
# Conversation replay
# ---------------------------------------------------------------------------


def test_assistant_turn_is_replayed_with_every_block_including_thinking():
    provider = ScriptedProvider(
        [
            msg_tools([("search_flights", {"origin": "SFO"})], thinking="let me look"),
            msg_text("all set"),
        ]
    )

    run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    second = provider.calls[1]["request"]["messages"]
    assert second[1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "let me look", "signature": "sig-abc"},
            {"type": "tool_use", "id": "toolu_0", "name": "search_flights",
             "input": {"origin": "SFO"}},
        ],
    }
    assert second[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_0",
                "content": json.dumps({"ok": True, "tool": "search_flights", "seq": 1}),
            }
        ],
    }


def test_parallel_tool_results_share_one_user_message_in_order():
    provider = ScriptedProvider(
        [
            msg_tools([("search_flights", {"origin": "SFO"}), ("book_flight", {"flight_id": "F"})]),
            msg_text("done"),
        ]
    )

    run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    messages = provider.calls[1]["request"]["messages"]
    assert len(messages) == 3  # user, assistant, one user turn carrying both results
    blocks = messages[2]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["toolu_0", "toolu_1"]
    assert all(b["type"] == "tool_result" for b in blocks)


def test_tool_result_is_error_only_when_the_backend_returned_an_error():
    provider = ScriptedProvider(
        [msg_tools([("book_flight", {"flight_id": "nope"})]), msg_text("sorry")]
    )
    backend = StubBackend(results={"book_flight": {"error": "no such flight"}})

    run_episode(make_config(), make_case(), provider, backend, rep=0, seed=0)

    block = provider.calls[1]["request"]["messages"][2]["content"][0]
    assert block["is_error"] is True
    assert block["content"] == json.dumps({"error": "no such flight"})


def test_follow_up_segments_keep_the_full_assistant_content_and_never_edit_history():
    provider = ScriptedProvider([msg_text("first answer"), msg_text("second answer")])
    case = make_case(user_messages=["one", "two"])

    result = run_episode(make_config(), case, provider, StubBackend(), rep=0, seed=0)

    first = provider.calls[0]["request"]["messages"]
    second = provider.calls[1]["request"]["messages"]
    assert first == [{"role": "user", "content": "one"}]
    assert second[0] == first[0]  # earlier turns are never rewritten
    assert second[1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "first answer"}],
    }
    assert second[2] == {"role": "user", "content": "two"}
    assert result.final_message == "second answer"


# ---------------------------------------------------------------------------
# Response parsing / usage
# ---------------------------------------------------------------------------


def test_tool_use_blocks_become_calls_with_dict_arguments():
    provider = ScriptedProvider(
        [msg_tools([("search_flights", {"origin": "SFO"})]), msg_text("done")]
    )
    backend = StubBackend()

    result = run_episode(make_config(), make_case(), provider, backend, rep=0, seed=0)

    assert backend.executed == [("search_flights", {"origin": "SFO"})]
    assert result.tool_executions[0].arguments == {"origin": "SFO"}
    assert result.stop_reason == "end_turn"
    assert result.resolved_model == "claude-fable-5"


def test_text_blocks_concatenate_and_thinking_is_not_part_of_the_final_message():
    thinking = {"type": "redacted_thinking", "data": "opaque"}
    provider = ScriptedProvider([msg_text("hello", extra_blocks=(thinking,))])

    result = run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert result.final_message == "hello"


def test_stop_reason_of_a_tool_turn_is_recorded_when_the_episode_ends_there():
    provider = ScriptedProvider([msg_tools([("search_flights", {})])])
    config = make_config(max_turns=1)

    result = run_episode(config, make_case(), provider, StubBackend(), rep=0, seed=0)

    assert result.stop_reason == "tool_use"


def test_usage_accumulates_with_both_cache_fields():
    provider = ScriptedProvider(
        [
            msg_tools(
                [("search_flights", {})],
                usage={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 7,
                },
            ),
            msg_text(
                "done",
                usage={
                    "input_tokens": 50,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 0,
                },
            ),
        ]
    )

    result = run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    # input_tokens is total billable input: Anthropic reports it excluding cache reads
    # (100 + 50), so the 60 read-from-cache tokens are folded in to match the convention
    # pricing.py and the OpenAI endpoints use.
    assert result.usage == {
        "input_tokens": 210,
        "output_tokens": 15,
        "cached_input_tokens": 60,
        "cache_creation_input_tokens": 7,
    }


def test_openai_endpoints_keep_their_three_key_usage_dict():
    provider = ScriptedProvider(
        [
            {
                "id": "c",
                "model": "gpt-5.5",
                "choices": [
                    {"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        ]
    )

    result = run_episode(
        make_config(endpoint="chat_completions", model="gpt-5.5"),
        make_case(),
        provider,
        StubBackend(),
        rep=0,
        seed=0,
    )

    assert result.usage == {"input_tokens": 3, "output_tokens": 1, "cached_input_tokens": 0}
    assert result.stop_reason is None


def test_api_error_ends_the_episode_and_is_recorded():
    provider = ScriptedProvider(
        [ProviderAPIError(message="bad tool_choice", status_code=400, error_type="api_status_error")]
    )

    result = run_episode(make_config(), make_case(), provider, StubBackend(), rep=0, seed=0)

    assert result.api_error == {
        "status_code": 400,
        "message": "bad tool_choice",
        "type": "api_status_error",
    }
    assert result.api_calls[0].response is None


# ---------------------------------------------------------------------------
# The provider (fake client, no network)
# ---------------------------------------------------------------------------


class _Dumped:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self, mode="json"):
        return dict(self._payload)


class FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.seen = None

    def create(self, **request):
        self.seen = request
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _Dumped(self.outcome)


class FakeModels:
    def __init__(self, outcome):
        self.outcome = outcome
        self.retrieved = []

    def retrieve(self, model_id):
        self.retrieved.append(model_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _Dumped({"id": model_id, **self.outcome})


class FakeClient:
    def __init__(self, messages_outcome=None, models_outcome=None):
        self.messages = FakeMessages(messages_outcome or {"id": "msg_1", "content": []})
        self.models = FakeModels(models_outcome or {})


def make_provider(client):
    provider = AnthropicProvider()
    provider._client = client
    return provider


def status_error(message, status_code=400):
    import anthropic

    exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
    exc.status_code = status_code
    exc.body = {"error": {"type": "invalid_request_error", "message": message}}
    exc.message = "wrapper text that must not be shown"
    return exc


def test_provider_is_registered_and_named():
    assert get_provider("anthropic").name == "anthropic"


def test_provider_sends_the_request_unchanged_and_returns_a_plain_dict():
    client = FakeClient(messages_outcome={"id": "msg_1", "content": [], "stop_reason": "end_turn"})
    provider = make_provider(client)
    request = {"model": "claude-fable-5", "max_tokens": 8192, "messages": []}

    response = provider.call("messages", request, "case:0:0")

    assert response == {"id": "msg_1", "content": [], "stop_reason": "end_turn"}
    assert client.messages.seen == request  # nothing injected
    assert request == {"model": "claude-fable-5", "max_tokens": 8192, "messages": []}


def test_provider_rejects_the_openai_endpoints():
    provider = make_provider(FakeClient())
    for endpoint in ("chat_completions", "responses"):
        with pytest.raises(ValueError, match="only serves"):
            provider.call(endpoint, {}, "k")


def test_missing_key_is_a_provider_error_naming_the_variable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderAPIError) as excinfo:
        AnthropicProvider().call("messages", {}, "k")
    assert "ANTHROPIC_API_KEY" in excinfo.value.message
    assert excinfo.value.error_type == "missing_api_key"


def test_status_error_keeps_the_api_message_verbatim():
    message = 'tool_choice: type "tool" and "any" are not supported for this model.'
    provider = make_provider(FakeClient(messages_outcome=status_error(message)))

    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("messages", {"model": "claude-fable-5-1"}, "k")

    assert excinfo.value.message == message
    assert excinfo.value.status_code == 400
    assert excinfo.value.error_type == "api_status_error"


def test_generic_api_error_and_network_error_mapping():
    import anthropic

    api_error = anthropic.APIError.__new__(anthropic.APIError)
    api_error.message = "connection reset by peer"
    provider = make_provider(FakeClient(messages_outcome=api_error))
    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("messages", {}, "k")
    assert excinfo.value.error_type == "api_error"
    assert excinfo.value.message == "connection reset by peer"

    provider = make_provider(FakeClient(messages_outcome=TimeoutError("timed out")))
    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("messages", {}, "k")
    assert excinfo.value.error_type == "network_error"
    assert "timed out" in excinfo.value.message


def test_preflight_models_returns_capabilities_per_id():
    client = FakeClient(models_outcome={"capabilities": {"effort": {"levels": ["low", "max"]}}})
    provider = make_provider(client)

    result = provider.preflight_models(["claude-fable-5", "claude-fable-5-1"])

    assert client.models.retrieved == ["claude-fable-5", "claude-fable-5-1"]
    assert result["claude-fable-5"] == {"effort": {"levels": ["low", "max"]}}


def test_preflight_models_maps_a_404():
    provider = make_provider(FakeClient(models_outcome=status_error("model not found", 404)))

    with pytest.raises(ProviderAPIError) as excinfo:
        provider.preflight_models(["claude-fable-9"])

    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_fable_rates_are_ten_and_fifty():
    # 1M in + 0.1M out: 10 + 5 = 15
    assert abs(price("anthropic", "claude-fable-5", 1_000_000, 100_000, 0) - 15.0) < 1e-9
    assert abs(price("anthropic", "claude-fable-5-1", 1_000_000, 100_000, 0) - 15.0) < 1e-9


def test_cache_read_fraction_differs_between_the_two_fables():
    five = price("anthropic", "claude-fable-5", 1_000_000, 0, 1_000_000)
    five_one = price("anthropic", "claude-fable-5-1", 1_000_000, 0, 1_000_000)
    assert abs(five - 1.0) < 1e-9  # 10 * 0.1
    assert abs(five_one - 0.25) < 1e-9  # 10 * 0.025
    assert five_one < five


def test_fable_5_1_snapshot_ids_do_not_fall_back_to_fable_5():
    assert abs(price("anthropic", "claude-fable-5-1-20260901", 1_000_000, 0, 1_000_000)
               - 0.25) < 1e-9


def test_cache_writes_bill_at_one_and_a_quarter_the_input_rate():
    # 1M cache-write tokens on a $10/MTok model = $12.50, on top of the input total
    assert abs(price("anthropic", "claude-fable-5", 0, 0, 0, 1_000_000) - 12.50) < 1e-9
    assert abs(price("anthropic", "claude-fable-5-1", 0, 0, 0, 1_000_000) - 12.50) < 1e-9


def test_a_cached_episode_prices_reads_writes_and_fresh_input_together():
    # 1M input of which 800k came from cache, 200k written to cache, 100k output, on 5.1:
    #   200k fresh   * $10/MTok             = $2.00
    #   800k read    * $10 * 0.025 /MTok    = $0.20
    #   200k written * $10 * 1.25 /MTok     = $2.50
    #   100k output  * $50/MTok             = $5.00
    usd = price("anthropic", "claude-fable-5-1", 1_000_000, 100_000, 800_000, 200_000)
    assert abs(usd - 9.70) < 1e-9


def test_cache_creation_defaults_to_zero_so_openai_pricing_is_unchanged():
    assert price("openai", "gpt-5.5", 1_000_000, 100_000, 0) == price(
        "openai", "gpt-5.5", 1_000_000, 100_000, 0, 0
    )


def test_sim_fable_models_still_cost_nothing():
    assert price("sim", "sim-fable-5-1", 10_000, 10_000, 0) == 0.0


# ---------------------------------------------------------------------------
# CLI guards
# ---------------------------------------------------------------------------


def error_of(fn, *args):
    with pytest.raises(ValueError) as excinfo:
        fn(*args)
    return str(excinfo.value)


def test_claude_model_requires_the_anthropic_provider():
    assert "--provider anthropic" in error_of(cli._check_models, "openai", ["claude-fable-5"])


def test_gpt_model_requires_the_openai_provider():
    assert "--provider openai" in error_of(cli._check_models, "anthropic", ["gpt-5.5"])


def test_sim_models_require_the_sim_provider():
    assert "--provider sim" in error_of(cli._check_models, "anthropic", ["sim-fable-5-1"])


def test_sim_provider_rejects_a_claude_model_and_names_the_fable_sims():
    message = error_of(cli._check_models, "sim", ["claude-fable-5"])
    assert cli.SIM_FABLE_BASELINE_MODEL in message and cli.SIM_FABLE_CANDIDATE_MODEL in message


def test_matching_pairs_pass():
    cli._check_models("anthropic", ["claude-fable-5", "claude-fable-5-1"])
    cli._check_models("openai", ["gpt-5.5", "gpt-5.6-sol"])
    cli._check_models("sim", [cli.SIM_FABLE_BASELINE_MODEL, cli.SIM_FABLE_CANDIDATE_MODEL])


def test_batch_and_flex_are_openai_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    args = type("A", (), {"provider": "anthropic", "batch": False, "flex": True})()
    assert "--provider openai" in error_of(cli._make_provider, args)


def test_anthropic_provider_requires_a_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # away from any .env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    args = type("A", (), {"provider": "anthropic", "batch": False, "flex": False})()
    message = error_of(cli._make_provider, args)
    assert "ANTHROPIC_API_KEY" in message and ".env" in message


def test_dotenv_feeds_the_anthropic_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
    monkeypatch.chdir(tmp_path)

    cli._load_dotenv()

    assert cli.os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"
    args = type("A", (), {"provider": "anthropic", "batch": False, "flex": False})()
    assert cli._make_provider(args).name == "anthropic"


# ---------------------------------------------------------------------------
# CLI preflight
# ---------------------------------------------------------------------------


class PreflightProvider:
    name = "anthropic"

    def __init__(self, outcome):
        self.outcome = outcome
        self.asked = None

    def preflight_models(self, ids):
        self.asked = list(ids)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_preflight_prints_a_line_per_model_and_returns_manifest_notes(capsys):
    provider = PreflightProvider(
        {
            "claude-fable-5": {"effort": {"levels": ["low", "medium", "high", "xhigh", "max"]}},
            "claude-fable-5-1": {"effort": ["low", "max"]},
        }
    )

    notes = cli.anthropic_preflight(provider, ["claude-fable-5", "claude-fable-5-1", None])

    out = capsys.readouterr().out
    assert "claude-fable-5: ok, effort levels low…max" in out
    assert provider.asked == ["claude-fable-5", "claude-fable-5-1"]
    assert "claude-fable-5: low,medium,high,xhigh,max" in notes


def test_preflight_is_skipped_for_other_providers():
    assert cli.anthropic_preflight(get_provider("sim"), ["sim-fable-5"]) == ""


def test_preflight_404_is_a_clean_exit_two_message():
    provider = PreflightProvider(
        ProviderAPIError(message="model: claude-fable-9", status_code=404)
    )
    message = error_of(cli.anthropic_preflight, provider, ["claude-fable-9"])
    assert "model id not found for this ANTHROPIC_API_KEY" in message


def test_preflight_other_errors_propagate_as_api_errors():
    provider = PreflightProvider(ProviderAPIError(message="server down", status_code=503))
    with pytest.raises(ProviderAPIError):
        cli.anthropic_preflight(provider, ["claude-fable-5"])


# ---------------------------------------------------------------------------
# SDK-side parameter rejection (anthropic >= 1.3.0 removed temperature/top_p/top_k
# from Messages.create, so the documented 400 never reaches the wire)
# ---------------------------------------------------------------------------


def removed_kwarg_type_error(name: str) -> TypeError:
    """What anthropic 1.3.0 raises from `_utils._utils.wrapper` for a dropped param."""
    return TypeError(f"Messages.create() got an unexpected keyword argument '{name}'")


@pytest.mark.parametrize("param", ["temperature", "top_p", "top_k"])
def test_sdk_removed_sampling_kwarg_is_a_400_the_differ_calls_a_sampling_break(param):
    provider = make_provider(FakeClient(messages_outcome=removed_kwarg_type_error(param)))

    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("messages", {"model": "claude-fable-5", param: 0}, "k")

    exc = excinfo.value
    assert exc.status_code == 400
    assert f"unexpected keyword argument '{param}'" in exc.message
    assert (
        differ._api_error_signature(exc.to_dict())
        == differ.SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS
    )


def test_a_type_error_that_is_not_an_unexpected_kwarg_is_not_swallowed():
    provider = make_provider(FakeClient(messages_outcome=TypeError("unhashable type: 'dict'")))

    with pytest.raises(TypeError, match="unhashable type"):
        provider.call("messages", {"model": "claude-fable-5"}, "k")


def test_the_episode_records_the_sdk_rejection_instead_of_dying():
    """The forced outcome stands in for ANY parameter the installed SDK refuses: since the
    sampling params are routed through `extra_body`, `temperature` is no longer one of them
    (see the strict-client tests below), but the recording path must still hold."""
    provider = make_provider(FakeClient(messages_outcome=removed_kwarg_type_error("temperature")))

    result = run_episode(
        make_config(params={}), make_case(), provider, StubBackend(), rep=0, seed=0,
    )

    assert result.api_error["status_code"] == 400
    assert "temperature" in result.api_error["message"]
    assert result.api_calls[0].response is None


def test_the_whole_suite_survives_it_and_records_failing_reps(tmp_path):
    """The bug that motivated this: an uncaught TypeError propagated out of the thread pool
    and killed run_suite, so a run produced no reps and no diff at all."""
    provider = make_provider(FakeClient(messages_outcome=removed_kwarg_type_error("temperature")))
    runs_root = tmp_path / "runs"

    run_dir = run_suite(
        TODO_AGENT, provider, "sdk-sampling-break", n_reps=2,
        model_override="claude-fable-5", endpoint_override="messages",
        params_override={}, runs_root=runs_root, workers=1,
    )

    case_ids = sorted(d.name for d in (run_dir / "cases").iterdir() if d.is_dir())
    assert case_ids
    for case_id in case_ids:
        reps = load_case_reps(run_dir, case_id)
        assert len(reps) == 2
        assert all(not r.passed for r in reps)
        assert all(r.api_error["status_code"] == 400 for r in reps)
        assert failure_signatures(reps) == [differ.SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS]


# ---------------------------------------------------------------------------
# ... and why `temperature` is no longer one of those refused parameters
# ---------------------------------------------------------------------------


class StrictFakeMessages(FakeMessages):
    """A `messages` resource with anthropic 1.3.0's real keyword set: no sampling params, no
    **kwargs, `extra_body` present. Anything else raises the SDK's own TypeError."""

    ACCEPTED = frozenset(
        {
            "max_tokens", "messages", "model", "cache_control", "container", "inference_geo",
            "metadata", "output_config", "service_tier", "stop_sequences", "stream", "system",
            "thinking", "tool_choice", "tools", "user_profile_id", "extra_headers",
            "extra_query", "extra_body", "timeout",
        }
    )

    def create(self, **request):
        for name in request:
            if name not in self.ACCEPTED:
                raise removed_kwarg_type_error(name)
        return super().create(**request)


def strict_provider():
    return make_provider(FakeClient(messages_outcome={"id": "msg_1", "content": []}))


def test_temperature_reaches_an_sdk_that_dropped_it_instead_of_raising(
    sdk_dropped_sampling_params,
):
    """The A-052 mapping made the in-process TypeError a recorded failure, but it fired on
    BOTH models and left the signature undecidable. Routed through extra_body the request now
    reaches the wire, so the API's own answer is what the run records."""
    client = FakeClient()
    client.messages = StrictFakeMessages({"id": "msg_1", "content": [], "stop_reason": "end_turn"})
    provider = make_provider(client)

    result = run_episode(
        make_config(params={"temperature": 0.2, "top_p": 0.9, "top_k": 5}),
        make_case(), provider, StubBackend(), rep=0, seed=0,
    )

    assert result.api_error is None
    assert client.messages.seen["extra_body"] == {"temperature": 0.2, "top_p": 0.9, "top_k": 5}
    for name in SAMPLING_PARAMS:
        assert name not in client.messages.seen


def test_another_unexpected_kwarg_still_maps_to_the_documented_400():
    """The A-052 mapping is kept for every other parameter a future SDK removes."""
    client = FakeClient()
    client.messages = StrictFakeMessages({"id": "msg_1", "content": []})
    provider = make_provider(client)

    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("messages", {"model": "claude-fable-5", "top_z": 1}, "k")

    assert excinfo.value.status_code == 400
    assert "unexpected keyword argument 'top_z'" in excinfo.value.message


def test_the_sdk_probe_reads_the_real_signature():
    assert messages_create_accepts("model") is True
    assert messages_create_accepts("extra_body") is True
    assert messages_create_accepts("definitely_not_a_messages_param") is False

