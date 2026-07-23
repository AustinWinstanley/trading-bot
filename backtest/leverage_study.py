"""Fixed versus volatility-targeted leverage on the production portfolio."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    returns_summary,
)


def production_raw(streams: pd.DataFrame) -> pd.Series:
    return (
        0.40 * streams["spy"]
        + 0.25 * streams["tsmom"]
        + 0.20 * streams["trend"]
        + 0.30 * streams["mom_ls"]
    )


def volatility_targeted(
    raw: pd.Series,
    target_vol: float,
    *,
    lookback: int = 63,
    min_leverage: float = 0.5,
    max_leverage: float = 2.0,
) -> tuple[pd.Series, pd.Series]:
    """Scale using only volatility known before each return observation."""
    observed = raw.rolling(lookback, min_periods=max(20, lookback // 2)).std() * np.sqrt(TD)
    leverage = (target_vol / observed).clip(min_leverage, max_leverage).shift(1).fillna(1.0)
    financing = (leverage - 1).clip(lower=0) * MARGIN_RATE / TD
    short_borrow = (0.15 * leverage) * SHORT_BORROW / TD
    return leverage * raw - financing - short_borrow, leverage


def main() -> None:
    streams = build_streams()
    raw = production_raw(streams)
    variants: dict[str, tuple[pd.Series, pd.Series]] = {
        "base 1x": (
            raw - 0.15 * SHORT_BORROW / TD,
            pd.Series(1.0, index=raw.index),
        ),
        "fixed 2x": (
            2 * raw - MARGIN_RATE / TD - 0.30 * SHORT_BORROW / TD,
            pd.Series(2.0, index=raw.index),
        ),
    }
    for target in (0.12, 0.15, 0.18):
        variants[f"vol target {target:.0%}"] = volatility_targeted(raw, target)

    windows = {
        "full": streams.index,
        "early_2020_2022": streams.loc[:"2022-12-31"].index,
        "heldout_2023_2026": streams.loc["2023-01-01":].index,
    }
    results = {}
    for window, index in windows.items():
        rows = []
        for name, (returns, leverage) in variants.items():
            row = returns_summary(returns.loc[index], name)
            row["avg_leverage"] = round(float(leverage.loc[index].mean()), 3)
            row["max_leverage"] = round(float(leverage.loc[index].max()), 3)
            rows.append(row)
        results[window] = rows

    out = Path("reports/leverage_study.json")
    out.write_text(json.dumps({
        "note": (
            "63-day trailing volatility, shifted one day; leverage clipped to "
            "0.5x-2x. Includes 5% margin and 3% short borrow."
        ),
        "results": results,
    }, indent=2))
    for window, rows in results.items():
        print(f"\n{window}")
        for row in rows:
            print(
                f"  {row['portfolio']:<16} CAGR {row['cagr']:>7.2%} "
                f"Sharpe {row['sharpe']:>5.3f} DD {row['max_dd']:>7.2%} "
                f"avg lev {row['avg_leverage']:.2f}"
            )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
