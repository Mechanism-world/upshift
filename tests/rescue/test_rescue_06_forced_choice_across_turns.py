"""Rescue regression 6 — a forced tool choice that changes between turns stays changed.

maintenance coverage derived from rescue case A-062 (the litellm capture shape) — not
independent evidence of general repair capability.

Frameworks that force a tool on the first assistant turn and then go `auto` are a different
agent from ones that force on every turn: under a permanent forced choice the model can never
answer in plain text at all, which is exactly what made the FACT pilot a stable-fail rather
than a regression. One episode-level params dict cannot express the difference, so the
capture derives `turn_params` (DESIGN §F/§G). This pins that request 1 carries `any` and
request 2 carries `auto`.
"""

from __future__ import annotations

import copy

import pytest
from _support import incident

from upshift.agent_loop import build_request, params_for_turn

pytestmark = [pytest.mark.recorded_fixture]

CAPTURE = incident("litellm_capture")


def test_fixture_is_the_recorded_two_turn_shape():
    assert CAPTURE["endpoint"] == "messages"
    assert CAPTURE["params"] == {"max_tokens": 512, "tool_choice": {"type": "any"}}
    assert CAPTURE["turn_params"] == [
        {"tool_choice": {"type": "any"}},
        {"tool_choice": {"type": "auto"}},
    ]


def test_request_one_forces_any_and_request_two_goes_auto():
    params = CAPTURE["params"]
    turn_params = CAPTURE["turn_params"]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look something up.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    items = [{"role": "user", "content": "what is the status?"}]

    requests = [
        build_request(
            "messages",
            CAPTURE["candidate_model"],
            params_for_turn(params, turn_params, index),
            tools=copy.deepcopy(tools),
            items=copy.deepcopy(items),
            system="You are a support bot.",
        )
        for index in (0, 1)
    ]

    assert requests[0]["tool_choice"] == {"type": "any"}
    assert requests[1]["tool_choice"] == {"type": "auto"}
    assert requests[0]["max_tokens"] == requests[1]["max_tokens"] == 512


def test_the_last_recorded_turn_repeats_for_every_later_turn():
    params = CAPTURE["params"]
    turn_params = CAPTURE["turn_params"]
    for index in (2, 3, 9):
        assert params_for_turn(params, turn_params, index)["tool_choice"] == {"type": "auto"}


def test_an_override_of_none_unsets_the_param_rather_than_sending_auto():
    """"the framework did not send this field" is a different request from "it sent auto"."""
    merged = params_for_turn({"max_tokens": 512, "tool_choice": {"type": "any"}},
                             [{}, {"tool_choice": None}], 1)
    assert "tool_choice" not in merged
    assert merged["max_tokens"] == 512
