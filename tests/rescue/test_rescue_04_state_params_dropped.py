"""Rescue regression 4 — state-linking params cannot contaminate a one-shot replay.

maintenance coverage derived from rescue cases ghisdk-052 (omnideck-dev/omnideck, responses)
and trk-010 (the Responses replay class) — not independent evidence of general repair
capability.

A captured or adapted agent replays one episode at a time against a stateless request
builder. `previous_response_id`, `conversation` and `store: true` link a request to
server-side state from *another* episode: forwarded, they either 404 on a response id from a
different account/run, or ask the provider to retain the transcript upshift promised stays on
the operator's machine. DESIGN §G says they are DROPPED, listed in `dropped_params`, and that
the managed `store: false` cannot be overridden. Nothing is silently dropped.
"""

from __future__ import annotations

import pytest
from _support import dropped_names, incident, translation_report

from upshift.agent_loop import build_request

pytestmark = [pytest.mark.mocked_transport]

STATEFUL = {
    "previous_response_id": "resp_abc123",
    "conversation": "conv_abc123",
    "store": True,
}


def test_state_linking_params_are_dropped_and_recorded():
    params = dict(incident("omnideck")["params"]) | STATEFUL

    report = translation_report("responses", params)

    # DESIGN §G element shape: `dropped_params: [{name, reason}]` — the names are asserted
    # here, and `every drop carries a reason` below.
    dropped = set(dropped_names(report))
    assert {"previous_response_id", "conversation", "store"} <= dropped, (
        f"state-linking params must be dropped and listed; got {sorted(dropped)}"
    )
    assert all(entry.get("reason") for entry in report["dropped_params"]), (
        "every drop must say why: DESIGN §G, nothing is silently dropped"
    )
    mapped = report.get("params") or {}
    assert "previous_response_id" not in mapped
    assert "conversation" not in mapped


def test_the_managed_store_false_cannot_be_overridden():
    """`store: true` in the agent's params must not turn on server-side retention."""
    # Same DESIGN §G work item as the drop list: gate on it so this reads as pending, not
    # as a failure the integration owner has to bisect.
    translation_report("responses", dict(STATEFUL))
    request = build_request(
        "responses",
        "gpt-5.5",
        dict(STATEFUL),
        tools=[],
        items=[{"role": "user", "content": "hello"}],
    )
    assert request["store"] is False, (
        "build_request manages `store` and a params value must not win; DESIGN §G "
        "(state-linking params are dropped on one-shot replays)"
    )
    assert "previous_response_id" not in request
    assert "conversation" not in request


def test_an_unknown_param_passes_through_and_is_listed():
    report = translation_report("responses", {"safety_identifier": "user-42"})

    mapped = report.get("params") or {}
    assert mapped.get("safety_identifier") == "user-42"
    assert "safety_identifier" in set(report.get("passthrough_params") or [])
