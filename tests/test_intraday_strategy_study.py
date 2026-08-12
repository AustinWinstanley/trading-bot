import pandas as pd

from backtest.intraday import prepare_bars
from backtest.intraday_strategy_study import (
    compression_breakout_signal,
    gap_continuation_signal,
    opening_range_signal,
)


def _day(start, closes, prior=None):
    index = pd.date_range(start, periods=len(closes), freq="5min", tz="UTC")
    return pd.DataFrame({
        "open": closes, "high": [v + .1 for v in closes],
        "low": [v - .1 for v in closes], "close": closes,
        "volume": [100] * len(closes),
    }, index=index)


def test_opening_range_signal_waits_until_range_is_complete():
    bars = prepare_bars(_day("2026-07-20 13:30", [100] * 6 + [101] + [101] * 5))
    bars.iloc[6, bars.columns.get_loc("volume")] = 200
    signal = opening_range_signal(bars)
    assert not signal.iloc[:6].any()
    assert signal.iloc[6] == 1


def test_compression_requires_narrow_opening_range():
    bars = prepare_bars(_day("2026-07-20 13:30", [100] * 6 + [100.5] + [100.5] * 5))
    assert compression_breakout_signal(bars).iloc[6] == 1


def test_gap_signal_uses_prior_session_close_at_checkpoint():
    first = _day("2026-07-20 13:30", [100] * 12)
    second = _day("2026-07-21 13:30", [101] * 5 + [102] + [102] * 6)
    bars = prepare_bars(pd.concat([first, second]))
    signal = gap_continuation_signal(bars)
    second_rows = signal[bars["session"] == "2026-07-21"]
    assert second_rows.iloc[5] == 1
