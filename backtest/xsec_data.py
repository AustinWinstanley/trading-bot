"""Build wide close/volume matrices for cross-sectional tests.

Fetches in batches and keeps only close and volume as float32 matrices
(dates x symbols), discarding the rest of each bar. Holding full OHLCV frames
for ~11k symbols is what OOM-kills this box.

    python -m backtest.xsec_data --start 2020-01-01

SURVIVORSHIP BIAS — read before trusting any result built on this.
Alpaca's asset list contains only *currently listed* securities. Every company
that was delisted, acquired at a discount, or went to zero during the window is
absent. That biases returns upward, and it biases them upward *most* for
strategies that hold small, volatile, high-momentum names — exactly the kind of
strategy this data is used to test.

So this data can only support one kind of conclusion honestly: if a strategy
fails here, it fails, because the bias was working in its favour. A strategy
that *succeeds* here has not been validated and must be re-tested on a
point-in-time universe that includes dead tickers before it is believed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from engine.data import REPO_ROOT, AlpacaClient

OUT_DIR = REPO_ROOT / "state" / "xsec"


def build_current_universe() -> dict[str, list[str]]:
    """Classify active Alpaca equities using the SEC operating-company list.

    This is intentionally labelled *current*: it excludes delisted companies
    and therefore remains survivorship-biased. Persisting the exact list makes
    that bias reproducible instead of silently changing on every backtest.
    """
    from engine.fundamentals import cik_map

    client = AlpacaClient()
    assets = client.list_assets()
    active = {
        str(a["symbol"]).upper()
        for a in assets
        if a.get("tradable") and a.get("status") == "active"
    }
    sec_symbols = set(cik_map()["symbol"])
    stocks = sorted(active & sec_symbols)
    funds = sorted(active - sec_symbols)
    classified = {"stocks": stocks, "funds": funds}
    (REPO_ROOT / "state" / "universe_active.json").write_text(
        json.dumps(sorted(active), indent=2)
    )
    (REPO_ROOT / "state" / "universe_classified.json").write_text(
        json.dumps(classified, indent=2)
    )
    (REPO_ROOT / "state" / "universe_stocks.json").write_text(
        json.dumps(stocks, indent=2)
    )
    return classified


def build_matrices(
    symbols: list[str], start: dt.date, end: dt.date, *, chunk: int = 50
) -> tuple[pd.DataFrame, pd.DataFrame]:
    client = AlpacaClient()
    close_parts: list[pd.DataFrame] = []
    vol_parts: list[pd.DataFrame] = []

    for i in range(0, len(symbols), chunk):
        batch = symbols[i : i + chunk]
        try:
            frames = client.get_bars(batch, start, end)
        except Exception as exc:
            print(f"\n  batch {i // chunk} failed: {type(exc).__name__}: {exc}", flush=True)
            continue
        if frames:
            close_parts.append(
                pd.DataFrame({s: d["close"].astype("float32") for s, d in frames.items()})
            )
            vol_parts.append(
                pd.DataFrame({s: d["volume"].astype("float32") for s, d in frames.items()})
            )
        del frames
        done = min(i + chunk, len(symbols))
        print(f"  fetched {done:,}/{len(symbols):,} symbols", end="\r", flush=True)

    print()
    close = pd.concat(close_parts, axis=1).sort_index()
    volume = pd.concat(vol_parts, axis=1).sort_index()
    close = close.loc[:, ~close.columns.duplicated()]
    volume = volume.loc[:, ~volume.columns.duplicated()]
    return close, volume


def save(close: pd.DataFrame, volume: pd.DataFrame, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    close.to_parquet(out_dir / "close.parquet")
    volume.to_parquet(out_dir / "volume.parquet")
    return out_dir


def load(out_dir: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    out_dir = out_dir or OUT_DIR
    return (
        pd.read_parquet(out_dir / "close.parquet"),
        pd.read_parquet(out_dir / "volume.parquet"),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--universe", default="state/universe_active.json")
    ap.add_argument("--build-universe", action="store_true",
                    help="refresh active/current operating-company classification")
    ap.add_argument("--limit", type=int, default=None, help="cap symbol count (for a smoke test)")
    args = ap.parse_args()

    if args.build_universe:
        classified = build_current_universe()
        print(f"Universe: {len(classified['stocks']):,} operating companies, "
              f"{len(classified['funds']):,} funds/other equities")
    symbols = json.loads(Path(args.universe).read_text())
    if args.limit:
        symbols = symbols[: args.limit]
    print(f"Building matrices for {len(symbols):,} symbols "
          f"({args.start} -> {args.end})...")

    close, volume = build_matrices(
        symbols, dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    )
    out = save(close, volume)
    coverage = close.notna().sum()
    print(f"\nclose matrix: {close.shape[0]:,} days x {close.shape[1]:,} symbols")
    print(f"  symbols with >=250 bars: {(coverage >= 250).sum():,}")
    print(f"  symbols with >=500 bars: {(coverage >= 500).sum():,}")
    print(f"  memory: {close.memory_usage(deep=True).sum() / 1e6:.0f} MB")
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
