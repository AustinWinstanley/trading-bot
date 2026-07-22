"""Backtest CLI.

    python -m backtest.run --source alpaca --start 2021-01-01
    python -m backtest.run --source tiingo --start 2015-01-01 --fetch
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, run_backtest
from engine.config import load_config
from engine.data import REPO_ROOT, AlpacaClient, load_cached_bars

STRESS_WINDOWS = {
    "2018 Q4 selloff":  ("2018-10-01", "2018-12-31"),
    "2020 COVID crash": ("2020-02-15", "2020-04-15"),
    "2022 bear market": ("2022-01-01", "2022-10-31"),
    "2023 recovery":    ("2023-01-01", "2023-12-31"),
}


def universe(cfg) -> list[str]:
    syms = set(cfg.sleeves["momentum"]["universe"])
    syms |= set(cfg.sleeves["leveraged"]["symbols"])
    syms.add(cfg.sleeves["momentum"].get("risk_off_symbol", "SHY"))
    syms.add(cfg.sleeves["leveraged"].get("trend_symbol", "QQQ"))
    syms.add("SPY")
    return sorted(syms)


def load_bars(source: str, syms: list[str], start: dt.date, end: dt.date, fetch: bool) -> dict:
    if source == "tiingo":
        from engine.tiingo import TiingoClient, load_parquet, save_parquet

        if fetch:
            client = TiingoClient()
            client.check_auth()
            print(f"Fetching {len(syms)} symbols from Tiingo ({start} -> {end})...")
            frames = client.get_bars(syms, start, end)
            save_parquet(frames)
            print(f"Saved {len(frames)} symbols to state/history/")
            return frames
        frames = load_parquet(syms)
        if not frames:
            sys.exit("No cached Tiingo history. Re-run with --fetch.")
        return frames

    if fetch:
        client = AlpacaClient()
        print(f"Fetching {len(syms)} symbols from Alpaca ({start} -> {end})...")
        from engine.data import cache_bars

        frames = client.get_bars(syms, start, end)
        cache_bars(frames)
        return frames

    frames = {s: load_cached_bars(s) for s in syms}
    return {s: d for s, d in frames.items() if not d.empty}


def buy_and_hold(bars: dict, start: dt.date, end: dt.date, equity: float, symbol="SPY") -> dict:
    df = bars[symbol]
    seg = df.loc[(df.index.date >= start) & (df.index.date <= end)]
    if len(seg) < 2:
        return {}
    curve = equity * seg["close"] / float(seg["close"].iloc[0])
    rets = curve.pct_change().dropna()
    years = max((seg.index[-1] - seg.index[0]).days / 365.25, 1e-9)
    run_max = curve.cummax()
    vol = float(rets.std() * np.sqrt(252))
    return {
        "cagr": round(float((curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1), 4),
        "sharpe": round(float((rets.mean() * 252) / vol), 3) if vol > 0 else 0.0,
        "max_drawdown": round(float(((curve - run_max) / run_max).min()), 4),
        "final_equity": round(float(curve.iloc[-1]), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["alpaca", "tiingo"], default="alpaca")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--fetch", action="store_true", help="pull fresh data instead of using cache")
    ap.add_argument("--equity", type=float, default=None)
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--spread-bps", type=float, default=3.0)
    ap.add_argument("--sleeves", default="momentum,mean_reversion,leveraged")
    ap.add_argument("--out", default=None, help="write JSON results here")
    args = ap.parse_args()

    cfg = load_config()
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    syms = universe(cfg)
    bars = load_bars(args.source, syms, start, end, args.fetch)

    missing = [s for s in syms if s not in bars]
    if missing:
        print(f"WARNING: no data for {missing}")
    if "SPY" not in bars:
        sys.exit("SPY is required as the regime benchmark")

    coverage = min(d.index.min() for d in bars.values()).date()
    print(f"\nData: {len(bars)} symbols, earliest bar {coverage}")
    if coverage > start:
        print(f"NOTE: requested start {start} but data begins {coverage}; using {coverage}")
        start = coverage

    bt = BacktestConfig(
        start=start, end=end,
        initial_equity=args.equity or cfg.starting_equity,
        slippage_bps=args.slippage_bps, spread_bps=args.spread_bps,
        sleeves=tuple(args.sleeves.split(",")),
    )

    print(f"Running backtest {bt.start} -> {bt.end}, sleeves={bt.sleeves}, "
          f"costs={bt.slippage_bps + bt.spread_bps:.0f}bps/side\n")
    result = run_backtest(bars, cfg, bt)
    m = result.metrics()

    print("=" * 62)
    print("PERFORMANCE")
    print("=" * 62)
    for k in ("start", "end", "years", "initial_equity", "final_equity",
              "total_return", "cagr", "annual_vol", "sharpe", "sortino",
              "max_drawdown", "max_dd_date"):
        print(f"  {k:18} {m.get(k)}")
    print("\n  --- trades ---")
    for k in ("trades", "win_rate", "avg_win", "avg_loss", "profit_factor", "avg_days_held"):
        print(f"  {k:18} {m.get(k)}")

    bh = buy_and_hold(bars, bt.start, bt.end, bt.initial_equity)
    if bh:
        print("\n" + "=" * 62)
        print("vs BUY & HOLD SPY")
        print("=" * 62)
        print(f"  {'':18} {'strategy':>12} {'SPY B&H':>12}")
        for k in ("cagr", "sharpe", "max_drawdown", "final_equity"):
            print(f"  {k:18} {str(m.get(k)):>12} {str(bh.get(k)):>12}")

    attr = result.sleeve_attribution()
    if not attr.empty:
        print("\n" + "=" * 62)
        print("SLEEVE ATTRIBUTION")
        print("=" * 62)
        print(attr.to_string(index=False))

    print("\n" + "=" * 62)
    print("STRESS WINDOWS")
    print("=" * 62)
    print(result.window_performance(STRESS_WINDOWS).to_string(index=False))

    monthly = result.monthly_returns()
    if len(monthly):
        neg = (monthly < 0).sum()
        print("\n" + "=" * 62)
        print("MONTHLY")
        print("=" * 62)
        print(f"  months            {len(monthly)}")
        print(f"  negative          {neg} ({neg / len(monthly):.0%})")
        print(f"  best              {monthly.max():.2%}")
        print(f"  worst             {monthly.min():.2%}")
        print(f"  median            {monthly.median():.2%}")
        print(f"  mean              {monthly.mean():.2%}")
        print(f"  >= +10%           {(monthly >= 0.10).sum()}")

    if result.gate_rejections:
        print("\n" + "=" * 62)
        print(f"GATE REJECTIONS ({len(result.gate_rejections)})")
        print("=" * 62)
        counts: dict[str, int] = {}
        for r in result.gate_rejections:
            key = r["reason"].split("(")[0].split(":")[0].strip()[:58]
            counts[key] = counts.get(key, 0) + 1
        for reason, n in sorted(counts.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:5}  {reason}")

    if result.notes:
        print("\nNOTES")
        for n in result.notes[:10]:
            print(f"  {n}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "metrics": m,
            "buy_and_hold": bh,
            "sleeve_attribution": attr.to_dict("records") if not attr.empty else [],
            "monthly_returns": {k.date().isoformat(): round(float(v), 6) for k, v in monthly.items()},
            "notes": result.notes,
        }, indent=2))
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
