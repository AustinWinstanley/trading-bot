import pandas as pd

from backtest.long_history_risk_overlay_study import de_risk_fixed_2x


def test_overlay_has_no_future_lookahead():
    index = pd.date_range("2020-01-01", periods=150)
    original = pd.Series([0.01, -0.01] * 75, index=index)
    changed = original.copy()
    changed.iloc[-1] = 0.50
    _, original_leverage = de_risk_fixed_2x(original, 0.15)
    _, changed_leverage = de_risk_fixed_2x(changed, 0.15)
    pd.testing.assert_series_equal(
        original_leverage.iloc[:-1], changed_leverage.iloc[:-1]
    )


def test_overlay_never_exceeds_fixed_2x_or_configured_floor():
    index = pd.date_range("2020-01-01", periods=200)
    returns = pd.Series([0.10, -0.10] * 100, index=index)
    _, leverage = de_risk_fixed_2x(returns, 0.12, min_scale=0.30)
    assert leverage.max() <= 2.0
    assert leverage.min() >= 0.60


def test_turnover_cost_can_only_reduce_overlay_return():
    index = pd.date_range("2020-01-01", periods=200)
    returns = pd.Series(([0.001, -0.001] * 50) + ([0.05, -0.05] * 50), index=index)
    free, _ = de_risk_fixed_2x(returns, 0.15, cost_bps=0)
    costly, _ = de_risk_fixed_2x(returns, 0.15, cost_bps=10)
    assert (costly <= free + 1e-15).all()
    assert costly.sum() < free.sum()
