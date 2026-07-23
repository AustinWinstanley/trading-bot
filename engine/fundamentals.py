"""SEC fundamental data (XBRL financial statement data sets).

Fundamentals are the one signal source in this repo that is *structurally*
independent of price. Every signal in the library so far is a transformation of
close and volume, which is why they collapsed into ~4 correlated clusters —
there are only so many ways to slice one data source. Earnings, assets and
equity come from a different generative process entirely, so a value or quality
rank has a real chance of being uncorrelated to a momentum rank.

    https://www.sec.gov/files/dera/data/financial-statement-data-sets/{Y}q{Q}.zip

`num.txt` is ~530MB uncompressed per quarter (≈35GB across the full history),
so it is streamed line by line against a tag whitelist rather than loaded.

POINT-IN-TIME: every fact is stamped with the submission's **`filed`** date, not
its `ddate` (period end). A December quarter disclosed in February cannot be
used in January. Using period-end dates is the single most common way a
fundamental backtest invents returns that were never available.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

from engine.data import REPO_ROOT

URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{q}.zip"
CACHE_DIR = REPO_ROOT / "state" / "fundamentals"
USER_AGENT = "austin redacted@example.com"

# Only the facts the signals need. Anything else is discarded during the
# streaming pass so num.txt never lands in memory.
TAGS = {
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "GrossProfit",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "NetCashProvidedByUsedInOperatingActivities",
    "CommonStockSharesOutstanding",
    "LongTermDebtNoncurrent",
    "ResearchAndDevelopmentExpense",
}


def download_quarter(year: int, quarter: int) -> Path | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / f"{year}q{quarter}.zip"
    if dest.exists() and dest.stat().st_size > 100_000:
        return dest
    r = requests.get(
        URL.format(year=year, q=quarter),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        timeout=600,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def parse_quarter(path: Path) -> pd.DataFrame:
    """Return one row per (submission, tag) for whitelisted tags only."""
    with zipfile.ZipFile(path) as z:
        with z.open("sub.txt") as fh:
            sub = pd.read_csv(
                io.BytesIO(fh.read()), sep="\t", dtype=str, on_bad_lines="skip",
                usecols=["adsh", "cik", "name", "form", "period", "filed", "fy", "fp"],
                low_memory=False,
            )
        sub = sub[sub["form"].isin(["10-K", "10-Q"])]
        wanted = set(sub["adsh"])

        # Stream num.txt: 530MB uncompressed, most of it irrelevant.
        keep: list[tuple] = []
        with z.open("num.txt") as fh:
            header = fh.readline().decode(errors="replace").rstrip("\n").split("\t")
            idx = {c: i for i, c in enumerate(header)}
            i_adsh, i_tag = idx["adsh"], idx["tag"]
            i_ddate, i_qtrs = idx["ddate"], idx["qtrs"]
            i_uom, i_val = idx["uom"], idx["value"]
            i_seg = idx.get("segments")
            for line in fh:
                parts = line.decode(errors="replace").rstrip("\n").split("\t")
                if len(parts) <= i_val:
                    continue
                if parts[i_tag] not in TAGS or parts[i_adsh] not in wanted:
                    continue
                # Consolidated totals only — segment breakdowns would double count.
                if i_seg is not None and len(parts) > i_seg and parts[i_seg]:
                    continue
                keep.append(
                    (parts[i_adsh], parts[i_tag], parts[i_ddate],
                     parts[i_qtrs], parts[i_uom], parts[i_val])
                )

    if not keep:
        return pd.DataFrame()

    num = pd.DataFrame(keep, columns=["adsh", "tag", "ddate", "qtrs", "uom", "value"])
    num["value"] = pd.to_numeric(num["value"], errors="coerce")
    num = num.dropna(subset=["value"])
    num = num[num["uom"].isin(["USD", "shares"])]

    df = num.merge(sub, on="adsh", how="inner")
    df["filed"] = pd.to_datetime(df["filed"], format="%Y%m%d", errors="coerce")
    df["period_end"] = pd.to_datetime(df["ddate"], format="%Y%m%d", errors="coerce")
    df["cik"] = pd.to_numeric(df["cik"], errors="coerce").astype("Int64")
    return df.dropna(subset=["filed", "cik"])[
        ["cik", "name", "form", "filed", "period_end", "qtrs", "tag", "value"]
    ]


def build(start_year: int = 2018, end_year: int | None = None, *, refresh: bool = False) -> pd.DataFrame:
    end_year = end_year or dt.date.today().year
    out = CACHE_DIR / f"fundamentals_{start_year}_{end_year}.parquet"
    if out.exists() and not refresh:
        return pd.read_parquet(out)

    frames = []
    for year in range(start_year, end_year + 1):
        for q in (1, 2, 3, 4):
            path = download_quarter(year, q)
            if path is None:
                continue
            try:
                f = parse_quarter(path)
                if len(f):
                    frames.append(f)
                    print(f"  {year}Q{q}: {len(f):>7,} facts", flush=True)
            except Exception as exc:
                print(f"  {year}Q{q}: FAILED {type(exc).__name__}: {exc}", flush=True)

    if not frames:
        raise RuntimeError("no quarters parsed")
    df = pd.concat(frames, ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out)
    return df


def cik_map() -> pd.DataFrame:
    """CIK -> ticker via SEC's public company_tickers.json (free, no key)."""
    r = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": USER_AGENT}, timeout=120,
    )
    r.raise_for_status()
    rows = [
        {"cik": int(v["cik_str"]), "symbol": str(v["ticker"]).upper(), "title": v["title"]}
        for v in r.json().values()
    ]
    return pd.DataFrame(rows).drop_duplicates("cik")
