import pandas as pd

from backtest.anticipatory_tail_hedge_study import (
    black_scholes_put,
    calm_signal,
    modeled_spread_value,
    synthetic_overlay,
)


def test_put_value_converges_to_intrinsic_at_expiry():
    assert black_scholes_put(80, 100, 0, 0.20) == 20
    assert black_scholes_put(120, 100, 0, 0.20) == 0


def test_put_spread_value_is_bounded_by_width():
    value = modeled_spread_value(
        100,
        long_strike=90,
        short_strike=85,
        days_to_expiry=45,
        vix=18,
    )
    assert 0 < value < 5


def test_calm_signal_uses_only_prior_observations():
    index = pd.bdate_range("2025-01-01", periods=240)
    original = pd.Series(
        [100 + 0.05 * number for number in range(240)],
        index=index,
    )
    changed = original.copy()
    changed.iloc[-1] = 1
    assert calm_signal(original, index[-1]) == calm_signal(
        changed, index[-1]
    )


def test_synthetic_overlay_respects_annual_budget():
    index = pd.bdate_range("2025-01-01", "2025-12-31")
    spy = pd.Series(
        [100 + 0.03 * number for number in range(len(index))],
        index=index,
    )
    portfolio = pd.Series(0.0, index=index)
    vix = pd.Series(12.0, index=index)
    _, logs = synthetic_overlay(
        portfolio, spy, vix,
        starting_equity=10_000,
        per_trade_budget=0.02,
        annual_budget=0.04,
    )
    completed = [
        row for row in logs if "maximum_loss_dollars" in row
    ]
    assert len(completed) == 2
    assert sum(
        row["maximum_loss_dollars"] for row in completed
    ) <= 401
