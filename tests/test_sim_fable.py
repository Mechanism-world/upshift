"""sim-fable-5 / sim-fable-5-1: the Messages wire shape, the documented 5 -> 5.1 changes,
and a full run_suite over the packaged example agent switched to endpoint `messages`.

Offline and deterministic — no key, no network, no cost.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift import recorder
from upshift.agent_loop import run_episode
from upshift.providers.base import ProviderAPIError
from upshift.providers.sim import FORCED_TOOL_CHOICE_MESSAGE, SimProvider
from upshift.runner import run_suite
from upshift.schemas import AgentConfig, Case

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_AGENT = ROOT / "src" / "upshift" / "example_agent"

SYSTEM_PROMPT = "You are a flight booking assistant. Help the user book travel."

PLAN = [
    {"tool_calls": [{"name": "search_flights", "arguments": {"origin": "SFO"}}]},
    {"tool_calls": [{"name": "book_flight",
                     "arguments": {"flight_id": "$ref:search_flights[0].flights[0].id"}}]},
    {"final_message": "All set: {ref:book_flight[0].confirmation_id}."},
]

# One step with two independent calls: the shape the `serialize` corruption acts on.
PARALLEL_PLAN = [
    {
        "tool_calls": [
            {"name": "search_flights", "arguments": {"origin": "SFO"}},
            {"name": "search_hotels", "arguments": {"city": "NYC"}},
        ]
    },
    {"final_message": "Both done."},
]

BATCHING_SENTENCE = (
    "First privately list what you need next; then request every item that doesn't depend "
    "on another's result in this one response."
)
VERIFICATION_NUDGE = "Always search before answering a question about a specific name."


def make_tools(names=("search_flights", "book_flight")):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


class StubBackend:
    def __init__(self):
        self.executions = []
        self._seq = 0

    def execute(self, name, arguments):
        self.executions.append((name, dict(arguments)))
        if name.startswith("search"):
            return {"flights": [{"id": "F1"}], "hotels": [{"id": "H1"}]}
        if name == "book_flight":
            self._seq += 1
            return {"confirmation_id": f"UPS-{self._seq}"}
        return {"error": f"unknown tool {name}"}

    def state(self):
        return {"calls": [name for name, _ in self.executions]}

    def names(self):
        return [name for name, _ in self.executions]


def make_config(model="sim-fable-5", params=None, system_prompt=SYSTEM_PROMPT, tools=None):
    return AgentConfig(
        name="booking_agent",
        endpoint="messages",
        model=model,
        params=params or {},
        system_prompt=system_prompt,
        tools=tools or make_tools(),
        max_turns=12,
        agent_dir="/nonexistent/agent",
    )


def make_case(sim=None, case_id="book-sfo"):
    return Case(
        id=case_id,
        description="",
        initial_state={},
        user_messages=["Book me a flight from SFO."],
        checks=[],
        sim=sim if sim is not None else {"oracle_plan": PLAN},
    )


def run(model="sim-fable-5", case=None, provider=None, rep=0, **config_kwargs):
    backend = StubBackend()
    result = run_episode(
        make_config(model=model, **config_kwargs),
        case or make_case(),
        provider or SimProvider(),
        backend,
        rep=rep,
        seed=rep,
    )
    return result, backend


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


def test_sim_fable_5_replays_the_plan_in_the_messages_shape():
    result, backend = run()

    assert result.api_error is None
    assert backend.names() == ["search_flights", "book_flight"]
    assert result.final_message == "All set: UPS-1."
    assert result.stop_reason == "end_turn"

    first = result.api_calls[0].response
    assert first["type"] == "message" and first["role"] == "assistant"
    assert first["content"][0]["type"] == "tool_use"
    assert first["stop_reason"] == "tool_use"
    assert set(first["usage"]) == {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
    last = result.api_calls[-1].response
    assert last["content"] == [{"type": "text", "text": "All set: UPS-1."}]


def test_tool_results_are_read_back_out_of_tool_result_blocks():
    result, _ = run()

    # the book call resolved $ref against the search result carried in a tool_result block
    booked = result.api_calls[1].response["content"][0]["input"]
    assert booked == {"flight_id": "F1"}


def test_fable_models_are_only_served_on_the_messages_endpoint():
    provider = SimProvider()
    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("chat_completions", {"model": "sim-fable-5"}, "c:0:0", {"sim": {}})
    assert "messages" in excinfo.value.message


def test_openai_sims_are_not_served_on_the_messages_endpoint():
    provider = SimProvider()
    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("messages", {"model": "sim-5.5"}, "c:0:0", {"sim": {}})
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Forced tool_choice -> the documented 400 (5.1 only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "book_flight"}}])
def test_forced_tool_choice_is_a_400_on_fable_5_1(choice):
    result, backend = run(model="sim-fable-5-1", params={"tool_choice": choice})

    assert result.api_error == {
        "status_code": 400,
        "message": FORCED_TOOL_CHOICE_MESSAGE,
        "type": "invalid_request_error",
    }
    assert backend.names() == []


def test_the_same_forced_tool_choice_is_fine_on_fable_5():
    result, backend = run(model="sim-fable-5", params={"tool_choice": "required"})

    assert result.api_error is None
    assert backend.names() == ["search_flights", "book_flight"]


def test_auto_and_none_tool_choice_do_not_fire_the_400():
    for choice in ("auto", "none"):
        result, _ = run(model="sim-fable-5-1", params={"tool_choice": choice},
                        system_prompt=SYSTEM_PROMPT + " " + VERIFICATION_NUDGE)
        assert result.api_error is None


# ---------------------------------------------------------------------------
# serialize
# ---------------------------------------------------------------------------


def parallel_case():
    return make_case(sim={"oracle_plan": PARALLEL_PLAN, "retrieval_tools": []},
                     case_id="parallel")


def test_fable_5_batches_both_calls_in_one_turn():
    result, backend = run(case=parallel_case(), tools=make_tools(("search_flights",
                                                                 "search_hotels")))

    assert len(result.api_calls) == 2  # one tool turn, one text turn
    assert len(result.api_calls[0].response["content"]) == 2
    assert backend.names() == ["search_flights", "search_hotels"]


def test_fable_5_1_serializes_the_step_into_one_call_per_turn():
    result, backend = run(
        model="sim-fable-5-1",
        case=parallel_case(),
        tools=make_tools(("search_flights", "search_hotels")),
    )

    assert [len(c.response["content"]) for c in result.api_calls] == [1, 1, 1]
    assert backend.names() == ["search_flights", "search_hotels"]
    assert result.final_message == "Both done."


def test_the_batching_sentence_suppresses_serialization():
    result, backend = run(
        model="sim-fable-5-1",
        case=parallel_case(),
        tools=make_tools(("search_flights", "search_hotels")),
        system_prompt=SYSTEM_PROMPT + " " + BATCHING_SENTENCE,
    )

    assert len(result.api_calls) == 2
    assert len(result.api_calls[0].response["content"]) == 2
    assert backend.names() == ["search_flights", "search_hotels"]


# ---------------------------------------------------------------------------
# skip_retrieval
# ---------------------------------------------------------------------------


def test_fable_5_1_drops_retrieval_calls_at_default_effort():
    result, backend = run(model="sim-fable-5-1")

    assert backend.names() == ["book_flight"]  # search_flights never happened
    assert result.api_error is None


def test_fable_5_keeps_them():
    _, backend = run(model="sim-fable-5")
    assert backend.names() == ["search_flights", "book_flight"]


def test_retrieval_tools_can_be_declared_per_case():
    case = make_case(sim={"oracle_plan": PLAN, "retrieval_tools": ["book_flight"]})
    _, backend = run(model="sim-fable-5-1", case=case)

    # the declared list wins over the name heuristic: search survives, book is dropped
    assert backend.names() == ["search_flights"]


def test_the_verification_nudge_suppresses_the_drop():
    _, backend = run(
        model="sim-fable-5-1", system_prompt=SYSTEM_PROMPT + " " + VERIFICATION_NUDGE
    )
    assert backend.names() == ["search_flights", "book_flight"]


@pytest.mark.parametrize(("effort", "expected"), [
    ("low", ["book_flight"]),
    ("medium", ["book_flight"]),
    ("high", ["book_flight"]),
    ("xhigh", ["search_flights", "book_flight"]),
    ("max", ["search_flights", "book_flight"]),
])
def test_effort_xhigh_and_above_suppress_the_drop(effort, expected):
    _, backend = run(model="sim-fable-5-1", params={"reasoning_effort": effort})
    assert backend.names() == expected


def test_determinism_across_identical_runs():
    first, backend_a = run(model="sim-fable-5-1", rep=2)
    second, backend_b = run(model="sim-fable-5-1", rep=2)

    assert backend_a.names() == backend_b.names()
    assert [json.dumps(c.request, sort_keys=True) for c in first.api_calls] == [
        json.dumps(c.request, sort_keys=True) for c in second.api_calls
    ]


# ---------------------------------------------------------------------------
# Full suite over the packaged example agent
# ---------------------------------------------------------------------------


def summary_of(run_directory: Path) -> dict:
    return json.loads((run_directory / "summary.json").read_text())


def example_case_ids(deterministic_only=False):
    """Case ids of the packaged example agent. Two cases declare `sim.flaky`, whose whole
    point is to fail some reps; a single-rep assertion excludes them (as the CLI e2e test
    does) rather than pretending the sim is deterministic where the case says it is not."""
    cases = json.loads((EXAMPLE_AGENT / "cases" / "cases.json").read_text())
    return [
        c["id"]
        for c in cases
        if not (deterministic_only and (c.get("sim") or {}).get("flaky"))
    ]


def test_full_suite_on_sim_fable_5_passes_every_case(tmp_path):
    case_ids = example_case_ids(deterministic_only=True)
    assert len(case_ids) == 36 and len(example_case_ids()) == 38

    run_directory = run_suite(
        EXAMPLE_AGENT,
        SimProvider(),
        "fable5-messages",
        n_reps=1,
        model_override="sim-fable-5",
        endpoint_override="messages",
        case_ids=case_ids,
        runs_root=tmp_path,
        workers=4,
    )

    summary = summary_of(run_directory)
    assert sorted(summary) == sorted(case_ids)
    failing = [case_id for case_id, s in summary.items() if s["passes"] != s["n"]]
    assert failing == []
    manifest = json.loads((run_directory / "manifest.json").read_text())
    assert manifest["agent"]["endpoint"] == "messages"


def test_full_suite_on_sim_fable_5_1_with_a_forced_tool_choice_400s_everywhere(tmp_path):
    run_directory = run_suite(
        EXAMPLE_AGENT,
        SimProvider(),
        "fable51-forced",
        n_reps=1,
        model_override="sim-fable-5-1",
        endpoint_override="messages",
        params_override={"tool_choice": "any"},
        runs_root=tmp_path,
        workers=4,
    )

    summary = summary_of(run_directory)
    assert all(s["passes"] == 0 for s in summary.values())
    records = recorder.load_case_reps(run_directory, next(iter(summary)))
    assert records[0].api_error["status_code"] == 400
    assert records[0].api_error["message"] == FORCED_TOOL_CHOICE_MESSAGE
