"""Statistics for upshift, stdlib only (no scipy). See DESIGN.md "Statistics".

Two functions, both exact/closed-form so a reviewer can re-derive any number in the report
by hand:

- ``fisher_exact_one_sided`` — is the candidate's pass rate LOWER than the baseline's?
- ``wilson_interval``       — score interval for a suite-level pass rate.

Nothing here knows about runs, cases or models; it takes counts and returns numbers.
"""

from __future__ import annotations

import math

# 1.959964 is the two-sided 95% normal quantile (z_{0.975}).
Z_95 = 1.959964


def fisher_exact_one_sided(b_pass: int, b_n: int, c_pass: int, c_n: int) -> float:
    """One-sided Fisher exact test for "the candidate pass rate is LOWER than the baseline".

    The 2x2 table is::

                    pass          fail
        baseline    b_pass        b_n - b_pass
        candidate   c_pass        c_n - c_pass

    Conditioning on both margins, the number of passes in the *candidate* column is
    hypergeometric::

        N = b_n + c_n            (all reps in both runs)
        K = b_pass + c_pass      (total successes, the column margin)
        n = c_n                  (candidate row margin, the number of draws)
        X ~ Hypergeometric(N, K, n)

        P(X = x) = C(K, x) * C(N - K, n - x) / C(N, n)

    The p-value is the probability of a candidate result at least as extreme (i.e. at least
    as few passes) as the one observed::

        p = sum_{x = max(0, n - (N - K))}^{c_pass} P(X = x)

    The lower summation bound is the support floor: the candidate cannot draw fewer than
    ``n - (N - K)`` successes when there are not enough failures to fill its row. The result
    is clamped with ``min(p, 1.0)`` so floating-point error can never emit p > 1.

    Degenerate margins (``K == 0``, ``K == N``, or an empty run) give p = 1.0: with no
    variation left there is no evidence of a difference.

    Returns a float in [0, 1]. p is small only when the candidate passed *less* often than
    the baseline; an improvement yields a p close to 1.
    """
    if b_n < 0 or c_n < 0:
        raise ValueError("run sizes must be non-negative")
    if not (0 <= b_pass <= b_n) or not (0 <= c_pass <= c_n):
        raise ValueError("pass counts must satisfy 0 <= k <= n")

    total_n = b_n + c_n
    successes = b_pass + c_pass
    draws = c_n
    if total_n == 0 or draws == 0 or successes == 0 or successes == total_n:
        return 1.0

    denom = math.comb(total_n, draws)
    lo = max(0, draws - (total_n - successes))
    p = 0.0
    for x in range(lo, c_pass + 1):
        p += math.comb(successes, x) * math.comb(total_n - successes, draws - x) / denom
    return min(p, 1.0)


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion k/n.

    With ``p = k / n``::

        centre = (p + z^2 / (2n)) / (1 + z^2 / n)
        half   = (z * sqrt( p(1-p)/n + z^2 / (4n^2) )) / (1 + z^2 / n)
        (lower, upper) = (centre - half, centre + half)

    Unlike the normal-approximation interval this never leaves [0, 1] and stays sensible at
    k = 0 and k = n, which is why the report uses it for suite pass rates with N as small as
    5 reps. ``n == 0`` returns the uninformative (0.0, 1.0). The default z is the two-sided
    95% normal quantile, so this is a 95% interval.

    The bounds at k = 0 and k = n are exactly 0.0 and 1.0 respectively; they are pinned
    rather than computed so floating-point error cannot report an upper bound of
    0.9999999999999999 for a case that passed every rep.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return (0.0, 1.0)
    if not 0 <= k <= n:
        raise ValueError("pass counts must satisfy 0 <= k <= n")

    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denom
    lower = 0.0 if k == 0 else max(0.0, centre - half)
    upper = 1.0 if k == n else min(1.0, centre + half)
    return (lower, upper)
