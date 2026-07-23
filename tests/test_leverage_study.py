import pandas as pd

from backtest.leverage_study import volatility_targeted


def test_volatility_target_has_no_future_lookahead():
    index = pd.date_range("2024-01-01", periods=100)
    original = pd.Series([0.01, -0.01] * 50, index=index)
    changed = original.copy()
    changed.iloc[-1] = 0.50

    _, lev_original = volatility_targeted(original, 0.15)
    _, lev_changed = volatility_targeted(changed, 0.15)
    pd.testing.assert_series_equal(
        lev_original.iloc[:-1], lev_changed.iloc[:-1]
    )


def test_leverage_is_clipped_to_configured_bounds():
    index = pd.date_range("2024-01-01", periods=100)
    raw = pd.Series([0.001, -0.001] * 50, index=index)
    _, leverage = volatility_targeted(
        raw, 0.15, min_leverage=0.75, max_leverage=1.25
    )
    assert leverage.min() >= 0.75
    assert leverage.max() <= 1.25
