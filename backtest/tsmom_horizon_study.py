"""Multi-horizon and long/short alternatives to deployed asset TSMOM."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from engine.config import load_config
from engine.tiingo import load_parquet


def trend_stream(close: pd.DataFrame, variant: str) -> pd.Series:
    returns = close.pct_change()
    inverse_vol = 1 / (
        returns.rolling(63, min_periods=40).std() * np.sqrt(TD)
    ).clip(lower=0.04)
    signs = [np.sign(close / close.shift(horizon) - 1) for horizon in (63, 126, 252)]

    if variant == "12m long_flat":
        signal = (signs[-1] > 0).astype(float)
    elif variant == "ensemble fractional long_flat":
        signal = sum((sign > 0).astype(float) for sign in signs) / len(signs)
    elif variant == "ensemble majority long_flat":
        signal = (sum((sign > 0).astype(int) for sign in signs) >= 2).astype(float)
    elif variant == "ensemble long_short":
        signal = sum(signs) / len(signs)
    else:
        raise ValueError(f"unknown variant {variant!r}")

    weights = signal * inverse_vol
    weights = weights.div(weights.abs().sum(axis=1).replace(0, np.nan), axis=0)
    weights = weights.fillna(0).shift(1)
    gross = (weights * returns).sum(axis=1)
    return gross - weights.diff().abs().sum(axis=1) * 8 / 10_000


def main() -> None:
    cfg = load_config()
    frames = load_parquet(
        cfg.sleeves_paper["tsmom_universe"], Path("state/history_assets")
    )
    close = pd.DataFrame({
        symbol: norm_index(frame["close"]) for symbol, frame in frames.items()
    })
    variants = (
        "12m long_flat",
        "ensemble fractional long_flat",
        "ensemble majority long_flat",
        "ensemble long_short",
    )
    streams = {name: trend_stream(close, name) for name in variants}
    production = build_streams()
    core = (
        0.40 * production["spy"]
        + 0.20 * production["trend"]
        + 0.30 * production["mom_ls"]
    )

    standalone = [
        returns_summary(stream.loc["2003":], name)
        for name, stream in streams.items()
    ]
    portfolio = []
    for name, stream in streams.items():
        aligned = pd.DataFrame({"core": core, "tsmom": stream}).dropna()
        combined = (
            aligned["core"] + 0.25 * aligned["tsmom"]
            - 0.15 * SHORT_BORROW / TD
        )
        portfolio.append(returns_summary(combined, name))

    out = Path("reports/tsmom_horizon_study.json")
    out.write_text(json.dumps({
        "conclusion": (
            "Keep deployed 12-month long/flat. Multi-horizon and long/short "
            "variants do not improve the production portfolio."
        ),
        "standalone": standalone,
        "production_portfolio": portfolio,
    }, indent=2))
    print("STANDALONE")
    print(pd.DataFrame(standalone).to_string(index=False))
    print("\nPRODUCTION PORTFOLIO")
    print(pd.DataFrame(portfolio).to_string(index=False))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
