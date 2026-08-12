import pandas as pd

from backtest.tsmom_liquidity_alignment_study import build_tsmom


def test_liquidity_filter_removes_illiquid_positive_signal_before_normalizing():
    dates = pd.date_range("2020-01-01", periods=320, freq="B")
    close = pd.DataFrame({
        "LIQUID": [100 + i for i in range(len(dates))],
        "THIN": [50 + i / 2 for i in range(len(dates))],
    }, index=dates)
    volume = pd.DataFrame({"LIQUID": 100_000.0, "THIN": 10.0}, index=dates)
    _, weights = build_tsmom(close, volume, liquidity_floor=1_000_000)
    live = weights.iloc[-1]
    assert live["LIQUID"] == 1.0
    assert live["THIN"] == 0.0
