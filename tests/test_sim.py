"""Sim-provider tests. Cases are built inline and the backend is a stub, so these depend on
nothing but agent_loop + sim (no victim agent, no checks engine)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from upshift.agent_loop import run_episode
from upshift.providers.sim import SimProvider
from upshift.schemas import AgentConfig, Case

SYSTEM_PROMPT = "You are a flight booking assistant. Help the user book travel."

PLAN = [
    {
        "tool_calls": [
            {"name": "search_flights", "arguments": {"origin": "SFO", "destination": "JFK"}}
        ]
    },
    {
        "tool_calls": [
            {
                "name": "book_flight",
                "arguments": {"flight_id": "$ref:search_flights[0].flights[0].id"},
            }
        ]
    },
    {"final_message": "All set. Your confirmation number is {ref:book_flight[0].confirmation_id}."},
]


def make_tools(book_description="Book a flight."):
    return [
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
                "description": book_description,
                "parameters": {"type": "object", "properties": {"flight_id": {"type": "string"}}},
            },
        },
    ]


class StubBookingBackend:
    """Minimal deterministic stand-in for the real booking backend."""

    def __init__(self):
        self.executions = []
        self.bookings = {}
        self._seq = 0

    def execute(self, name, arguments):
        self.executions.append((name, dict(arguments)))
        if name == "search_flights":
            return {"flights": [{"id": "F1", "price": 250}, {"id": "F2", "price": 310}]}
        if name == "book_flight":
            self._seq += 1
            confirmation = f"UPS-{self._seq}"
            self.bookings[confirmation] = dict(arguments)
            return {"confirmation_id": confirmation, "status": "confirmed"}
        return {"error": f"unknown tool {name}"}

    def state(self):
        return {"bookings": dict(self.bookings)}

    def names(self):
        return [name for name, _ in self.executions]


def make_case(case_id="book-sfo-jfk", sim=None, user_messages=None):
    return Case(
        id=case_id,
        description="",
        initial_state={},
        user_messages=user_messages or ["Book me a flight from SFO to JFK."],
        checks=[],
        sim=sim if sim is not None else {"oracle_plan": PLAN},
    )


def make_config(
    endpoint="responses",
    model="sim-5.5",
    params=None,
    system_prompt=SYSTEM_PROMPT,
    book_description="Book a flight.",
    max_turns=12,
):
    return AgentConfig(
        name="booking_agent",
        endpoint=endpoint,
        model=model,
        params={} if params is None else params,
        system_prompt=system_prompt,
        tools=make_tools(book_description),
        max_turns=max_turns,
        agent_dir="/nonexistent/agent",
    )


def run(rep=0, provider=None, case=None, **config_kwargs):
    backend = StubBookingBackend()
    result = run_episode(
        make_config(**config_kwargs),
        case or make_case(),
        provider or SimProvider(),
        backend,
        rep=rep,
        seed=rep,
    )
    return result, backend


def transcript(result):
    """Everything about an episode except wall-clock latency."""
    return json.dumps(
        {
            "api_calls": [asdict(c) for c in result.api_calls],
            "tool_executions": [asdict(t) for t in result.tool_executions],
            "final_message": result.final_message,
            "final_state": result.final_state,
            "usage": result.usage,
            "resolved_model": result.resolved_model,
            "api_error": result.api_error,
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# sim-5.5: faithful plan execution
# ---------------------------------------------------------------------------


def test_sim55_executes_the_plan_faithfully_on_chat_completions():
    result, backend = run(endpoint="chat_completions", model="sim-5.5")

    assert result.api_error is None
    assert len(result.api_calls) == 3
    assert backend.names() == ["search_flights", "book_flight"]
    assert result.tool_executions[0].arguments == {"origin": "SFO", "destination": "JFK"}
    assert result.tool_executions[1].arguments == {"flight_id": "F1"}  # $ref resolved
    assert result.final_message == "All set. Your confirmation number is UPS-1."
    assert result.final_state == {"bookings": {"UPS-1": {"flight_id": "F1"}}}
    assert result.resolved_model == "sim-5.5"
    assert result.usage["input_tokens"] > 0 and result.usage["output_tokens"] > 0


def test_sim55_executes_the_plan_faithfully_on_responses():
    result, backend = run(endpoint="responses", model="sim-5.5")

    assert result.api_error is None
    assert backend.names() == ["search_flights", "book_flight"]
    assert result.tool_executions[1].arguments == {"flight_id": "F1"}
    assert result.final_message == "All set. Your confirmation number is UPS-1."
    # wire shape the agent loop parses
    tool_turn = result.api_calls[0].response["output"][0]
    assert tool_turn["type"] == "function_call"
    assert tool_turn["name"] == "search_flights"
    assert json.loads(tool_turn["arguments"]) == {"origin": "SFO", "destination": "JFK"}
    assert tool_turn["call_id"].startswith("call_")
    assert result.api_calls[0].response["id"].startswith("sim-")


def test_sim55_multi_segment_plan():
    case = make_case(
        user_messages=["Find me a flight.", "Book the first one."],
        sim={
            "oracle_plan": [
                PLAN[0],
                {"final_message": "I found flight {ref:search_flights[0].flights[0].id}."},
                PLAN[1],
                PLAN[2],
            ]
        },
    )
    result, backend = run(case=case)

    assert backend.names() == ["search_flights", "book_flight"]
    assert [t.segment for t in result.tool_executions] == [0, 1]
    assert result.final_message == "All set. Your confirmation number is UPS-1."


def test_plan_exhaustion_falls_back_to_done():
    case = make_case(
        user_messages=["Find me a flight.", "Anything else?"],
        sim={"oracle_plan": [PLAN[0], {"final_message": "Found one."}]},
    )
    result, _ = run(case=case)

    assert result.final_message == "Done."


def test_unresolvable_ref_becomes_ref_error():
    case = make_case(
        sim={"oracle_plan": [{"final_message": "Ref {ref:book_flight[0].confirmation_id}."}]}
    )
    result, _ = run(case=case)

    assert result.final_message == "Ref REF-ERROR."


def test_message_step_aliases_are_accepted():
    for key in ("final_message", "message", "text"):
        case = make_case(sim={"oracle_plan": [{key: "Nothing to do."}]})
        result, _ = run(case=case)
        assert result.final_message == "Nothing to do."


def test_tool_step_aliases_are_accepted():
    plans = [
        [{"calls": [{"tool": "search_flights", "args": {"origin": "SFO"}}]}],
        [{"name": "search_flights", "arguments": {"origin": "SFO"}}],
    ]
    for plan in plans:
        result, backend = run(case=make_case(sim={"oracle_plan": plan}))
        assert backend.executions[0] == ("search_flights", {"origin": "SFO"})
        assert result.final_message == "Done."  # plan exhausted after the tool step


def test_unknown_model_is_an_api_error():
    result, _ = run(model="gpt-5.5")

    assert result.api_error is not None
    assert result.api_error["status_code"] == 404
    assert result.api_calls[0].response is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_identical_inputs_produce_identical_transcripts():
    first, _ = run(rep=2, model="sim-5.5")
    second, _ = run(rep=2, model="sim-5.5")

    assert transcript(first) == transcript(second)


def test_determinism_holds_for_a_reused_provider_and_across_reps():
    provider = SimProvider()
    a, _ = run(rep=4, provider=provider, model="sim-5.6-sol")
    b, _ = run(rep=4, provider=provider, model="sim-5.6-sol")
    other, _ = run(rep=5, provider=provider, model="sim-5.6-sol")

    assert transcript(a) == transcript(b)  # episode state resets at seed_key ":0"
    assert isinstance(transcript(other), str)


# ---------------------------------------------------------------------------
# sim-5.6: the documented hard break
# ---------------------------------------------------------------------------


HARD_BREAK_FRAGMENT = "Function tools with reasoning_effort are not supported"


def test_hard_break_fires_on_chat_completions_with_tools():
    result, backend = run(endpoint="chat_completions", model="sim-5.6-sol")

    assert result.api_error is not None
    assert result.api_error["status_code"] == 400
    assert result.api_error["type"] == "invalid_request_error"
    assert HARD_BREAK_FRAGMENT in result.api_error["message"]
    assert "/v1/responses" in result.api_error["message"]
    assert len(result.api_calls) == 1  # episode ends immediately
    assert backend.names() == []


def test_hard_break_fires_even_when_reasoning_effort_is_set_to_a_normal_value():
    result, _ = run(
        endpoint="chat_completions", model="sim-5.6-sol", params={"reasoning_effort": "medium"}
    )

    assert result.api_error is not None and result.api_error["status_code"] == 400


def test_hard_break_suppressed_by_reasoning_effort_none():
    result, backend = run(
        endpoint="chat_completions",
        model="sim-5.6-sol",
        params={"reasoning_effort": "none"},
        case=make_case(sim={"oracle_plan": PLAN}),
    )

    assert result.api_error is None
    assert backend.names() == ["search_flights", "book_flight"]


def test_hard_break_does_not_fire_on_the_responses_endpoint():
    result, backend = run(endpoint="responses", model="sim-5.6-sol")

    assert result.api_error is None
    assert backend.names() == ["search_flights", "book_flight"]


def test_hard_break_does_not_fire_for_sim55():
    result, _ = run(
        endpoint="chat_completions", model="sim-5.5", params={"reasoning_effort": "high"}
    )

    assert result.api_error is None


# ---------------------------------------------------------------------------
# sim-5.6 corruptions
# ---------------------------------------------------------------------------


def vulnerable_case(rule, case_id="book-sfo-jfk"):
    return make_case(case_id=case_id, sim={"oracle_plan": PLAN, "vulnerable_to": [rule]})


def find_rep(rule, predicate, reps=30, **config_kwargs):
    """Corruptions are seeded per (case_id, rep, model); find a rep where the draw fires."""
    case = vulnerable_case(rule)
    for rep in range(reps):
        result, backend = run(rep=rep, model="sim-5.6-sol", case=case, **config_kwargs)
        if predicate(result, backend):
            return rep
    raise AssertionError(f"{rule} never fired across {reps} reps")


def test_duplicate_call_fires_and_is_suppressed_by_prompt_marker():
    rep = find_rep("duplicate_call", lambda r, b: b.names().count("book_flight") == 2)

    fired, backend = run(rep=rep, model="sim-5.6-sol", case=vulnerable_case("duplicate_call"))
    assert backend.names() == ["search_flights", "book_flight", "book_flight"]
    # both calls are in ONE assistant turn, with identical arguments and distinct ids
    assert [t.turn for t in fired.tool_executions] == [0, 1, 1]
    assert (
        fired.tool_executions[1].arguments
        == fired.tool_executions[2].arguments
        == {"flight_id": "F1"}
    )
    tool_calls = fired.api_calls[1].response["output"]
    assert len({item["call_id"] for item in tool_calls}) == 2
    assert len(backend.bookings) == 2

    repaired, backend = run(
        rep=rep,
        model="sim-5.6-sol",
        case=vulnerable_case("duplicate_call"),
        system_prompt=SYSTEM_PROMPT + " Call book_flight at most once per itinerary.",
    )
    assert backend.names() == ["search_flights", "book_flight"]
    assert repaired.final_message == "All set. Your confirmation number is UPS-1."


def test_duplicate_call_suppressed_by_tool_schema_marker():
    rep = find_rep("duplicate_call", lambda r, b: b.names().count("book_flight") == 2)

    _, backend = run(
        rep=rep,
        model="sim-5.6-sol",
        case=vulnerable_case("duplicate_call"),
        book_description="Book a flight. Call this exactly once per confirmed itinerary.",
    )
    assert backend.names() == ["search_flights", "book_flight"]


def test_duplicate_call_marker_in_an_unrelated_prompt_does_not_suppress():
    rep = find_rep("duplicate_call", lambda r, b: b.names().count("book_flight") == 2)

    _, backend = run(
        rep=rep,
        model="sim-5.6-sol",
        case=vulnerable_case("duplicate_call"),
        system_prompt=SYSTEM_PROMPT + " Be concise.",
    )
    assert backend.names().count("book_flight") == 2


def over_acting_fired(result, backend):
    return backend.names() == ["search_flights", "book_flight", "book_flight"] and [
        t.turn for t in result.tool_executions
    ] == [0, 1, 2]


def test_over_acting_fires_and_is_suppressed_by_prompt_marker():
    rep = find_rep("over_acting", over_acting_fired)

    fired, backend = run(rep=rep, model="sim-5.6-sol", case=vulnerable_case("over_acting"))
    # an extra assistant turn re-issues the last tool call, then the final message lands
    assert len(fired.api_calls) == 4
    assert fired.tool_executions[2].arguments == fired.tool_executions[1].arguments
    assert fired.final_message == "All set. Your confirmation number is UPS-1."

    repaired, backend = run(
        rep=rep,
        model="sim-5.6-sol",
        case=vulnerable_case("over_acting"),
        system_prompt=SYSTEM_PROMPT + " Stop once the task is complete.",
    )
    assert backend.names() == ["search_flights", "book_flight"]
    assert len(repaired.api_calls) == 3


def skip_tool_fired(result, backend):
    return "book_flight" not in backend.names()


def test_skip_tool_fires_with_a_fabricated_confirmation_and_is_suppressed_by_prompt_marker():
    rep = find_rep("skip_tool", skip_tool_fired)

    fired, backend = run(rep=rep, model="sim-5.6-sol", case=vulnerable_case("skip_tool"))
    assert backend.names() == ["search_flights"]
    assert backend.bookings == {}
    match = re.search(r"UPS-9\d{3}", fired.final_message)
    assert match, fired.final_message
    assert match.group(0) not in fired.final_state["bookings"]  # hallucinated

    repaired, backend = run(
        rep=rep,
        model="sim-5.6-sol",
        case=vulnerable_case("skip_tool"),
        system_prompt=SYSTEM_PROMPT + " Never state a confirmation number a tool did not return.",
    )
    assert backend.names() == ["search_flights", "book_flight"]
    assert repaired.final_message == "All set. Your confirmation number is UPS-1."


def test_skip_tool_suppressed_by_tool_schema_marker():
    rep = find_rep("skip_tool", skip_tool_fired)

    _, backend = run(
        rep=rep,
        model="sim-5.6-sol",
        case=vulnerable_case("skip_tool"),
        book_description="Book a flight. This tool must be called before confirming a booking.",
    )
    assert backend.names() == ["search_flights", "book_flight"]


def test_skip_tool_appends_a_confirmation_when_the_message_has_no_reference():
    plan = [PLAN[0], PLAN[1], {"final_message": "You are all set for your trip."}]
    case = make_case(sim={"oracle_plan": plan, "vulnerable_to": ["skip_tool"]})
    for rep in range(30):
        result, backend = run(rep=rep, model="sim-5.6-sol", case=case)
        if "book_flight" not in backend.names():
            assert result.final_message.startswith("You are all set for your trip.")
            assert re.search(r"Your confirmation number is UPS-9\d{3}\.$", result.final_message)
            return
    raise AssertionError("skip_tool never fired across 30 reps")


def test_skip_tool_only_volunteers_the_fabricated_id_on_the_final_message():
    plan = [
        PLAN[0],
        PLAN[1],
        {"final_message": "Here is what I found."},
        {"final_message": "All done."},
    ]
    case = make_case(
        sim={"oracle_plan": plan, "vulnerable_to": ["skip_tool"]},
        user_messages=["Book me a flight.", "Anything else?"],
    )
    for rep in range(30):
        result, backend = run(rep=rep, model="sim-5.6-sol", case=case)
        if "book_flight" not in backend.names():
            texts = [
                item["content"][0]["text"]
                for call in result.api_calls
                for item in (call.response or {}).get("output", [])
                if item.get("type") == "message"
            ]
            assert texts[0] == "Here is what I found."  # intermediate turn is left alone
            assert re.fullmatch(r"All done\. Your confirmation number is UPS-9\d{3}\.", texts[1])
            return
    raise AssertionError("skip_tool never fired across 30 reps")


def test_corruptions_do_not_apply_to_sim55():
    for rep in range(10):
        _, backend = run(rep=rep, model="sim-5.5", case=vulnerable_case("duplicate_call"))
        assert backend.names() == ["search_flights", "book_flight"]


def test_corruptions_apply_on_chat_completions_with_reasoning_effort_none():
    case = vulnerable_case("duplicate_call")
    outcomes = set()
    for rep in range(20):
        _, backend = run(
            rep=rep,
            model="sim-5.6-sol",
            case=case,
            endpoint="chat_completions",
            params={"reasoning_effort": "none"},
        )
        outcomes.add(backend.names().count("book_flight"))
    assert 2 in outcomes


# ---------------------------------------------------------------------------
# Case-level flakiness (both models)
# ---------------------------------------------------------------------------


def flaky_case(rate, case_id="flaky-case"):
    return make_case(
        case_id=case_id,
        sim={"oracle_plan": PLAN, "flaky": {"rate": rate, "mode": "skip_final_tool"}},
    )


def test_flaky_rate_one_always_skips_the_final_tool_call_on_both_models():
    for model in ("sim-5.5", "sim-5.6-sol"):
        result, backend = run(rep=0, model=model, case=flaky_case(1.0))
        assert backend.names() == ["search_flights"]
        # the ref into the skipped call is unresolvable, which is acceptable
        assert result.final_message == "All set. Your confirmation number is REF-ERROR."


def test_flaky_rate_zero_never_skips():
    for model in ("sim-5.5", "sim-5.6-sol"):
        _, backend = run(rep=0, model=model, case=flaky_case(0.0))
        assert backend.names() == ["search_flights", "book_flight"]


def test_flaky_produces_both_outcomes_across_reps():
    case = flaky_case(0.5)
    skipped = 0
    for rep in range(30):
        _, backend = run(rep=rep, model="sim-5.5", case=case)
        if "book_flight" not in backend.names():
            skipped += 1
    assert 0 < skipped < 30
    assert 5 <= skipped <= 25  # deliberately loose: only checks the knob is roughly honoured
