"""Cost model: standard vs flex/batch tier, cached-input discount, unknown models."""

import pytest

from upshift.pricing import price


def test_standard_sync_rates():
    # 1M in + 0.1M out on gpt-5.5 sync: 1.0*5 + 0.1*30 = 8.0
    assert abs(price("openai", "gpt-5.5", 1_000_000, 100_000, 0) - 8.0) < 1e-9


def test_flex_and_batch_halve_everything():
    sync = price("openai", "gpt-5.6-sol", 1_000_000, 100_000, 0)
    flex = price("openai-flex", "gpt-5.6-sol", 1_000_000, 100_000, 0)
    batch = price("openai-batch", "gpt-5.6-sol", 1_000_000, 100_000, 0)
    assert abs(flex - sync / 2) < 1e-9
    assert abs(batch - sync / 2) < 1e-9


def test_cached_input_bills_at_ten_percent():
    # gpt-5.5 flex: in rate 2.50, out 15.00. 1M input fully cached -> 1.0*2.5*0.1 = 0.25
    assert abs(price("openai-flex", "gpt-5.5", 1_000_000, 0, 1_000_000) - 0.25) < 1e-9
    # half cached: 0.5*2.5 + 0.5*0.25 = 1.375
    assert abs(price("openai-flex", "gpt-5.5", 1_000_000, 0, 500_000) - 1.375) < 1e-9


def test_cached_never_exceeds_input():
    assert abs(
        price("openai", "gpt-5.5", 100, 0, 10_000) - price("openai", "gpt-5.5", 100, 0, 100)
    ) < 1e-12


def test_snapshot_model_ids_match_by_prefix():
    assert price("openai", "gpt-5.5-2026-05-01", 1_000_000, 0, 0) == 5.0


def test_unknown_model_and_provider():
    assert price("openai", "gpt-9-mystery", 1000, 1000, 0) is None
    assert price("some-proxy", "gpt-5.5", 1000, 1000, 0) is None
    assert price("sim", "sim-5.6-sol", 1000, 1000, 0) == 0.0


def test_claude_sonnet_4_5_has_a_known_rate():
    """A model the lab actually runs must price, or `upshift cost` reports "unknown rate"
    and the whole spend ledger stops being accountable. Sonnet 4.5 is $3/$15 per MTok with
    cache reads at $0.30/MTok (10% of input), per claude.com/pricing (Legacy models),
    verified 2026-09-04."""
    assert abs(price("anthropic", "claude-sonnet-4-5", 1_000_000, 100_000, 0) - 4.5) < 1e-9
    # snapshot id resolves by prefix
    assert abs(
        price("anthropic", "claude-sonnet-4-5-20250929", 1_000_000, 0, 0) - 3.0
    ) < 1e-9
    # cache reads at 10% of the input rate: 1M fully cached -> 0.30
    assert abs(price("anthropic", "claude-sonnet-4-5", 1_000_000, 0, 1_000_000) - 0.30) < 1e-9


# Every model id `upshift` can be pointed at must price, or `upshift cost` reports
# "unknown rate" on a real run and the whole cost column becomes a guess.
GPT_56_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


@pytest.mark.parametrize("model", GPT_56_MODELS)
def test_every_served_gpt_56_model_has_a_rate(model):
    assert price("openai", model, 1_000_000, 1_000_000, 0) is not None, (
        f"{model} has no entry in pricing.RATES"
    )


@pytest.mark.parametrize("model", GPT_56_MODELS)
def test_gpt_56_published_rates(model):
    # developers.openai.com/api/docs/pricing, fetched 2026-09-03; USD per 1M tokens.
    published = {
        "gpt-5.6-sol": (4.00, 20.00),
        "gpt-5.6-terra": (2.00, 12.00),
        "gpt-5.6-luna": (0.20, 1.20),
    }[model]
    assert abs(price("openai", model, 1_000_000, 0, 0) - published[0]) < 1e-9
    assert abs(price("openai", model, 0, 1_000_000, 0) - published[1]) < 1e-9


@pytest.mark.parametrize("model", GPT_56_MODELS)
def test_gpt_56_flex_batch_and_cached_match_the_published_table(model):
    # The published flex and batch rows are exactly half the standard row, and the published
    # cached-input rate is exactly 10% of the applicable input rate, for all three models.
    standard = price("openai", model, 1_000_000, 100_000, 0)
    assert abs(price("openai-flex", model, 1_000_000, 100_000, 0) - standard / 2) < 1e-9
    assert abs(price("openai-batch", model, 1_000_000, 100_000, 0) - standard / 2) < 1e-9
    full_input = price("openai", model, 1_000_000, 0, 0)
    assert abs(price("openai", model, 1_000_000, 0, 1_000_000) - full_input / 10) < 1e-9


# gpt-5.2 and gpt-5-mini: models a user can point --baseline-model / --candidate-model at.
# developers.openai.com/api/docs/pricing, fetched 2026-09-03; USD per 1M tokens, standard tier.
GPT_52_MODELS = {
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.2-pro": (21.00, 168.00),
    "gpt-5-mini": (0.25, 2.00),
}


@pytest.mark.parametrize("model", sorted(GPT_52_MODELS))
def test_gpt_52_family_published_rates(model):
    published = GPT_52_MODELS[model]
    assert abs(price("openai", model, 1_000_000, 0, 0) - published[0]) < 1e-9
    assert abs(price("openai", model, 0, 1_000_000, 0) - published[1]) < 1e-9


def test_gpt_52_pro_does_not_resolve_to_the_gpt_52_rate():
    """`gpt-5.2-pro` is a separate published row at 12x the `gpt-5.2` rate. Without its own
    entry the longest-prefix lookup would price it as `gpt-5.2` and understate a run 12-fold
    — which, with a spend ceiling set, is a fail-open."""
    assert price("openai", "gpt-5.2-pro", 1_000_000, 0, 0) > price(
        "openai", "gpt-5.2", 1_000_000, 0, 0
    )


def test_gpt_52_snapshot_and_flex_rows():
    assert abs(price("openai", "gpt-5.2-2026-01-15", 1_000_000, 0, 0) - 1.75) < 1e-9
    # published flex/batch row is exactly half standard: $0.875 in / $7.00 out
    assert abs(price("openai-flex", "gpt-5.2", 1_000_000, 0, 0) - 0.875) < 1e-9
    assert abs(price("openai-batch", "gpt-5.2", 0, 1_000_000, 0) - 7.00) < 1e-9
    # published cached-input row is exactly 10% of input: $0.175 / $0.025 per 1M
    assert abs(price("openai", "gpt-5.2", 1_000_000, 0, 1_000_000) - 0.175) < 1e-9
    assert abs(price("openai", "gpt-5-mini", 1_000_000, 0, 1_000_000) - 0.025) < 1e-9
