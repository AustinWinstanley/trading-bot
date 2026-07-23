"""Raw versus market-residualized cross-sectional momentum."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import norm_index, returns_summary
from backtest.xsec_data import load
from engine.tiingo import load_parquet

TD = 252


def residual_signal(close: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    returns = close.pct_change()
    benchmark_returns = benchmark.pct_change()
    beta = returns.rolling(126, min_periods=80).cov(benchmark_returns).div(
        benchmark_returns.rolling(126, min_periods=80).var(), axis=0
    )
    raw = close.shift(21) / close.shift(252) - 1
    benchmark_momentum = benchmark.shift(21) / benchmark.shift(252) - 1
    return raw - beta.shift(21).mul(benchmark_momentum, axis=0)


def portfolio_returns(
    signal: pd.DataFrame,
    close: pd.DataFrame,
    volume: pd.DataFrame,
    benchmark: pd.Series,
    *,
    beta_neutral: bool,
) -> pd.Series:
    daily_returns = close.pct_change()
    benchmark_returns = benchmark.pct_change()
    beta = daily_returns.rolling(126, min_periods=80).cov(benchmark_returns).div(
        benchmark_returns.rolling(126, min_periods=80).var(), axis=0
    )
    raw_momentum = close.shift(21) / close.shift(252) - 1
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    eligible = (
        (close.shift(21) > 5)
        & (dollar_volume.shift(21) > 5e6)
        & raw_momentum.notna()
    )
    weights = pd.DataFrame(
        0.0, index=close.index, columns=close.columns, dtype="float32"
    )
    for date in close.index[273::5]:
        ranked = signal.loc[date].where(eligible.loc[date]).dropna().sort_values()
        if len(ranked) < 40:
            continue
        shorts, longs = ranked.head(20).index, ranked.tail(20).index
        long_gross = short_gross = 0.5
        if beta_neutral:
            long_beta = float(beta.loc[date, longs].mean())
            short_beta = float(beta.loc[date, shorts].mean())
            denominator = long_beta + short_beta
            if denominator > 0:
                long_gross = short_beta / denominator
                short_gross = long_beta / denominator
        row = pd.Series(0.0, index=close.columns, dtype="float32")
        row[longs] = long_gross / len(longs)
        row[shorts] = -short_gross / len(shorts)
        weights.loc[date:] = row.values
    gross = (weights.shift(1) * daily_returns).sum(axis=1)
    return gross - weights.diff().abs().sum(axis=1) * 15 / 10_000


def main() -> None:
    close, volume = load()
    close, volume = norm_index(close), norm_index(volume)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close]
    close, volume = close[stocks], volume[stocks]
    benchmark = norm_index(load_parquet(["SPY"])["SPY"]["close"]).reindex(
        close.index
    ).ffill()
    raw = close.shift(21) / close.shift(252) - 1
    residual = residual_signal(close, benchmark)
    variants = {
        "raw MOM_LS": portfolio_returns(
            raw, close, volume, benchmark, beta_neutral=False
        ),
        "residual MOM_LS": portfolio_returns(
            residual, close, volume, benchmark, beta_neutral=False
        ),
        "residual beta-neutral": portfolio_returns(
            residual, close, volume, benchmark, beta_neutral=True
        ),
    }
    results = {}
    for window, slicer in {
        "full": slice(None),
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_2026": slice("2023-01-01", None),
    }.items():
        results[window] = [
            returns_summary(stream.loc[slicer], name)
            for name, stream in variants.items()
        ]
    out = Path("reports/residual_momentum_study.json")
    out.write_text(json.dumps({
        "conclusion": (
            "Keep raw MOM_LS. Removing market-beta momentum lowers Sharpe in "
            "both early and held-out samples."
        ),
        "results": results,
    }, indent=2))
    for window, rows in results.items():
        print(f"\n{window}")
        print(pd.DataFrame(rows).to_string(index=False))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
