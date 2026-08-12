import pandas as pd

from backtest.deployable_momentum import build_deployable_stream


def test_narrower_breadth_increases_short_slot_capacity():
    index = pd.date_range("2024-01-01", periods=18, freq="B")
    symbols = [f"S{i:02d}" for i in range(24)]
    close = pd.DataFrame(
        {
            symbol: [60 + rank + (rank - 12) * day / 20 for day in range(len(index))]
            for rank, symbol in enumerate(symbols)
        },
        index=index,
    )
    volume = pd.DataFrame(1_000_000.0, index=index, columns=symbols)
    wide, _ = build_deployable_stream(
        close, volume, lookback=2, skip=1, long_n=10, short_n=10,
        rebalance=2, min_price=0, min_dollar_volume=0,
    )
    narrow, _ = build_deployable_stream(
        close, volume, lookback=2, skip=1, long_n=5, short_n=5,
        rebalance=2, min_price=0, min_dollar_volume=0,
    )
    wide_short = -wide.weights.clip(upper=0).sum(axis=1).mean()
    narrow_short = -narrow.weights.clip(upper=0).sum(axis=1).mean()
    assert narrow_short > wide_short
