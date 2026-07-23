"""Cross-sectional momentum on individual stocks.

The sector-ETF momentum sleeve produced nothing, which is expected: the
academic momentum effect is measured on *single names*, where cross-sectional
dispersion is wide. Eleven sector ETFs average that dispersion away — the whole
signal is diluted before it is ranked.

This tests the anomaly the way the literature actually defines it: rank a broad
stock universe by trailing return skipping the most recent month, hold the top
slice, rebalance periodically.

    python -m backtest.xsec_momentum --top 20 --rebalance 21

Read the survivorship warning in `backtest/xsec_data.py`. This data contains
only currently-listed names, so results here are an **upper bound**. A negative
result is trustworthy; a positive one is not, until re-run on point-in-time
data that includes delisted tickers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.xsec_data import load

TRADING_DAYS = 252


def operating_stock_symbols(
    classified: list[str],
    available: pd.Index,
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    """Return unique operating stocks without benchmark/ETF duplicates."""
    excluded = exclude or set()
    available_set = set(available)
    return list(dict.fromkeys(
        symbol for symbol in classified
        if symbol in available_set and symbol not in excluded
    ))


def build_portfolio(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    lookback: int = 252,
    skip: int = 21,
    top_n: int = 20,
    rebalance: int = 21,
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
    cost_bps: float = 15.0,
    long_only: bool = True,
    short_bottom: bool = False,
    return_overrides: pd.DataFrame | None = None,
) -> tuple[pd.Series, pd.DataFrame]:
    """Equal-weight the top momentum names, rebalanced every `rebalance` days."""
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()

    # Momentum: return over `lookback` days, skipping the most recent `skip`.
    past = close.shift(lookback)
    recent = close.shift(skip)
    momentum = (recent / past) - 1.0

    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )

    daily_returns = close.pct_change()
    if return_overrides is not None:
        aligned = return_overrides.reindex(
            index=daily_returns.index, columns=daily_returns.columns
        )
        daily_returns = daily_returns.mask(aligned.notna(), aligned)
    rebalance_days = close.index[lookback + skip :: rebalance]

    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns, dtype="float32")
    holdings_log = []

    for date in rebalance_days:
        mom = momentum.loc[date].where(eligible.loc[date])
        ranked = mom.dropna().sort_values(ascending=False)
        if len(ranked) < top_n * 2:
            continue
        longs = ranked.head(top_n).index
        w = pd.Series(0.0, index=close.columns, dtype="float32")
        w[longs] = 1.0 / top_n
        if short_bottom:
            shorts = ranked.tail(top_n).index
            w[shorts] = -1.0 / top_n
            w /= 2.0
        weights.loc[date:] = w.values
        holdings_log.append({
            "date": date, "n_eligible": int(len(ranked)),
            "top": list(longs[:5]), "median_mom": float(ranked.head(top_n).median()),
        })

    # Portfolio return, with turnover cost charged on rebalance days.
    port_returns = (weights.shift(1) * daily_returns).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    port_returns -= turnover * (cost_bps / 10_000.0)

    equity = (1 + port_returns.fillna(0)).cumprod()
    return equity, pd.DataFrame(holdings_log)


def stats(equity: pd.Series, label: str) -> dict:
    r = equity.pct_change().dropna()
    if len(r) < 20:
        return {"strategy": label, "error": "insufficient data"}
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    vol = float(r.std() * np.sqrt(TRADING_DAYS))
    mx = equity.cummax()
    dd = ((equity - mx) / mx).min()
    return {
        "strategy": label,
        "cagr": round(float(equity.iloc[-1] ** (1 / years) - 1), 4),
        "sharpe": round(float((r.mean() * TRADING_DAYS) / vol), 3) if vol > 0 else 0.0,
        "vol": round(vol, 4),
        "max_dd": round(float(dd), 4),
        "x_money": round(float(equity.iloc[-1]), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--rebalance", type=int, default=21)
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--out", default="reports/xsec_momentum.json")
    args = ap.parse_args()

    print("Loading matrices...")
    close, volume = load()
    print(f"  {close.shape[0]:,} days x {close.shape[1]:,} symbols")

    # Restrict to operating companies. Leaving ETFs in would let leveraged
    # products (TQQQ, SOXL, UPRO) dominate every momentum ranking in a bull
    # market, which measures embedded leverage, not the momentum anomaly.
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = operating_stock_symbols(
        classified["stocks"], close.columns, exclude={"SPY"}
    )
    keep = stocks + (["SPY"] if "SPY" in close.columns else [])
    close, volume = close[keep], volume[keep]
    print(f"  restricted to {len(stocks):,} operating companies (+SPY benchmark)")

    spy = close["SPY"] if "SPY" in close.columns else None
    results, curves = [], {}

    configs = [
        ("12-1mo top20 monthly", dict(lookback=252, skip=21, top_n=20, rebalance=21)),
        ("12-1mo top50 monthly", dict(lookback=252, skip=21, top_n=50, rebalance=21)),
        ("12-1mo top10 monthly", dict(lookback=252, skip=21, top_n=10, rebalance=21)),
        ("6-1mo  top20 monthly", dict(lookback=126, skip=21, top_n=20, rebalance=21)),
        ("6-1mo  top20 weekly",  dict(lookback=126, skip=21, top_n=20, rebalance=5)),
        ("3-1mo  top20 monthly", dict(lookback=63,  skip=21, top_n=20, rebalance=21)),
        ("12-1mo top20 quarterly", dict(lookback=252, skip=21, top_n=20, rebalance=63)),
        ("12-1mo long/short top20", dict(lookback=252, skip=21, top_n=20, rebalance=21, short_bottom=True)),
    ]

    for label, kwargs in configs:
        equity, holdings = build_portfolio(close, volume, cost_bps=args.cost_bps, **kwargs)
        s = stats(equity, label)
        s["rebalances"] = len(holdings)
        results.append(s)
        curves[label] = equity
        print(f"  {label:26} cagr={s.get('cagr')} sharpe={s.get('sharpe')} dd={s.get('max_dd')}")

    if spy is not None:
        bh = spy / spy.iloc[0]
        results.append(stats(bh, "SPY buy & hold"))

    df = pd.DataFrame(results).sort_values("cagr", ascending=False)
    pd.set_option("display.width", 170)
    print("\n" + "=" * 90)
    print(df.to_string(index=False))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "note": "SURVIVORSHIP BIASED (currently-listed names only) — upper bound, "
                "negative results trustworthy, positive results are not validated",
        "cost_bps": args.cost_bps,
        "results": df.to_dict("records"),
    }, indent=2, default=str))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
