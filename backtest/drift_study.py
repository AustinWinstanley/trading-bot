"""Post-event drift: does a big volume-confirmed jump keep going?

This is the PEAD hypothesis in a form that needs no earnings calendar. A true
PEAD test requires knowing every earnings announcement date; free bulk sources
give filing dates, not announcement dates, and the two differ by days to weeks.

Instead we test the mechanism directly: a large one-day move on heavy volume is
the market repricing on news. If drift exists after such events generally, PEAD
is worth the plumbing to isolate. If there is no drift after *any* volume-
confirmed repricing, PEAD specifically is unlikely to rescue us.

Entry is the **close of D+1**, not the close of D. A bot that runs after the
close sees the jump only once it has happened and can act no earlier than the
next session — measuring from D's close would book a return nobody could get.

Everything is vectorised over the (dates x symbols) matrices; per-symbol loops
over ~11k names are prohibitively slow.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.xsec_data import load

HORIZONS = (5, 20, 40, 60)


def event_mask(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    min_move: float = 0.05,
    min_vol_mult: float = 2.0,
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
    direction: str = "up",
) -> pd.DataFrame:
    ret = close.pct_change()
    avg_vol = volume.rolling(20, min_periods=10).mean()
    vol_mult = volume / avg_vol
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()

    move = ret >= min_move if direction == "up" else ret <= -min_move
    return (
        move
        & vol_mult.ge(min_vol_mult)
        & close.ge(min_price)
        & dollar_volume.ge(min_dollar_volume)
        & close.shift(1).notna()
    )


def abnormal_returns(
    close: pd.DataFrame, mask: pd.DataFrame, benchmark: str = "SPY"
) -> dict[int, pd.Series]:
    """CAR from the close of D+1 to the close of D+1+h, benchmark-adjusted."""
    if benchmark not in close.columns:
        raise ValueError(f"{benchmark} missing from matrix")
    bench = close[benchmark]

    out: dict[int, pd.Series] = {}
    entry = close.shift(-1)              # enter at D+1 close
    bench_entry = bench.shift(-1)
    for h in HORIZONS:
        fwd = close.shift(-1 - h) / entry - 1.0
        bench_fwd = (bench.shift(-1 - h) / bench_entry - 1.0)
        car = fwd.sub(bench_fwd, axis=0)
        out[h] = car.where(mask).stack()
    return out


def summarize(cars: dict[int, pd.Series], label: str) -> pd.DataFrame:
    rows = []
    for h, series in cars.items():
        s = series.dropna()
        if len(s) < 30:
            continue
        t = float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if s.std(ddof=1) > 0 else 0.0
        rows.append({
            "label": label,
            "horizon": h,
            "n": len(s),
            "mean_car": round(float(s.mean()), 4),
            "median_car": round(float(s.median()), 4),
            "pct_positive": round(float((s > 0).mean()), 3),
            "t_stat": round(t, 2),
            "significant": abs(t) > 1.96,
        })
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/drift_study.json")
    args = ap.parse_args()

    print("Loading matrices...")
    close, volume = load()
    print(f"  {close.shape[0]:,} days x {close.shape[1]:,} symbols\n")

    variants = [
        ("up >=5% on 2x vol",   dict(min_move=0.05, min_vol_mult=2.0, direction="up")),
        ("up >=8% on 3x vol",   dict(min_move=0.08, min_vol_mult=3.0, direction="up")),
        ("up >=12% on 4x vol",  dict(min_move=0.12, min_vol_mult=4.0, direction="up")),
        ("up >=5% on 2x vol, liquid $20M",
         dict(min_move=0.05, min_vol_mult=2.0, direction="up", min_dollar_volume=2e7)),
        ("down >=5% on 2x vol", dict(min_move=0.05, min_vol_mult=2.0, direction="down")),
        ("down >=8% on 3x vol", dict(min_move=0.08, min_vol_mult=3.0, direction="down")),
    ]

    tables = []
    for label, kwargs in variants:
        mask = event_mask(close, volume, **kwargs)
        n = int(mask.sum().sum())
        print(f"{label:38} {n:>8,} events")
        if n < 100:
            continue
        tables.append(summarize(abnormal_returns(close, mask), label))

    table = pd.concat(tables, ignore_index=True)
    pd.set_option("display.width", 180)
    print("\n" + "=" * 100)
    print(table.to_string(index=False))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "note": "survivorship-biased universe; entry at close of D+1",
        "results": table.to_dict("records"),
    }, indent=2, default=str))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
