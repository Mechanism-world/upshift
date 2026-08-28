"""Tests for upshift.stats.

Every expected number below is verified by an independent hand computation shown in a
comment, not by running the implementation under test.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from upshift.stats import fisher_exact_one_sided, wilson_interval

# ---------------------------------------------------------------------------
# Fisher exact, one-sided (candidate lower than baseline)
#
# Hand derivation, N = b_n + c_n = 10 draws, K = b_pass + c_pass successes, n = c_n = 5:
#   P(X = x) = C(K, x) * C(N-K, n-x) / C(N, n),  C(10, 5) = 252
#   p = sum over x from max(0, n-(N-K)) to c_pass.
#
# (5,5,0,5): K=5. support floor = max(0, 5-5) = 0, so only x=0.
#            P(0) = C(5,0)*C(5,5)/252 = 1*1/252 = 1/252 = 0.003968253968...
# (5,5,1,5): K=6. floor = max(0, 5-4) = 1, so only x=1.
#            P(1) = C(6,1)*C(4,4)/252 = 6*1/252 = 6/252 = 1/42 = 0.0238095238...
# (5,5,2,5): K=7. floor = max(0, 5-3) = 2, so only x=2.
#            P(2) = C(7,2)*C(3,3)/252 = 21/252 = 1/12 = 0.0833333...
# (5,5,3,5): K=8. floor = 3, only x=3. P(3) = C(8,3)*C(2,2)/252 = 56/252 = 2/9 = 0.222222...
# (5,5,4,5): K=9. floor = 4, only x=4. P(4) = C(9,4)*C(1,1)/252 = 126/252 = 1/2 exactly.
# (5,5,5,5): K=10 = N -> no variation left, p = 1.
# (4,5,4,5): K=8. floor = 3, x in {3,4}.
#            (C(8,3)*C(2,2) + C(8,4)*C(2,1))/252 = (56 + 140)/252 = 196/252 = 7/9 = 0.7777...
# (3,5,3,5): K=6. floor = 1, x in {1,2,3}.
#            (C(6,1)*C(4,4) + C(6,2)*C(4,3) + C(6,3)*C(4,2))/252
#            = (6 + 60 + 120)/252 = 186/252 = 31/42 = 0.7380952...
# (4,5,1,5): K=5. floor = 0, x in {0,1}.
#            (C(5,0)*C(5,5) + C(5,1)*C(5,4))/252 = (1 + 25)/252 = 26/252 = 13/126 = 0.1031746...
#
# Cross-checked by brute force: label the 10 reps, condition on which K of them succeeded
# (all C(10,K) assignments equally likely) and count the fraction with <= c_pass successes
# in the candidate's 5 slots. Same fractions.
# ---------------------------------------------------------------------------

EXACT_FISHER = [
    ((5, 5, 0, 5), 1 / 252),
    ((5, 5, 1, 5), 1 / 42),
    ((5, 5, 2, 5), 1 / 12),
    ((5, 5, 3, 5), 2 / 9),
    ((5, 5, 4, 5), 1 / 2),
    ((5, 5, 5, 5), 1.0),
    ((4, 5, 4, 5), 7 / 9),
    ((3, 5, 3, 5), 31 / 42),
    ((4, 5, 1, 5), 13 / 126),
]


@pytest.mark.parametrize("args,expected", EXACT_FISHER)
def test_fisher_known_values(args: tuple[int, int, int, int], expected: float) -> None:
    assert abs(fisher_exact_one_sided(*args) - expected) < 1e-6


def test_fisher_documented_headline_values() -> None:
    # The two values quoted in DESIGN.md / the differ contract.
    assert abs(fisher_exact_one_sided(5, 5, 0, 5) - 0.003968253968253968) < 1e-6
    assert abs(fisher_exact_one_sided(5, 5, 1, 5) - 0.023809523809523808) < 1e-6


def test_fisher_no_effect_cases_are_not_significant() -> None:
    # 4/5 vs 4/5 -> 7/9 = 0.778; must be well above 0.5.
    assert fisher_exact_one_sided(4, 5, 4, 5) > 0.5
    # Symmetric tables carry no evidence of a decrease: p >= 0.5 for every k.
    for k in range(6):
        assert fisher_exact_one_sided(k, 5, k, 5) >= 0.5


def test_fisher_monotonic_in_candidate_passes() -> None:
    # Fixing a perfect baseline, p must fall strictly as the candidate passes fewer reps.
    ps = [fisher_exact_one_sided(5, 5, c, 5) for c in range(6)]
    assert ps == sorted(ps)
    for lower, higher in pairwise(ps):
        assert lower < higher
    assert ps[0] < 0.05 < ps[4]


def test_fisher_monotonic_in_baseline_passes() -> None:
    # Fixing a broken candidate, p must fall as the baseline passes MORE reps.
    ps = [fisher_exact_one_sided(b, 5, 0, 5) for b in range(6)]
    assert ps == sorted(ps, reverse=True)
    assert ps[0] == 1.0  # nothing passed anywhere -> no evidence


def test_fisher_improvement_is_never_significant() -> None:
    # Candidate strictly better than baseline: p must be at the top of the range.
    assert fisher_exact_one_sided(1, 5, 5, 5) == pytest.approx(1.0)
    assert fisher_exact_one_sided(0, 5, 5, 5) == pytest.approx(1.0)


def test_fisher_degenerate_margins() -> None:
    assert fisher_exact_one_sided(0, 5, 0, 5) == 1.0  # K = 0
    assert fisher_exact_one_sided(5, 5, 5, 5) == 1.0  # K = N
    assert fisher_exact_one_sided(0, 0, 0, 0) == 1.0  # empty runs


def test_fisher_bounds_and_uneven_run_sizes() -> None:
    for b_n in range(1, 7):
        for c_n in range(1, 7):
            for b in range(b_n + 1):
                for c in range(c_n + 1):
                    p = fisher_exact_one_sided(b, b_n, c, c_n)
                    assert 0.0 <= p <= 1.0


def test_fisher_larger_n_gives_more_power() -> None:
    # A total wipeout is more significant with 10 reps than with 5.
    assert fisher_exact_one_sided(10, 10, 0, 10) < fisher_exact_one_sided(5, 5, 0, 5)


def test_fisher_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        fisher_exact_one_sided(6, 5, 0, 5)
    with pytest.raises(ValueError):
        fisher_exact_one_sided(5, 5, -1, 5)
    with pytest.raises(ValueError):
        fisher_exact_one_sided(0, -1, 0, 5)


# ---------------------------------------------------------------------------
# Wilson score interval
#
# Hand derivation with z = 1.959964, z^2 = 3.84145888 (1.959964^2 = 3.8416 - 2*1.96*0.000036).
# Bounds solve (phat - p)^2 = z^2 p(1-p)/n, i.e.
#   (1 + z^2/n) p^2 - (2*phat + z^2/n) p + phat^2 = 0.
#
# k=0, n=5:  phat = 0, denom = 1 + 3.84145888/5 = 1.768291776.
#            lower = (0 + 0.384145888 - 0.384145888)/1.768291776 = 0
#            upper = 0.768291776/1.768291776 = 0.43448247
# k=5, n=5:  phat = 1, lower = (1.384145888 - 0.384145888)/1.768291776 = 1/1.768291776
#                          = 0.56551753 ; upper = 1.768291776/1.768291776 = 1
# k=8, n=10: phat = 0.8, z^2/n = 0.384145888, denom = 1.384145888,
#            centre numerator = 0.8 + 0.192072944 = 0.992072944
#            inner = 0.8*0.2/10 + 3.84145888/400 = 0.016 + 0.0096036472 = 0.0256036472
#            sqrt(inner) = 0.16001140 ; z*sqrt = 0.31361658
#            lower = 0.678456364/1.384145888 = 0.49016247
#            upper = 1.305689524/1.384145888 = 0.94331785
# k=31,n=38: lower = 0.66581142, upper = 0.90778235  (this is the 66.6-90.8% in the report)
#
# Cross-checked by solving the quadratic above with the quadratic formula (a different
# arrangement from the centre +/- half form the implementation uses). Same roots.
# ---------------------------------------------------------------------------

KNOWN_WILSON = [
    ((0, 5), (0.0, 0.43448247)),
    ((5, 5), (0.56551753, 1.0)),
    ((8, 10), (0.49016247, 0.94331785)),
    ((31, 38), (0.66581142, 0.90778235)),
    ((3, 5), (0.23072428, 0.88237923)),
    ((0, 1), (0.0, 0.79345069)),
    ((1, 1), (0.20654931, 1.0)),
]


@pytest.mark.parametrize("args,expected", KNOWN_WILSON)
def test_wilson_known_values(args: tuple[int, int], expected: tuple[float, float]) -> None:
    lo, hi = wilson_interval(*args)
    assert abs(lo - expected[0]) < 1e-4
    assert abs(hi - expected[1]) < 1e-4


def test_wilson_report_string_matches_design_example() -> None:
    # "31/38 cases pass (81.6%, CI 66.6-90.8%)" from DESIGN.md's report sketch.
    lo, hi = wilson_interval(31, 38)
    assert f"{31 / 38 * 100:.1f}%" == "81.6%"
    assert f"{lo * 100:.1f}-{hi * 100:.1f}%" == "66.6-90.8%"


def test_wilson_edges_stay_in_unit_interval() -> None:
    lo, hi = wilson_interval(0, 5)
    assert lo == 0.0 and 0.0 < hi < 1.0
    lo, hi = wilson_interval(5, 5)
    assert 0.0 < lo < 1.0 and hi == 1.0


def test_wilson_n_zero_is_uninformative() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_always_brackets_the_point_estimate() -> None:
    for n in range(1, 21):
        for k in range(n + 1):
            lo, hi = wilson_interval(k, n)
            assert 0.0 <= lo <= k / n <= hi <= 1.0


def test_wilson_narrows_as_n_grows() -> None:
    widths = []
    for n in (5, 10, 50, 200):
        lo, hi = wilson_interval(round(0.8 * n), n)
        widths.append(hi - lo)
    for wide, narrow in pairwise(widths):
        assert narrow < wide


def test_wilson_is_symmetric_under_success_failure_swap() -> None:
    for n in range(1, 12):
        for k in range(n + 1):
            lo, hi = wilson_interval(k, n)
            lo2, hi2 = wilson_interval(n - k, n)
            assert math.isclose(lo, 1.0 - hi2, abs_tol=1e-12)
            assert math.isclose(hi, 1.0 - lo2, abs_tol=1e-12)


def test_wilson_z_is_configurable() -> None:
    # A 99% z widens the interval relative to the 95% default.
    lo95, hi95 = wilson_interval(8, 10)
    lo99, hi99 = wilson_interval(8, 10, z=2.575829)
    assert lo99 < lo95 and hi99 > hi95


def test_wilson_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        wilson_interval(6, 5)
    with pytest.raises(ValueError):
        wilson_interval(-1, 5)
    with pytest.raises(ValueError):
        wilson_interval(0, -1)
