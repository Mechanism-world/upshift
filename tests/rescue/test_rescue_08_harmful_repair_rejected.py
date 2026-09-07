"""Rescue regression 8 — a repair that fixes one case and damages another is rejected.

maintenance coverage derived from rescue cases ghisdk-127 (waku-agent, the endpoint repair
that took `pokemon-team` from 4/10 to 0/10) and ghi56-006 (atlas-ui-3, a route-to-responses
candidate rejected outright) — not independent evidence of general repair capability.

This is the load-bearing guarantee of the whole tool: `SAFE WITH PATCH` claims zero
collateral damage. The claim is only worth anything if a candidate that trades one case for
another actually gets thrown out, by name, in the log a human reads.
"""

from __future__ import annotations

import json

import pytest
from _support import BOOKING_TOOLS, write_agent_dir

from upshift.differ import diff_runs
from upshift.providers.base import Provider, ProviderAPIError
from upshift.recorder import run_dir
from upshift.repair.loop import repair
from upshift.runner import run_suite
from upshift.verdict import STAY_PINNED, decide

pytestmark = [pytest.mark.mocked_transport]

BASELINE_MODEL = "rescue-base"
CANDIDATE_MODEL = "rescue-cand"

#: The documented gpt-5.6-family break, verbatim enough for differ's signature matcher.
TOOLS_400 = (
    "Function tools with reasoning_effort are not supported for rescue-cand in "
    "/v1/chat/completions. Use /v1/responses or set reasoning_effort to 'none'."
)


class TradeOffProvider(Provider):
    """chat/completions 400s the broken case; /v1/responses fixes it and breaks the other one.

    Exactly the shape a real trade-off takes: the repair the failure signature calls for is
    right for the case that failed and wrong for a case that was fine.
    """

    name = "scripted-tradeoff"

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def call(self, endpoint, request, seed_key, sim_context=None):
        case_id = seed_key.split(":")[0]
        model = request["model"]
        self.calls.append((endpoint, model, case_id))

        if model == BASELINE_MODEL:
            return self._text(endpoint, "OK")
        if endpoint == "chat_completions":
            if case_id == "broken_case":
                raise ProviderAPIError(TOOLS_400, status_code=400, error_type="invalid_request")
            return self._text(endpoint, "OK")
        if endpoint == "responses":
            return self._text(endpoint, "OK" if case_id == "broken_case" else "collateral")
        raise AssertionError(f"unexpected endpoint {endpoint!r}")

    @staticmethod
    def _text(endpoint, text):
        if endpoint == "chat_completions":
            return {
                "id": "chatcmpl-x",
                "model": "resolved",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
            }
        return {
            "id": "resp-x",
            "model": "resolved",
            "output": [
                {
                    "type": "message",
                    "id": "msg_0",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
        }


def _case(case_id):
    return {
        "id": case_id,
        "description": case_id,
        "initial_state": {},
        "user_messages": ["do the thing"],
        "checks": [{"type": "no_api_error"}, {"type": "response_contains", "text": "OK"}],
        "sim": {},
    }


@pytest.fixture
def mixed_suite(tmp_path):
    agent = write_agent_dir(
        tmp_path / "agent",
        endpoint="chat_completions",
        model=BASELINE_MODEL,
        cases=[_case("broken_case"), _case("healthy_case")],
    )
    (agent / "tools.json").write_text(json.dumps(BOOKING_TOOLS, indent=2))
    runs = tmp_path / "runs"
    provider = TradeOffProvider()
    run_suite(agent, provider, "r08-baseline", n_reps=5, runs_root=runs, workers=1)
    run_suite(
        agent, provider, "r08-candidate", n_reps=5, runs_root=runs,
        model_override=CANDIDATE_MODEL, workers=1,
    )
    diff = diff_runs(run_dir(runs, "r08-baseline"), run_dir(runs, "r08-candidate"))
    return agent, runs, provider, diff


def test_the_fixture_really_is_one_regression_and_one_protected_case(mixed_suite):
    _, _, _, diff = mixed_suite
    labels = {c.case_id: c.label for c in diff.cases}
    assert labels["broken_case"] == "regressed"
    assert labels["healthy_case"] == "stable-pass"
    assert diff.baseline_passing_cases() == 2


def test_the_trade_off_candidate_is_rejected_and_the_damaged_case_is_named(mixed_suite, tmp_path):
    agent, runs, provider, diff = mixed_suite

    outcome = repair(
        original_agent_dir=agent,
        work_dir=tmp_path / "work",
        provider=provider,
        candidate_model=CANDIDATE_MODEL,
        baseline_diff=diff,
        n_reps=5,
        runs_root=runs,
        run_prefix="r08",
        budget=1,
        workers=1,
    )

    log = "\n".join(outcome.log)
    assert "REJECTED" in log, log
    assert "broke previously-passing case(s) ['healthy_case']" in log, log
    assert outcome.accepted_patches == []
    assert outcome.restored == []
    assert outcome.unrestored == ["broken_case"]


def test_a_rejected_trade_off_cannot_become_safe_with_patch(mixed_suite, tmp_path):
    agent, runs, provider, diff = mixed_suite

    outcome = repair(
        original_agent_dir=agent,
        work_dir=tmp_path / "work",
        provider=provider,
        candidate_model=CANDIDATE_MODEL,
        baseline_diff=diff,
        n_reps=5,
        runs_root=runs,
        run_prefix="r08",
        budget=1,
        workers=1,
    )
    result = decide(diff, outcome)

    assert result["verdict"] == STAY_PINNED
    assert result["patch_path"] is None
    assert result["unrestored"] == ["broken_case"]
