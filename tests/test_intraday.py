import pandas as pd

from backtest.intraday import prepare_bars, simulate_fixed_horizon, trade_summary


def _bars():
    index = pd.date_range("2026-07-20 13:30", periods=6, freq="5min", tz="UTC")
    return pd.DataFrame({
        "open": [100, 101, 102, 103, 104, 105],
        "high": [101, 102, 103, 104, 105, 106],
        "low": [99, 100, 101, 102, 103, 104],
        "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
        "volume": [10] * 6,
    }, index=index)


def test_prepare_bars_adds_session_coordinates_and_vwap():
    bars = prepare_bars(_bars())
    assert list(bars["session_bar"]) == list(range(6))
    assert bars["session"].nunique() == 1
    assert bars["session_vwap"].notna().all()


def test_simulator_enters_only_after_completed_signal_bar():
    bars = prepare_bars(_bars())
    signal = pd.Series(0, index=bars.index)
    signal.iloc[1] = 1
    trades = simulate_fixed_horizon(bars, signal, hold_bars=2, cost_bps_per_leg=0)
    assert trades.iloc[0]["signal_ts"] == bars.index[1].isoformat()
    assert trades.iloc[0]["entry_ts"] == bars.index[2].isoformat()
    assert trades.iloc[0]["exit_ts"] == bars.index[3].isoformat()


def test_simulator_takes_only_first_signal_and_charges_two_legs():
    bars = prepare_bars(_bars())
    signal = pd.Series(1, index=bars.index)
    trades = simulate_fixed_horizon(bars, signal, hold_bars=1, cost_bps_per_leg=2)
    assert len(trades) == 1
    expected_gross = bars.iloc[1]["close"] / bars.iloc[1]["open"] - 1
    assert trades.iloc[0]["net_return"] == expected_gross - 0.0004
    assert trade_summary(trades)["trades"] == 1
