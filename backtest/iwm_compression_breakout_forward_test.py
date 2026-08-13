"""Pre-registered forward-test monitor for IWM compression breakout.

reports/intraday_strategy_study.json rejected this family across all four
ETF/cost combinations under a fixed gate (>=30 trades, positive mean
return, profit factor >1 at 5bp/leg, both windows) — but IWM specifically
was profitable in BOTH windows at the 5bp stress cost (design: 18 trades,
PF 1.085; screen: 24 trades, PF 1.375) and was rejected purely on trade
count, not on sign or magnitude. At its observed firing rate (~18-24 trades
per ~13-15 months, roughly 1.5/month), reaching a fixed N=30 threshold
takes on the order of 20 months — a fixed-N gate is the wrong tool for a
low-frequency signal like this one: it either waits an unreasonably long
time or (if run early) is statistically underpowered either way.

This module implements a Sequential Probability Ratio Test (SPRT) instead
— it can stop EARLIER than a fixed-N rule if the effect is strong, and
never has to wait for an arbitrary count if the effect is weak, while
keeping a pre-declared, honest false-positive/false-negative rate. Nothing
in this module can be evaluated yet: real forward trades only exist once
they are collected on IWM's compression-breakout signal starting
2026-08-13 (the newly re-frozen final-validation window per AGENTS.md) —
before that date this repo has no legitimate forward-test data to feed it.
Do not backfill this test with data from before 2026-08-13; that is
exactly the frozen-window violation AGENTS.md's own "2026-08-04 frozen
window was substantially spent" section documents and warns against.

See reports/iwm_compression_breakout_forward_test_registration.json for
the full pre-registration (hypotheses, cost convention, variance estimate
and its provenance, monitoring cadence, abandonment ceiling).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Pre-declared, NOT tuned after seeing forward data — see the registration
# JSON. Standard defaults: 5% false-positive rate (declaring the strategy
# works when it doesn't), 20% false-negative rate (80% power).
ALPHA = 0.05
BETA = 0.20

# H0: the strategy's true mean per-trade return (net of the 5bp/leg stress
# cost, matching intraday_strategy_study.py's own convention) is <= 0.
MU0 = 0.0
# H1: the true mean per-trade return matches the screen-window's observed
# effect, 0.00052/trade — the more recent, larger of the two historical
# estimates, chosen as the alternative to test against so the boundary is
# not tuned to whichever number makes acceptance easiest.
MU1 = 0.00052

# Per-trade return standard deviation, back-solved from the design-window's
# summary statistics (18 trades, mean_return=9.8e-05, profit_factor=1.085,
# win_rate=0.50 at 5bp/leg): assuming a simple two-point win/loss model,
# win and loss magnitudes W, L solve win_rate*W = profit_factor *
# (1-win_rate)*L and (win_rate*W - (1-win_rate)*L) = mean_return, giving
# W~=0.00251, L~=0.00231; SD of a symmetric-ish two-point mixture near
# those magnitudes is approximated here as their average. This is a rough
# PLANNING estimate only, not a measured value — refit it from the actual
# design-window (pre-2026-08-13) trade-level returns, not the forward
# sample this test is monitoring, before this test is first run for real.
SIGMA_ESTIMATE = 0.0024


@dataclass(frozen=True)
class SprtBoundaries:
    upper: float  # cross this (accept H1: the effect is real) -> promote to shadow
    lower: float  # cross this (accept H0: no effect) -> reject


def sprt_boundaries(alpha: float = ALPHA, beta: float = BETA) -> SprtBoundaries:
    """Wald's SPRT decision boundaries on the cumulative log-likelihood ratio."""
    return SprtBoundaries(
        upper=math.log((1 - beta) / alpha),
        lower=math.log(beta / (1 - alpha)),
    )


def sprt_log_likelihood_ratio(
    trade_returns: list[float],
    *,
    mu0: float = MU0,
    mu1: float = MU1,
    sigma: float = SIGMA_ESTIMATE,
) -> float:
    """Cumulative log-likelihood ratio of H1 (mean=mu1) vs H0 (mean=mu0)
    for a sequence of per-trade returns, assumed approximately Normal(*,
    sigma) — the standard SPRT construction for a mean-shift test. Pure
    function: takes whatever trades have been observed so far, returns one
    number; the caller decides continue/stop from sprt_boundaries()."""
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")
    llr = 0.0
    for r in trade_returns:
        # log[N(r; mu1, sigma) / N(r; mu0, sigma)], simplifies to a linear
        # term in r since sigma is shared between the two hypotheses.
        llr += (mu1 - mu0) * (r - (mu0 + mu1) / 2) / (sigma ** 2)
    return llr


def sprt_decision(llr: float, boundaries: SprtBoundaries) -> str:
    if llr >= boundaries.upper:
        return "accept_h1_promote_to_shadow"
    if llr <= boundaries.lower:
        return "accept_h0_reject"
    return "continue_monitoring"


def monitor(trade_returns: list[float]) -> dict:
    """Run the current forward sample through the SPRT and report the
    decision plus enough context to audit it. `trade_returns` should be
    every completed IWM-compression-breakout forward trade's return (net
    of the 5bp/leg stress cost) since 2026-08-13, in execution order."""
    boundaries = sprt_boundaries()
    llr = sprt_log_likelihood_ratio(trade_returns)
    return {
        "trades_observed": len(trade_returns),
        "cumulative_llr": round(llr, 4),
        "boundaries": {"upper": round(boundaries.upper, 4), "lower": round(boundaries.lower, 4)},
        "decision": sprt_decision(llr, boundaries),
        "parameters": {"alpha": ALPHA, "beta": BETA, "mu0": MU0, "mu1": MU1, "sigma_estimate": SIGMA_ESTIMATE},
    }


def main() -> None:
    """No forward trades exist yet as of 2026-08-12 — this prints the
    boundaries and an empty-sample decision so the monitoring machinery is
    verified end-to-end before there is anything real to feed it."""
    import json

    print(json.dumps(monitor([]), indent=2))


if __name__ == "__main__":
    main()
