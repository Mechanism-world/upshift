"""A billing/quota API error must abort the run and write no rep records (seen live on both
providers: OpenAI 429 no-credits, Anthropic 400 credit balance too low)."""

from __future__ import annotations

import pytest

from upshift.providers.base import ProviderAPIError
from upshift.runner import BillingError, _billing_message, run_suite


class BrokeProvider:
    name = "openai"
    requires_all_workers = False

    def call(self, endpoint, request, seed_key, sim_context=None):
        raise ProviderAPIError(
            message="Your credit balance is too low to access the Anthropic API. Please go to "
            "Plans & Billing to upgrade or purchase credits.",
            status_code=400,
            error_type="api_status_error",
        )


@pytest.mark.parametrize(
    "message",
    [
        "Your credit balance is too low to access the Anthropic API.",
        "You exceeded your current quota, please check your plan and billing details.",
        "insufficient_quota",
    ],
)
def test_billing_messages_are_recognised(message):
    assert _billing_message({"message": message}) == message


def test_non_billing_errors_pass_through():
    assert _billing_message({"message": "tool_choice: type \"tool\" ... not supported"}) is None
    assert _billing_message(None) is None


def test_run_suite_aborts_without_writing_reps(tmp_path):
    from upshift.cli import example_agent_root

    with pytest.raises(BillingError, match="billing problem"):
        run_suite(
            example_agent_root(),
            BrokeProvider(),
            "billing-abort",
            n_reps=2,
            model_override="gpt-5.5",
            runs_root=tmp_path,
            case_ids=["happy_search_basic"],
            workers=2,
        )
    assert not list((tmp_path / "billing-abort").rglob("rep_*.json"))
