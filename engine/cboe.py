"""CBOE index history (free, no key).

Gives the two things a volatility-risk-premium test needs and that option-chain
data cannot provide at this account's budget:

  VIX  1990+  implied volatility, for measuring the premium directly
  PUT  1991+  CBOE S&P 500 PutWrite Index — the canonical cash-secured
              put-writing strategy, 35 years including 1987-adjacent regimes,
              2000-02, 2008, 2020 and 2022
  BXM  2002+  CBOE S&P 500 BuyWrite (covered call) Index

Additional benchmark symbols are loaded on demand by the options strategy
study. They include protective puts, collars, defined-risk spreads, and
alternative option-writing constructions.

Alpaca's option-chain history starts around Feb 2024. Testing a short-volatility
strategy on 2.5 years containing no volatility event would be worse than not
testing it: short vol produces smooth returns right up until it doesn't, so a
crash-free sample flatters it precisely where the risk lives.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

from engine.data import REPO_ROOT

BASE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CACHE_DIR = REPO_ROOT / "state" / "cboe"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

SERIES = {
    name: name
    for name in (
        "PUT", "BXM", "VVIX", "PPUT", "CLL", "CLLZ", "CNDR", "BFLY",
        "BXMH", "BXMD", "PUTY", "WPUT",
    )
}
SERIES["VIX"] = "CLOSE"


def fetch(name: str, *, refresh: bool = False) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / f"{name}.csv"
    if refresh or not dest.exists():
        r = requests.get(BASE.format(name=name), headers={"User-Agent": UA}, timeout=120)
        r.raise_for_status()
        dest.write_bytes(r.content)

    df = pd.read_csv(dest)
    df.columns = [c.strip().upper() for c in df.columns]
    df["DATE"] = pd.to_datetime(df["DATE"], format="%m/%d/%Y", errors="coerce")
    df = df.dropna(subset=["DATE"]).set_index("DATE").sort_index()
    df.index = df.index.tz_localize("UTC")
    return df


def series(name: str, *, refresh: bool = False, daily_only: bool = True) -> pd.Series:
    """One CBOE index series.

    `daily_only` trims the leading sparse region. CBOE backfilled only a
    handful of reference points for PUT before it went daily — 2 observations
    in 1991, 1 in 1994, a 1,159-day gap — and computing returns across those
    gaps silently produces nonsense (a 3-year "daily" return). Anything
    downstream that annualises or takes a standard deviation must see a real
    daily series or not see it at all.
    """
    df = fetch(name, refresh=refresh)
    col = SERIES.get(name, name)
    if col not in df.columns:
        col = df.columns[-1]
    s = pd.to_numeric(df[col], errors="coerce").dropna().rename(name)
    if daily_only:
        s = s.loc[first_daily_date(s):]
    return s


def first_daily_date(s: pd.Series, min_obs_per_year: int = 200) -> pd.Timestamp:
    """First year the series actually has daily coverage."""
    counts = s.groupby(s.index.year).size()
    good = counts[counts >= min_obs_per_year]
    if good.empty:
        return s.index[0]
    return pd.Timestamp(f"{int(good.index[0])}-01-01", tz="UTC")


def load_all(*, refresh: bool = False) -> dict[str, pd.Series]:
    return {n: series(n, refresh=refresh) for n in ("VIX", "PUT", "BXM")}
