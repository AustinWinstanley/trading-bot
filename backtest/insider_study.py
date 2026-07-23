"""Event study: do insider cluster buys predict returns?

Before building a portfolio strategy on a signal, measure whether the signal
exists. An event study answers that far more cheaply and with far fewer moving
parts than a full backtest — no sizing, no stops, no rebalancing to confound it.

Method
------
For each cluster signal on date D:
  * enter at the OPEN of D+1 (the signal is only public once filed, and you
    cannot trade the close that revealed it)
  * measure raw return over +1, +5, +20, +60 trading days
  * subtract SPY's return over the identical window -> abnormal return (CAR)

The number that matters is mean/median CAR and the fraction positive. If CAR is
indistinguishable from zero, there is nothing here and no amount of portfolio
construction will create it.

Prices come from Alpaca, which covers all US tickers from 2021 (Tiingo's free
tier caps at 50 symbols/hour, which cannot cover a signal universe this wide).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from engine.data import AlpacaClient
from engine.edgar import build_insider_dataset, cluster_signals, open_market_buys

HORIZONS = (1, 5, 20, 60)


def load_insider_frame(start: dt.date, end: dt.date) -> pd.DataFrame:
    """Read only the columns and dates we need.

    The full parquet is ~5.6M rows across 18 columns; loading it whole alongside
    price data is enough to get the process OOM-killed on this box.
    """
    from engine.edgar import CACHE_DIR

    path = next(CACHE_DIR.glob("insider_*.parquet"), None)
    if path is None:
        raise FileNotFoundError("no insider parquet; run build_insider_dataset first")
    df = pd.read_parquet(path, columns=[
        "filing_date", "code", "acq_disp", "shares", "price", "value", "symbol",
        "is_10b5_1", "owner_cik", "is_officer", "is_director", "is_ten_pct",
        "is_c_suite", "issuer",
    ])
    return df[(df["filing_date"] >= pd.Timestamp(start)) & (df["filing_date"] <= pd.Timestamp(end))]


def study_streaming(
    signals: pd.DataFrame, start: dt.date, end: dt.date, benchmark: str = "SPY", chunk: int = 50
) -> pd.DataFrame:
    """Price symbols in batches, extract event returns, discard the bars.

    Holding bars for every signalled ticker at once is what blew memory; only
    the per-event return rows need to survive each batch.
    """
    client = AlpacaClient()
    spy = client.get_bars([benchmark], start, end).get(benchmark)
    if spy is None or spy.empty:
        raise ValueError(f"{benchmark} prices required")

    symbols = sorted(set(signals["symbol"]))
    by_symbol = {s: g for s, g in signals.groupby("symbol")}
    rows: list[dict] = []

    for i in range(0, len(symbols), chunk):
        batch = symbols[i : i + chunk]
        try:
            frames = client.get_bars(batch, start, end)
        except Exception as exc:
            print(f"  batch {i // chunk}: {type(exc).__name__}: {exc}", flush=True)
            continue
        for sym, df in frames.items():
            for _, sig in by_symbol.get(sym, pd.DataFrame()).iterrows():
                sd = pd.Timestamp(sig["signal_date"], tz="UTC")
                stock, bench = forward_returns(df, sd), forward_returns(spy, sd)
                if stock is None or bench is None:
                    continue
                row = {
                    "symbol": sym, "signal_date": sig["signal_date"],
                    "n_insiders": sig["n_insiders"], "n_c_suite": sig.get("n_c_suite", 0),
                    "total_value": sig["total_value"], "entry_price": stock["entry_price"],
                }
                for h in HORIZONS:
                    row[f"r{h}"] = stock[f"r{h}"]
                    row[f"car{h}"] = stock[f"r{h}"] - bench[f"r{h}"]
                rows.append(row)
        del frames
        print(f"  priced {min(i + chunk, len(symbols)):,}/{len(symbols):,} symbols, "
              f"{len(rows):,} events", end="\r", flush=True)
    print()
    return pd.DataFrame(rows)


def forward_returns(df: pd.DataFrame, signal_date: pd.Timestamp, horizons=HORIZONS) -> dict | None:
    """Enter at the next open after the signal; measure forward returns."""
    idx = df.index.searchsorted(signal_date + pd.Timedelta(hours=23, minutes=59), side="right")
    if idx >= len(df):
        return None
    entry = float(df["open"].iloc[idx])
    if not np.isfinite(entry) or entry <= 0:
        return None
    out = {"entry_date": df.index[idx], "entry_price": entry}
    for h in horizons:
        j = idx + h
        out[f"r{h}"] = (float(df["close"].iloc[j]) / entry - 1.0) if j < len(df) else np.nan
    return out


def run_study(
    signals: pd.DataFrame, prices: dict[str, pd.DataFrame], benchmark: str = "SPY"
) -> pd.DataFrame:
    spy = prices.get(benchmark)
    if spy is None or spy.empty:
        raise ValueError(f"{benchmark} prices required")

    rows = []
    for _, sig in signals.iterrows():
        df = prices.get(sig["symbol"])
        if df is None or df.empty:
            continue
        sd = pd.Timestamp(sig["signal_date"], tz="UTC")
        stock = forward_returns(df, sd)
        bench = forward_returns(spy, sd)
        if stock is None or bench is None:
            continue
        row = {
            "symbol": sig["symbol"],
            "signal_date": sig["signal_date"],
            "n_insiders": sig["n_insiders"],
            "n_c_suite": sig.get("n_c_suite", 0),
            "total_value": sig["total_value"],
            "entry_price": stock["entry_price"],
        }
        for h in HORIZONS:
            row[f"r{h}"] = stock[f"r{h}"]
            row[f"car{h}"] = stock[f"r{h}"] - bench[f"r{h}"]
        rows.append(row)
    return pd.DataFrame(rows)


def summarize(study: pd.DataFrame, label: str = "") -> pd.DataFrame:
    rows = []
    for h in HORIZONS:
        col = study[f"car{h}"].dropna()
        raw = study[f"r{h}"].dropna()
        if len(col) < 5:
            continue
        # t-stat on the mean abnormal return: is it distinguishable from zero?
        t = float(col.mean() / (col.std(ddof=1) / np.sqrt(len(col)))) if col.std(ddof=1) > 0 else 0.0
        rows.append({
            "label": label,
            "horizon_days": h,
            "n": len(col),
            "mean_raw": round(float(raw.mean()), 4),
            "mean_car": round(float(col.mean()), 4),
            "median_car": round(float(col.median()), 4),
            "pct_positive": round(float((col > 0).mean()), 3),
            "t_stat": round(t, 2),
            "significant": abs(t) > 1.96,
        })
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-07-22")
    ap.add_argument("--min-insiders", type=int, default=3)
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--min-value", type=float, default=50_000)
    ap.add_argument("--out", default="reports/insider_study.json")
    args = ap.parse_args()

    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)

    print("Loading insider dataset...")
    df = load_insider_frame(start, end)
    print(f"  {len(df):,} transaction rows")

    buys = open_market_buys(df, min_value=args.min_value)
    print(f"  {len(buys):,} qualifying open-market buys across {buys['symbol'].nunique():,} tickers")

    signals = cluster_signals(buys, window_days=args.window_days, min_insiders=args.min_insiders)
    print(f"  {len(signals):,} cluster signals ({args.min_insiders}+ insiders / {args.window_days}d)")

    print(f"\nPricing {signals['symbol'].nunique():,} symbols in batches...")
    study = study_streaming(signals, start - dt.timedelta(days=10), end)
    del df
    print(f"\nEvent study on {len(study):,} events with usable prices\n")

    summaries = [summarize(study, "all cluster signals")]

    # Does the signal strengthen with more insiders, C-suite involvement, size?
    if len(study):
        cuts = {
            "4+ insiders": study[study["n_insiders"] >= 4],
            "has C-suite buyer": study[study["n_c_suite"] >= 1],
            "cluster value > $1M": study[study["total_value"] > 1e6],
            "price >= $5": study[study["entry_price"] >= 5],
            "price < $5 (microcap)": study[study["entry_price"] < 5],
        }
        for name, subset in cuts.items():
            if len(subset) >= 20:
                summaries.append(summarize(subset, name))

    table = pd.concat(summaries, ignore_index=True)
    pd.set_option("display.width", 200)
    print(table.to_string(index=False))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "params": vars(args),
        "n_signals": int(len(signals)),
        "n_events_priced": int(len(study)),
        "summary": table.to_dict("records"),
    }, indent=2, default=str))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
