import pandas as pd

from backtest.deployable_momentum import build_deployable_stream


def _matrices():
    index = pd.date_range("2024-01-01", periods=18, freq="B")
    close = pd.DataFrame({
        symbol: [20 + rank + (rank - 4) * i / 6 for i in range(len(index))]
        for rank, symbol in enumerate("ABCDEFGHIJ")
    }, index=index)
    volume = pd.DataFrame(1_000_000.0, index=index, columns=close.columns)
    return close, volume


def test_whole_share_short_targets_never_create_fractional_shorts():
    close, volume = _matrices()
    result, _ = build_deployable_stream(
        close, volume, lookback=2, skip=1, long_n=2, short_n=2,
        rebalance=2, min_price=0, min_dollar_volume=0,
    )
    # Convert normalized weights back to account-dollar quantity at $10k and
    # the 0.30 multiplier used above.
    for date, row in result.weights.iterrows():
        for symbol, weight in row[row < 0].items():
            qty = abs(weight) * 10_000 * 0.30 / close.at[date, symbol]
            assert abs(qty - round(qty)) < 1e-9


def test_target_restoration_bypasses_only_loss_based_rejection():
    close, volume = _matrices()
    # Force the strongest long to fall enough after entry that restoring its
    # fixed dollar target clears the $25 minimum while it remains selected.
    close.loc[close.index[-5]:, "J"] *= 0.35
    control, control_diag = build_deployable_stream(
        close, volume, lookback=10, skip=1, long_n=1, short_n=1,
        rebalance=100, min_price=0, min_dollar_volume=0,
        allow_target_restoration_at_loss=False,
    )
    candidate, candidate_diag = build_deployable_stream(
        close, volume, lookback=10, skip=1, long_n=1, short_n=1,
        rebalance=100, min_price=0, min_dollar_volume=0,
        allow_target_restoration_at_loss=True,
    )
    assert control_diag.rejected_restorations > 0
    assert candidate_diag.rejected_restorations == 0
    assert candidate_diag.trades > control_diag.trades


def test_capacity_matching_does_not_target_more_longs_than_shorts():
    close, volume = _matrices()
    result, _ = build_deployable_stream(
        close, volume, lookback=2, skip=1, long_n=2, short_n=2,
        rebalance=2, min_price=0, min_dollar_volume=0,
        match_long_to_short_capacity=True,
    )
    live = result.weights[result.weights.abs().sum(axis=1) > 0]
    assert len(live) > 0
    long_gross = live.clip(lower=0).sum(axis=1)
    short_gross = -live.clip(upper=0).sum(axis=1)
    # Drift and no-averaging can create small path differences after entry;
    # the initial capacity-matched targets themselves cannot be net long.
    assert long_gross.iloc[0] <= short_gross.iloc[0] + 1e-9
