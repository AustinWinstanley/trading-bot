"""Build a delisted-equity extension for the cross-sectional research matrix.

The live/current Alpaca asset list omits companies that disappeared during the
backtest, creating survivorship bias. Tiingo publishes daily supported-ticker
metadata with listing start/end dates. We use that metadata only to identify
historical U.S. listings, then fetch bars through Alpaca's batched endpoint.

Ticker reuse is dangerous because a single matrix column could join unrelated
companies. Recycled/duplicate tickers are therefore excluded entirely.
Five-character NASDAQ-style warrant/unit/right suffixes are also excluded.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

from backtest.xsec_data import build_matrices, save
from engine.data import REPO_ROOT

TIINGO_TICKERS = (
    "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
)
OUT_DIR = REPO_ROOT / "state" / "xsec_delisted"
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "NYSE MKT"}


def download_metadata() -> pd.DataFrame:
    response = requests.get(TIINGO_TICKERS, timeout=60)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        csv_name = next(
            name for name in archive.namelist() if name.lower().endswith(".csv")
        )
        return pd.read_csv(archive.open(csv_name))


def candidate_symbols(
    metadata: pd.DataFrame,
    *,
    active_symbols: set[str],
    first_end: str = "2020-02-01",
    last_end: str = "2025-12-31",
) -> pd.DataFrame:
    data = metadata.copy()
    data["ticker"] = data["ticker"].astype(str).str.upper()
    for column in ("startDate", "endDate"):
        data[column] = pd.to_datetime(data[column], errors="coerce")

    duplicate_tickers = set(
        data.loc[data["ticker"].duplicated(keep=False), "ticker"]
    )
    plain_symbol = data["ticker"].str.fullmatch(r"[A-Z]{1,5}")
    fifth_char_derivative = (
        data["ticker"].str.len().eq(5)
        & data["ticker"].str[-1].isin(["W", "U", "R"])
    )
    established = (data["endDate"] - data["startDate"]).dt.days >= 365
    ended_in_window = data["endDate"].between(first_end, last_end)

    keep = (
        data["assetType"].eq("Stock")
        & data["priceCurrency"].eq("USD")
        & data["exchange"].isin(US_EXCHANGES)
        & plain_symbol
        & ~fifth_char_derivative
        & established
        & ended_in_window
        & ~data["ticker"].isin(duplicate_tickers)
        & ~data["ticker"].isin(active_symbols)
    )
    return (
        data.loc[
            keep,
            ["ticker", "exchange", "assetType", "startDate", "endDate"],
        ]
        .sort_values(["endDate", "ticker"])
        .reset_index(drop=True)
    )


def main() -> None:
    current_close = pd.read_parquet(REPO_ROOT / "state" / "xsec" / "close.parquet")
    metadata = download_metadata()
    candidates = candidate_symbols(
        metadata,
        active_symbols=set(map(str, current_close.columns)),
    )
    symbols = candidates["ticker"].tolist()
    print(
        f"Fetching {len(symbols):,} non-recycled delisted U.S. stocks "
        f"({candidates.endDate.min().date()} -> {candidates.endDate.max().date()})"
    )
    close, volume = build_matrices(
        symbols,
        current_close.index.min().date(),
        current_close.index.max().date(),
        chunk=50,
    )
    out = save(close, volume, OUT_DIR)
    candidates.to_parquet(out / "metadata.parquet", index=False)
    coverage = close.notna().sum()
    print(
        f"Saved {close.shape[1]:,} symbols to {out}; "
        f"{(coverage >= 252).sum():,} have at least 252 bars"
    )


if __name__ == "__main__":
    main()
