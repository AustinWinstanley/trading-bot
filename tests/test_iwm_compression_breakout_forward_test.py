"""Tests for the pre-registered SPRT forward-test monitor
(backtest/iwm_compression_breakout_forward_test.py). No real forward data
exists yet (trades only start accruing 2026-08-13) — these tests verify
the SPRT machinery itself is correct, using synthetic samples."""

from __future__ import annotations

import math

import numpy as np
import pytest

from backtest.iwm_compression_breakout_forward_test import (
    MU0,
    MU1,
    SIGMA_ESTIMATE,
    monitor,
    sprt_boundaries,
    sprt_decision,
    sprt_log_likelihood_ratio,
)


def test_boundaries_match_wald_formula():
    boundaries = sprt_boundaries(alpha=0.05, beta=0.20)
    assert boundaries.upper == pytest.approx(math.log(0.80 / 0.05))
    assert boundaries.lower == pytest.approx(math.log(0.20 / 0.95))
    assert boundaries.upper > 0 > boundaries.lower


def test_empty_sample_is_zero_llr_and_continues():
    boundaries = sprt_boundaries()
    llr = sprt_log_likelihood_ratio([])
    assert llr == 0.0
    assert sprt_decision(llr, boundaries) == "continue_monitoring"


def test_llr_is_positive_when_returns_favor_h1():
    # Every trade return sits exactly at mu1 -> strictly favors H1 over H0.
    trades = [MU1] * 20
    llr = sprt_log_likelihood_ratio(trades)
    assert llr > 0


def test_llr_is_negative_when_returns_favor_h0():
    trades = [MU0] * 20
    llr = sprt_log_likelihood_ratio(trades)
    assert llr < 0


def test_llr_is_zero_at_the_midpoint():
    midpoint = (MU0 + MU1) / 2
    trades = [midpoint] * 10
    assert sprt_log_likelihood_ratio(trades) == pytest.approx(0.0, abs=1e-9)


def test_llr_accumulates_additively_trade_by_trade():
    trades = [0.001, -0.0005, 0.0007, 0.0002]
    total = sprt_log_likelihood_ratio(trades)
    running = 0.0
    for i in range(1, len(trades) + 1):
        running = sprt_log_likelihood_ratio(trades[:i])
    assert running == pytest.approx(total)
    # And it's literally a sum of independent per-trade contributions.
    per_trade_sum = sum(sprt_log_likelihood_ratio([r]) for r in trades)
    assert per_trade_sum == pytest.approx(total)


def test_rejects_non_positive_sigma():
    with pytest.raises(ValueError, match="sigma must be positive"):
        sprt_log_likelihood_ratio([0.001], sigma=0.0)


def test_a_strong_sustained_effect_eventually_crosses_the_upper_boundary():
    # A per-trade return well above mu1, sustained, must cross accept-H1
    # well within a bounded number of trades (not "never").
    trades = [MU1 * 3] * 200
    result = monitor(trades)
    assert result["decision"] == "accept_h1_promote_to_shadow"
    assert result["trades_observed"] == 200


def test_a_sustained_zero_effect_eventually_crosses_the_lower_boundary():
    trades = [0.0] * 200
    result = monitor(trades)
    assert result["decision"] == "accept_h0_reject"


def test_monitor_reports_parameters_for_auditability():
    result = monitor([0.0001, 0.0002])
    assert result["parameters"] == {
        "alpha": 0.05, "beta": 0.20, "mu0": MU0, "mu1": MU1,
        "sigma_estimate": SIGMA_ESTIMATE,
    }


def test_true_h1_effect_size_resolves_within_a_bounded_horizon_on_average():
    """Sanity check the pre-registered parameters are actually usable —
    simulating trades drawn from the true H1 effect should resolve
    (either direction) within a few hundred trades on average, not run
    forever. This does not assert WHICH decision (a specific random seed
    could land either way for a modest effect size) — only that the test
    terminates in bounded time, confirming the boundaries are reachable."""
    rng = np.random.default_rng(42)
    boundaries = sprt_boundaries()
    trades = []
    for _ in range(2000):
        trades.append(float(rng.normal(MU1, SIGMA_ESTIMATE)))
        llr = sprt_log_likelihood_ratio(trades)
        if sprt_decision(llr, boundaries) != "continue_monitoring":
            break
    else:
        pytest.fail("SPRT did not resolve within 2000 simulated trades")
    assert len(trades) < 2000
