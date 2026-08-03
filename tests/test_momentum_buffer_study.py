import pandas as pd
import pytest

from backtest.momentum_buffer_study import buffered_members, build_buffer_stream


def test_buffer_retains_incumbent_inside_hold_band():
    ranked = pd.Series(range(6), index=list("ABCDEF"))
    assert buffered_members(
        ranked, ["C", "A"], side="long", enter_n=2, hold_n=3
    ) == ["C", "A"]
    assert buffered_members(
        ranked, ["D", "F"], side="short", enter_n=2, hold_n=3
    ) == ["D", "F"]


def test_buffer_replaces_incumbent_outside_hold_band():
    ranked = pd.Series(range(6), index=list("ABCDEF"))
    assert buffered_members(
        ranked, ["D", "A"], side="long", enter_n=2, hold_n=3
    ) == ["A", "B"]
    assert buffered_members(
        ranked, ["C", "F"], side="short", enter_n=2, hold_n=3
    ) == ["F", "E"]


def test_hold_band_cannot_be_smaller_than_entry_band():
    ranked = pd.Series(range(4), index=list("ABCD"))
    with pytest.raises(ValueError):
        buffered_members(
            ranked, [], side="long", enter_n=3, hold_n=2
        )


def test_stream_reports_leg_turnover_and_whole_share_capacity():
    index = pd.date_range("2024-01-01", periods=12, freq="B")
    close = pd.DataFrame(
        {
            symbol: [10 + rank + (rank - 3) * i / 20 for i in range(12)]
            for rank, symbol in enumerate("ABCDEFGH")
        },
        index=index,
    )
    volume = pd.DataFrame(1_000_000.0, index=index, columns=close.columns)
    result = build_buffer_stream(
        close,
        volume,
        account_equity=1_000,
        account_multiplier=0.30,
        enter_n=1,
        hold_n=2,
        lookback=2,
        skip=1,
        rebalance=2,
        min_price=0,
        min_dollar_volume=0,
    )
    assert result.weights.index.equals(close.index)
    assert result.long_turnover.sum() > 0
    assert result.short_turnover.sum() > 0
    assert (result.short_gross <= 0.5).all()
