"""Volatility risk premium: does selling options actually pay?

Two questions, in order:

1. **Does the premium exist?** Compare VIX (implied vol) to the realised
   volatility that actually followed over the next 21 trading days. If implied
   systematically exceeds realised, sellers are being overpaid for insurance —
   and unlike a price-prediction edge, that has an economic reason to persist:
   someone is buying protection and is willing to pay up for it.

2. **Is it harvestable after the tail?** The premium existing is not the same as
   it being collectable. CBOE's PUT index (cash-secured put writing on the S&P
   500, 1991+) is the canonical implementation. Thirty-five years covers
   2000-02, 2008, 2020 and 2022 — enough real crashes to see what the strategy
   does when the insurance it sold gets claimed.

The number that matters is not the average return. It is the crash behaviour:
short volatility earns small steady premiums and pays them back in bursts, so a
sample without a crash tells you nothing about whether you can hold it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from engine.cboe import series
from engine.tiingo import load_parquet

TRADING_DAYS = 252


def realised_vol_forward(close: pd.Series, window: int = 21) -> pd.Series:
    """Annualised realised vol over the NEXT `window` days, in VIX points."""
    logret = np.log(close / close.shift(1))
    fwd = logret.shift(-window).rolling(window).std() * np.sqrt(TRADING_DAYS) * 100
    return fwd


def stats(curve: pd.Series, label: str) -> dict:
    curve = curve.dropna()
    r = curve.pct_change().dropna()
    if len(r) < 50:
        return {"strategy": label, "error": "insufficient data"}

    # Guard against annualising a series that is not actually daily. CBOE's
    # PUT history has a sparse pre-2007 region with multi-year gaps; treating
    # a 3-year move as one "daily" return silently corrupts vol, Sharpe and
    # drawdown. Refuse rather than report a confident wrong number.
    gaps = curve.index.to_series().diff().dt.days.dropna()
    if len(gaps) and gaps.quantile(0.99) > 15:
        return {"strategy": label,
                "error": f"series not daily (p99 gap {gaps.quantile(0.99):.0f}d, max {gaps.max():.0f}d)"}
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    vol = float(r.std() * np.sqrt(TRADING_DAYS))
    mx = curve.cummax()
    dd = (curve - mx) / mx
    downside = r[r < 0].std() * np.sqrt(TRADING_DAYS)
    return {
        "strategy": label,
        "from": curve.index[0].date().isoformat(),
        "to": curve.index[-1].date().isoformat(),
        "cagr": round(float((curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1), 4),
        "sharpe": round(float((r.mean() * TRADING_DAYS) / vol), 3) if vol > 0 else 0.0,
        "sortino": round(float((r.mean() * TRADING_DAYS) / downside), 3) if downside > 0 else 0.0,
        "vol": round(vol, 4),
        "max_dd": round(float(dd.min()), 4),
        "worst_day": round(float(r.min()), 4),
        "worst_month": round(float(curve.resample("ME").last().pct_change().min()), 4),
        "x_money": round(float(curve.iloc[-1] / curve.iloc[0]), 1),
    }


CRASHES = {
    "2000-02 dot-com":  ("2000-03-01", "2002-10-09"),
    "2008 GFC":         ("2007-10-09", "2009-03-09"),
    "2020 COVID":       ("2020-02-19", "2020-03-23"),
    "2022 bear":        ("2022-01-03", "2022-10-12"),
    "2018 Volmageddon": ("2018-01-26", "2018-02-09"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/vrp_study.json")
    args = ap.parse_args()

    print("Loading CBOE series...")
    vix, put, bxm = series("VIX"), series("PUT"), series("BXM")
    print(f"  VIX {vix.index[0].date()} -> {vix.index[-1].date()}  ({len(vix):,} days)")
    print(f"  PUT {put.index[0].date()} -> {put.index[-1].date()}  ({len(put):,} days)")
    print(f"  BXM {bxm.index[0].date()} -> {bxm.index[-1].date()}  ({len(bxm):,} days)")

    spy = load_parquet(["SPY"], Path("state/history_deep"))["SPY"]["close"]

    # ---- 1. does the premium exist? ----
    fwd_rv = realised_vol_forward(spy, 21)
    joined = pd.concat([vix.rename("implied"), fwd_rv.rename("realised")], axis=1).dropna()
    joined["vrp"] = joined["implied"] - joined["realised"]

    print("\n" + "=" * 78)
    print("1. DOES THE PREMIUM EXIST?  (VIX vs the realised vol that followed)")
    print("=" * 78)
    v = joined["vrp"]
    t = float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v))))
    print(f"  observations           {len(v):,} days ({joined.index[0].date()} -> {joined.index[-1].date()})")
    print(f"  mean implied (VIX)     {joined['implied'].mean():.2f}")
    print(f"  mean realised (next21) {joined['realised'].mean():.2f}")
    print(f"  mean premium           {v.mean():+.2f} vol points")
    print(f"  median premium         {v.median():+.2f}")
    print(f"  % of days positive     {(v > 0).mean():.1%}")
    print(f"  t-stat                 {t:.1f}  (autocorrelated — treat as directional, not exact)")

    by_decade = joined.groupby(joined.index.year // 10 * 10)["vrp"].agg(["mean", "median", "count"])
    print("\n  by decade:")
    print(by_decade.round(2).to_string())

    # Where does the premium go when you need it?
    print("\n  premium during crashes (negative = sellers paying out):")
    for name, (a, b) in CRASHES.items():
        seg = joined.loc[a:b, "vrp"]
        if len(seg):
            print(f"    {name:20} mean {seg.mean():+6.2f}   worst {seg.min():+7.2f}")

    # ---- 2. is it harvestable? ----
    print("\n" + "=" * 78)
    print("2. IS IT HARVESTABLE?  (CBOE PutWrite / BuyWrite indices vs SPY)")
    print("=" * 78)
    rows = []
    rows.append(stats(put, "PUT (put-write), daily era"))
    rows.append(stats(bxm, "BXM (covered call), daily era"))

    # Common daily window so the comparison is like-for-like.
    lo = max(put.index[0], bxm.index[0], spy.index[0])
    common = put.index.intersection(spy.index)
    common = common[common >= lo]
    for lbl, ser in (("PUT put-write", put), ("BXM covered call", bxm), ("SPY buy & hold", spy)):
        rows.append(stats(ser.reindex(common).ffill(), f"{lbl}, common window"))
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False))

    print("\n  crash drawdowns, PUT vs SPY:")
    crash_rows = []
    for name, (a, b) in CRASHES.items():
        row = {"crash": name}
        for lbl, ser in (("PUT", put), ("BXM", bxm), ("SPY", spy)):
            seg = ser.loc[a:b].dropna()
            if len(seg) > 2:
                mx = seg.cummax()
                row[f"{lbl}_ret"] = round(float(seg.iloc[-1] / seg.iloc[0] - 1), 4)
                row[f"{lbl}_dd"] = round(float(((seg - mx) / mx).min()), 4)
        crash_rows.append(row)
    crash_df = pd.DataFrame(crash_rows)
    print(crash_df.to_string(index=False))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "premium": {
            "mean_vrp_points": round(float(v.mean()), 3),
            "median_vrp_points": round(float(v.median()), 3),
            "pct_days_positive": round(float((v > 0).mean()), 4),
            "n_days": int(len(v)),
        },
        "strategies": rows,
        "crashes": crash_rows,
    }, indent=2, default=str))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
