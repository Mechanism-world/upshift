"""Cost model: standard vs flex/batch tier, cached-input discount, unknown models."""

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
