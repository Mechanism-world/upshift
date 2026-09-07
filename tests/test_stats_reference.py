"""Reference values for stats.py — the numbers, not the code's opinion of the numbers.

`tests/test_stats.py` checks properties (monotonicity, bounds, symmetry). Properties cannot
catch a systematically wrong formula: a p-value that is uniformly off by one term is still
monotone, still in [0, 1], still small for a collapse. So this file checks against values
computed a DIFFERENT way:

- Fisher: an independent exact enumerator written here from the hypergeometric definition,
  plus hand-computed rationals for the small tables (verifiable with a pencil);
- Wilson: published intervals from Newcombe (1998), "Two-sided confidence intervals for the
  single proportion: comparison of seven methods", Statistics in Medicine 17:857-872, whose
  worked examples (81/263, 15/148, 0/20, 1/29) are the standard check for this interval.

The one number the whole product is quoted on — "N=5 detects a 5/5 -> 0/5 collapse at
p≈0.004" — is pinned here too, because a report sentence that drifts from the function it
cites is worse than no sentence.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb

import pytest

from upshift.stats import Z_95, fisher_exact_one_sided, smallest_detectable_drop, wilson_interval


def exact_fisher(b_pass: int, b_n: int, c_pass: int, c_n: int) -> Fraction:
    """Independent one-sided Fisher exact p, in exact rational arithmetic.

    Deliberately NOT a re-expression of stats.py: written straight from
    P(X = x) = C(K, x) C(N-K, n-x) / C(N, n), summed over every x <= c_pass with non-zero
    probability, in Fractions so there is no floating point to hide a discrepancy.
    """
    total, successes, draws = b_n + c_n, b_pass + c_pass, c_n
    p = Fraction(0)
    for x in range(c_pass + 1):
        if x > successes or draws - x > total - successes or draws - x < 0:
            continue
        p += Fraction(comb(successes, x) * comb(total - successes, draws - x), comb(total, draws))
    return p


# ---------------------------------------------------------------------------
# Fisher exact, one-sided
# ---------------------------------------------------------------------------

#: (b_pass, b_n, c_pass, c_n, expected p as an exact fraction). Each derivation, by hand:
#: 1. total collapse at N=5: N=10, K=5, n=5, only x=0 -> C(5,0)C(5,5)/C(10,5) = 1/252
#: 2. one rep survives:      K=6, support starts at x=1 -> C(6,1)C(4,4)/252 = 6/252 = 1/42
#: 3. two survive:           K=7, x in {2}: C(7,2)C(3,3)/252 = 21/252 = 1/12
#: 4. no change at all -> degenerate margins -> p = 1 by definition
#: 5. an improvement -> p = 1 (the test is one-sided, and one-sided the RIGHT way)
#: 6. a total collapse at --n 10: C(10,0)C(10,10)/C(20,10) = 1/184756
#: 7. the smallest interesting table: N=4, K=2, x=0 -> C(2,0)C(2,2)/C(4,2) = 1/6
#: 8. asymmetric run sizes (a 3-rep screen against a 5-rep verify): C(3,0)C(5,5)/C(8,5) = 1/56
REFERENCE_TABLES = [
    (5, 5, 0, 5, Fraction(1, 252)),
    (5, 5, 1, 5, Fraction(1, 42)),
    (5, 5, 2, 5, Fraction(1, 12)),
    (5, 5, 5, 5, Fraction(1)),
    (0, 5, 5, 5, Fraction(1)),
    (10, 10, 0, 10, Fraction(1, 184756)),
    (2, 2, 0, 2, Fraction(1, 6)),
    (3, 3, 0, 5, Fraction(1, 56)),
]


@pytest.mark.parametrize(("b_pass", "b_n", "c_pass", "c_n", "expected"), REFERENCE_TABLES)
def test_fisher_matches_the_hand_computed_value(b_pass, b_n, c_pass, c_n, expected) -> None:
    assert fisher_exact_one_sided(b_pass, b_n, c_pass, c_n) == pytest.approx(
        float(expected), abs=1e-12
    )


@pytest.mark.parametrize("b_n", [2, 3, 5, 8])
@pytest.mark.parametrize("c_n", [2, 3, 5, 8])
def test_fisher_matches_an_independent_enumerator_everywhere(b_n, c_n) -> None:
    """Every 2x2 table these run sizes can produce, against the rational enumerator above."""
    for b_pass in range(b_n + 1):
        for c_pass in range(c_n + 1):
            expected = exact_fisher(b_pass, b_n, c_pass, c_n)
            got = fisher_exact_one_sided(b_pass, b_n, c_pass, c_n)
            assert got == pytest.approx(float(expected), abs=1e-12), (
                f"table ({b_pass}/{b_n}, {c_pass}/{c_n})"
            )


def test_the_headline_number_is_what_the_report_claims() -> None:
    """DESIGN.md and every report say "N=5 detects a 5/5 -> 0/5 collapse (p~=0.004)"."""
    assert fisher_exact_one_sided(5, 5, 0, 5) == pytest.approx(0.003968, abs=5e-6)


# ---------------------------------------------------------------------------
# Wilson score interval — Newcombe (1998) worked examples
# ---------------------------------------------------------------------------

#: (k, n, lower, upper) from Newcombe 1998 Table I, method 3 (Wilson score, no continuity
#: correction), 95%. Published to 4 decimal places.
NEWCOMBE_1998 = [
    (81, 263, 0.2553, 0.3662),
    (15, 148, 0.0624, 0.1605),
    (0, 20, 0.0000, 0.1611),
    (1, 29, 0.0061, 0.1718),
]


@pytest.mark.parametrize(("k", "n", "lower", "upper"), NEWCOMBE_1998)
def test_wilson_matches_newcombe_1998(k, n, lower, upper) -> None:
    got_lower, got_upper = wilson_interval(k, n)
    assert got_lower == pytest.approx(lower, abs=5e-5)
    assert got_upper == pytest.approx(upper, abs=5e-5)


def test_wilson_pins_the_degenerate_bounds() -> None:
    """0/n cannot have a lower bound above 0 and n/n cannot have an upper bound below 1;
    floating point would otherwise report 0.9999999999999999 for a case that passed
    everything, and a report that prints "CI 66.6-100.0%" must mean it."""
    assert wilson_interval(0, 20)[0] == 0.0
    assert wilson_interval(20, 20)[1] == 1.0


def test_wilson_z_is_the_two_sided_95_quantile() -> None:
    assert Z_95 == pytest.approx(1.959964, abs=1e-6)


def test_wilson_is_not_the_normal_approximation() -> None:
    """The Wald interval for 0/20 is (0, 0) — the failure mode Wilson is chosen to avoid."""
    assert wilson_interval(0, 20)[1] > 0.15


# ---------------------------------------------------------------------------
# Detectability, and the multiple-comparison caveat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("n", "expected"), [(1, None), (2, None), (3, None), (5, 1), (10, 6)])
def test_smallest_detectable_drop(n, expected) -> None:
    got = smallest_detectable_drop(n)
    assert (got[0] if got else None) == expected


def test_at_n_equals_3_not_even_a_total_collapse_is_significant() -> None:
    """3/3 -> 0/3 is p = 1/20 = 0.05 exactly, which is NOT < 0.05. Worth pinning: a user who
    drops to --n 3 to save money buys a suite that cannot report a single significant case."""
    assert fisher_exact_one_sided(3, 3, 0, 3) == pytest.approx(0.05, abs=1e-12)
    assert smallest_detectable_drop(3) is None


def test_footnote_reports_the_test_count_and_the_lack_of_adjustment() -> None:
    """p is per case and unadjusted; a 38-case suite runs 38 tests, and at alpha=0.05 about
    two of them can reach significance with no regression present at all. The real
    38-case booking suite is the reason this line exists."""
    from upshift import report
    from upshift.differ import CaseDiff, DiffResult
    from upshift.schemas import LABEL_STABLE_PASS, OUTCOME_PASS

    cases = [
        CaseDiff(
            case_id=f"c{i}",
            baseline_passes=5,
            baseline_n=5,
            candidate_passes=5,
            candidate_n=5,
            baseline_outcome=OUTCOME_PASS,
            candidate_outcome=OUTCOME_PASS,
            label=LABEL_STABLE_PASS,
            p_value=1.0,
            failure_signatures=[],
            failing_check_details=[],
        )
        for i in range(38)
    ]
    manifest = {
        "provider": "openai",
        "n_reps": 5,
        "thresholds": {"pass": 0.8, "fail": 0.4},
        "agent": {"model_requested": "m", "endpoint": "chat_completions"},
    }
    result = DiffResult("b", "c", manifest, manifest, cases, {})
    footnote = " ".join(report._footnote(result))
    assert "38 test(s) performed, one per case" in footnote
    assert "NOT adjusted for multiple comparisons" in footnote
    assert "roughly 2 of 38" in footnote
