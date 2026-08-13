"""Crypto daily-bar data layer (Alpaca /v1beta3/crypto/us/bars).

Deliberately separate from engine/data.py: the equity bars path carries
IEX-feed and adjustment parameters that the crypto endpoint does not accept,
and the equity SQLite cache schema is keyed by plain symbols while crypto
pairs carry a slash ("BTC/USD"). Nothing in engine/data.py is touched or
reused beyond the AlpacaClient plumbing (`_get`, rate limiting, retries).

Conventions
-----------
* Daily-close convention: Alpaca's crypto "1Day" bars are stamped at UTC
  midnight and cover the UTC calendar day. Every consumer of this module
  treats the UTC calendar day as "the" daily bar. The still-forming current
  UTC day is dropped by `completed_daily_closes` (mirrors
  backtest/frontier_study.py's `completed_crypto_prices`).
* History floor: verified 2026-08-12 directly against the live endpoint —
  bars begin 2021-01-01, not earlier. Requests with earlier starts simply
  return data from 2021-01-01.
* Cache: one parquet per pair under state/crypto/ (gitignored local data),
  slash replaced with underscore (BTC/USD -> BTC_USD.parquet). The fetch
  code is the committed artifact; the cache is not.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
CRYPTO_CACHE_DIR = REPO_ROOT / "state" / "crypto"

CRYPTO_BARS_PATH = "/v1beta3/crypto/us/bars"

_COLUMN_RENAMES = {
    "t": "timestamp",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "n": "trade_count",
    "vw": "vwap",
}


# --------------------------------------------------------------------------
# Pure logic — no network, unit-testable with synthetic payloads
# --------------------------------------------------------------------------


def parse_crypto_bars(rows_by_symbol: dict[str, list[dict]]) -> dict[str, pd.DataFrame]:
    """Raw API bar rows -> DataFrames, mirroring engine.data.get_bars's
    frame-building conventions (UTC DatetimeIndex named "timestamp", renamed
    OHLCV columns, sorted, last-wins de-duplication)."""
    frames: dict[str, pd.DataFrame] = {}
    for sym, rows in rows_by_symbol.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df["t"], utc=True)
        df = df.rename(columns=_COLUMN_RENAMES).set_index("timestamp").sort_index()
        frames[sym] = df[~df.index.duplicated(keep="last")]
    return frames


def completed_daily_closes(
    frame: pd.DataFrame,
    *,
    today: dt.date | None = None,
) -> pd.Series:
    """Close series excluding the still-forming current UTC day.

    Crypto trades 24/7, so the most recent "daily" bar is usually a partial
    bar for the UTC day in progress. Signals must only see completed bars.
    """
    cutoff = today or dt.datetime.now(dt.timezone.utc).date()
    close = frame["close"]
    return close[close.index.date < cutoff]


def cache_path(symbol: str, cache_dir: Path | None = None) -> Path:
    return (cache_dir or CRYPTO_CACHE_DIR) / f"{symbol.replace('/', '_')}.parquet"


def cache_is_fresh(
    cached: pd.DataFrame,
    start: dt.date,
    end: dt.date,
    *,
    start_slack_days: int = 7,
    end_slack_days: int = 3,
) -> bool:
    """Whether a cached frame already covers [start, end] closely enough to
    skip a refetch. Slack on the end side allows for the dropped partial
    current-UTC-day bar plus a couple of days of staleness."""
    if cached.empty:
        return False
    first = cached.index[0].date()
    last = cached.index[-1].date()
    return (
        first <= start + dt.timedelta(days=start_slack_days)
        and last >= end - dt.timedelta(days=end_slack_days)
    )


# --------------------------------------------------------------------------
# Network fetch + parquet cache
# --------------------------------------------------------------------------


def get_crypto_bars(
    client,
    symbols: list[str],
    start: dt.date,
    end: dt.date,
    timeframe: str = "1Day",
) -> dict[str, pd.DataFrame]:
    """Daily crypto bars keyed by pair symbol ("BTC/USD").

    Same `_get(data_base, path, params)` calling convention and pagination
    loop as engine.data.AlpacaClient.get_bars, minus the equity-only
    `feed`/`adjustment` parameters (the crypto endpoint accepts neither).
    """
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    page_token = None
    while True:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
        }
        if page_token:
            params["page_token"] = page_token
        payload = client._get(client.data_base, CRYPTO_BARS_PATH, params)
        for sym, rows in (payload.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows)
        page_token = payload.get("next_page_token")
        if not page_token:
            break
    return parse_crypto_bars(out)


def fetch_daily_bars(
    symbols: list[str],
    start: dt.date,
    end: dt.date | None = None,
    *,
    cache_dir: Path | None = None,
    refresh: bool = False,
    client=None,
) -> dict[str, pd.DataFrame]:
    """Fetch-or-load daily bars for `symbols`, cached as parquet.

    A symbol is refetched only when its cache file is missing, stale, or
    `refresh` is set; the AlpacaClient is constructed lazily so cache hits
    need no credentials (and tests can inject a stub via `client`).
    """
    cache_dir = cache_dir or CRYPTO_CACHE_DIR
    end = end or dt.datetime.now(dt.timezone.utc).date()

    frames: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []
    for sym in symbols:
        path = cache_path(sym, cache_dir)
        if not refresh and path.exists():
            cached = pd.read_parquet(path)
            if cache_is_fresh(cached, start, end):
                frames[sym] = cached
                continue
        to_fetch.append(sym)

    if to_fetch:
        if client is None:
            from engine.data import AlpacaClient

            client = AlpacaClient()
        fetched = get_crypto_bars(client, to_fetch, start, end)
        missing = sorted(set(to_fetch) - set(fetched))
        if missing:
            raise RuntimeError(f"crypto bars endpoint returned no data for: {missing}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        for sym, df in fetched.items():
            df.to_parquet(cache_path(sym, cache_dir))
            frames[sym] = df
    return {sym: frames[sym] for sym in symbols if sym in frames}
