import pandas as pd

from backtest.residual_momentum_study import residual_signal


def test_residual_signal_does_not_change_before_future_shock():
    index = pd.date_range("2020-01-01", periods=400)
    benchmark = pd.Series(range(100, 500), index=index, dtype=float)
    close = pd.DataFrame({"X": range(120, 520)}, index=index, dtype=float)
    changed = close.copy()
    changed.iloc[-1, 0] = 10_000
    original = residual_signal(close, benchmark)
    shocked = residual_signal(changed, benchmark)
    pd.testing.assert_frame_equal(original.iloc[:-1], shocked.iloc[:-1])
