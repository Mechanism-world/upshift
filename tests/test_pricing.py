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


def test_zero_usage_is_zero_even_for_an_unpriced_model():
    """A run that recorded no tokens cost $0 whatever the rate would have been.

    Regression: an aborted run (e.g. a provider billing 400 on the first call) leaves a
    manifest and no reps. Reporting that as `unknown rate` froze the lab's whole budget
    ledger — `budget.py check` refuses to authorise any spend while an unknown-rate run is
    on record — over a run that provably cost nothing.
    """
    assert price("anthropic", "claude-nonesuch-9", 0, 0, 0, 0) == 0.0
    assert price("openai", "gpt-9-mystery", 0, 0, 0, 0) == 0.0
    # An unknown model with real usage is still unknown — the guard keeps its teeth.
    assert price("anthropic", "claude-nonesuch-9", 10, 0, 0, 0) is None
    # An unknown provider stays unknown at zero usage too: the tier multiplier, not the
    # token count, is what is missing, and a proxy may bill on its own terms.
    assert price("some-proxy", "gpt-5.5", 0, 0, 0, 0) is None


def test_claude_opus_4_8_has_a_known_rate():
    """The Anthropic rescue track runs claude-opus-4-8; without a rate every run it
    touches reports "unknown rate" and freezes the lab's budget guard. $5/$25 per MTok
    with cache reads at the default 10% ($0.50/MTok), per the published Anthropic
    model/pricing reference, verified 2026-09-04."""
    assert abs(price("anthropic", "claude-opus-4-8", 1_000_000, 100_000, 0) - 7.5) < 1e-9
    # cache reads at 10% of the input rate: 1M fully cached -> 0.50
    assert abs(price("anthropic", "claude-opus-4-8", 1_000_000, 0, 1_000_000) - 0.50) < 1e-9
    # 5-minute cache writes at 1.25x input: 1M written -> 6.25
    assert abs(price("anthropic", "claude-opus-4-8", 0, 0, 0, 1_000_000) - 6.25) < 1e-9
