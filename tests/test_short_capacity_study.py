import pandas as pd

from backtest.short_capacity_study import (
    build_capacity_stream,
    capacity_summary,
)


def _matrices():
    index = pd.date_range("2024-01-01", periods=20, freq="B")
    close = pd.DataFrame(
        {
            "CHEAP": [22.0 - 2.0 * i / 19 for i in range(20)],
            "MID": [80.0 - 20.0 * i / 19 for i in range(20)],
            "HIGH": [200.0 - 100.0 * i / 19 for i in range(20)],
            "LONG": [10.0 + 10.0 * i / 19 for i in range(20)],
        },
        index=index,
    )
    volume = pd.DataFrame(1_000_000.0, index=index, columns=close.columns)
    return close, volume


def test_ranked_whole_share_shorts_do_not_redistribute_unused_budget():
    close, volume = _matrices()
    result = build_capacity_stream(
        close,
        volume,
        account_equity=1_000,
        account_multiplier=0.30,
        lookback=2,
        skip=1,
        long_n=1,
        short_n=2,
        rebalance=2,
        min_price=0,
        min_dollar_volume=0,
        selection="ranked",
    )
    first = result.rebalances.iloc[0]
    assert first["desired_dollars_per_short"] == 75.0
    assert first["zero_share_targets"] == 1
    assert 0 < first["realized_short_dollars"] < 75.0


def test_price_aware_selection_scans_for_affordable_names():
    close, volume = _matrices()
    result = build_capacity_stream(
        close,
        volume,
        account_equity=1_000,
        account_multiplier=0.30,
        lookback=2,
        skip=1,
        long_n=1,
        short_n=2,
        rebalance=2,
        min_price=0,
        min_dollar_volume=0,
        selection="price_aware",
    )
    logs = result.rebalances
    assert (logs["zero_share_targets"] == 0).all()
    assert (logs["selected_shorts"] == 2).all()


def test_capacity_summary_reports_account_level_short_gross():
    close, volume = _matrices()
    result = build_capacity_stream(
        close,
        volume,
        account_equity=10_000,
        account_multiplier=0.30,
        lookback=2,
        skip=1,
        long_n=1,
        short_n=2,
        rebalance=2,
        min_price=0,
        min_dollar_volume=0,
        selection="fractional",
    )
    summary = capacity_summary(result, profile="base", label="test")
    assert summary["target_short_gross"] == 0.15
    assert summary["average_realized_short_gross"] == 0.15
    assert summary["average_capacity_pct"] == 100.0
