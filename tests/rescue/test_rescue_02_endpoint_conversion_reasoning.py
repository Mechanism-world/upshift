"""Rescue regression 2 — endpoint conversion carries the original reasoning configuration.

maintenance coverage derived from rescue case ghi56-001 (Rynaro/prisma) — not independent
evidence of general repair capability.

prisma sent `{"tool_choice": "required", "max_completion_tokens": 4096}` on
`/v1/chat/completions` and 400'd on every gpt-5.6 model. The accepted repair routes the same
agent to `/v1/responses`, and that conversion has to carry the whole configuration across:
`/v1/responses` spells reasoning `reasoning.effort` and the output cap `max_output_tokens`,
and it rejects the chat spellings outright. Dropping the reasoning configuration on the way
would silently change the agent under test — the migration would be "verified" for an agent
nobody runs.

The fixture of record carries `reasoning_effort: "high"`. The campaign never exercised that
value live (it ran the model's default effort), so this is a translation assertion, not
evidence about how prisma behaves at high effort. `incidents.json` records that explicitly.
"""

from __future__ import annotations

import pytest
from _support import incident

from upshift.agent_loop import build_request, map_params

pytestmark = [pytest.mark.mocked_transport]

PRISMA = incident("prisma")


def test_fixture_is_the_incident_configuration_of_record():
    assert PRISMA["params"] == {
        "tool_choice": "required",
        "max_completion_tokens": 4096,
        "reasoning_effort": "high",
    }
    assert PRISMA["endpoint"] == "chat_completions"
    assert (PRISMA["baseline_model"], PRISMA["candidate_model"]) == ("gpt-5.5", "gpt-5.6-luna")
    # The honesty flag the report has to carry: this value was never run against the API.
    assert PRISMA["untested_params"] == ["reasoning_effort"]


def test_routing_to_responses_keeps_reasoning_and_translates_the_output_cap():
    mapped = map_params("responses", PRISMA["params"])

    assert mapped["reasoning"] == {"effort": "high"}, "the reasoning configuration was dropped"
    assert mapped["max_output_tokens"] == 4096


def test_the_chat_spellings_never_survive_the_conversion():
    mapped = map_params("responses", PRISMA["params"])

    # Both would be rejected by /v1/responses; `max_completion_tokens` raises in the SDK
    # before a request is even sent (the bug fix/responses-token-cap-param landed for).
    assert "reasoning_effort" not in mapped
    assert "max_completion_tokens" not in mapped


def test_the_whole_request_prisma_would_send_after_the_repair():
    request = build_request(
        "responses",
        PRISMA["candidate_model"],
        PRISMA["params"],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "submit_review_findings",
                    "description": "Submit findings.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        items=[{"role": "user", "content": "Review this diff."}],
    )

    assert request["model"] == "gpt-5.6-luna"
    assert request["reasoning"] == {"effort": "high"}
    assert request["max_output_tokens"] == 4096
    assert "max_completion_tokens" not in request
    assert "reasoning_effort" not in request
    # `required` is valid on both endpoints and must pass through unrewritten.
    assert request["tool_choice"] == "required"
    # Flat tool definitions are the /v1/responses shape.
    assert request["tools"][0]["name"] == "submit_review_findings"
