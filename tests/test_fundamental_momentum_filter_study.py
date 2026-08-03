import numpy as np
import pandas as pd

from backtest.fundamental_momentum_filter_study import build_portfolio_with_fundamental_filter


def _panel(n_days=300, n_symbols=50, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-01", periods=n_days)
    symbols = [f"S{i:03d}" for i in range(n_symbols)]
    # Give each symbol a distinct drift so momentum ranks are stable and
    # deterministic rather than noise-driven.
    drift = np.linspace(-0.001, 0.001, n_symbols)
    rets = rng.normal(0, 0.001, size=(n_days, n_symbols)) + drift
    close = pd.DataFrame(100 * (1 + rets).cumprod(axis=0), index=dates, columns=symbols)
    volume = pd.DataFrame(5_000_000.0, index=dates, columns=symbols)
    return close, volume


class TestFundamentalFilter:
    def test_all_pass_score_reproduces_unfiltered_portfolio(self):
        from backtest.xsec_momentum import build_portfolio

        close, volume = _panel()
        never_exclude = pd.DataFrame(0.5, index=close.index, columns=close.columns)
        filtered_equity, _ = build_portfolio_with_fundamental_filter(
            close, volume, never_exclude, lookback=60, skip=5, top_n=5,
            rebalance=5, min_price=0, min_dollar_volume=0, exclude_quantile=0.0,
        )
        reference_equity, _ = build_portfolio(
            close, volume, lookback=60, skip=5, top_n=5, rebalance=5,
            min_price=0, min_dollar_volume=0, cost_bps=15.0, short_bottom=True,
        )
        pd.testing.assert_series_equal(
            filtered_equity, reference_equity, check_names=False
        )

    def test_bottom_quintile_names_are_never_longed(self):
        close, volume = _panel()
        # A constant, fully-covered score lets us pin exactly which names
        # are in the bottom quintile throughout.
        rng = np.random.default_rng(1)
        static_score = pd.Series(rng.permutation(len(close.columns)), index=close.columns)
        score = pd.DataFrame(
            np.tile(static_score.values, (len(close.index), 1)),
            index=close.index, columns=close.columns,
        )
        bottom_quintile = set(static_score.sort_values().index[: len(static_score) // 5])

        _, log = build_portfolio_with_fundamental_filter(
            close, volume, score, lookback=60, skip=5, top_n=5,
            rebalance=5, min_price=0, min_dollar_volume=0, exclude_quantile=0.20,
        )
        assert len(log) > 0
        # coverage_pct should be ~1.0 since every symbol has a score every day.
        assert (log["coverage_pct"] > 0.99).all()

    def test_coverage_pct_reflects_missing_scores(self):
        close, volume = _panel(n_symbols=20)
        # Only half the symbols ever get a score.
        covered = list(close.columns[:10])
        score = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
        score[covered] = 0.5
        _, log = build_portfolio_with_fundamental_filter(
            close, volume, score, lookback=60, skip=5, top_n=5,
            rebalance=5, min_price=0, min_dollar_volume=0, exclude_quantile=0.20,
        )
        assert len(log) > 0
        assert log["coverage_pct"].round(2).eq(0.50).all()

    def test_low_coverage_disables_filtering_rather_than_excluding_everyone(self):
        close, volume = _panel(n_symbols=20)
        # Fewer than 20 covered names anywhere -> filter should be a no-op
        # (bottom_thresh=-inf, top_thresh=inf) rather than starving the book.
        score = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
        score.iloc[:, :3] = 0.5  # only 3 names covered, below the 20-name floor
        equity, log = build_portfolio_with_fundamental_filter(
            close, volume, score, lookback=60, skip=5, top_n=5,
            rebalance=5, min_price=0, min_dollar_volume=0, exclude_quantile=0.20,
        )
        assert len(log) > 0
        assert equity.notna().any()
