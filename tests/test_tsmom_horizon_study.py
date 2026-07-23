import pandas as pd

from backtest.tsmom_horizon_study import trend_stream


def test_trend_stream_does_not_use_future_prices():
    index = pd.date_range("2020-01-01", periods=400)
    close = pd.DataFrame({"X": range(100, 500)}, index=index, dtype=float)
    changed = close.copy()
    changed.iloc[-1, 0] = 10_000
    original_returns = trend_stream(close, "ensemble fractional long_flat")
    changed_returns = trend_stream(changed, "ensemble fractional long_flat")
    pd.testing.assert_series_equal(
        original_returns.iloc[:-1], changed_returns.iloc[:-1]
    )
