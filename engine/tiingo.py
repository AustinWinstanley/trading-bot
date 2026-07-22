"""Tiingo historical price provider.

Alpaca's free IEX feed only carries clean daily bars from 2021, which is not
enough history to test a strategy against a real crash. Tiingo's free tier
gives 30+ years of adjusted end-of-day data at 50 symbols/hour, which covers
the whole universe in a single pull.

This provider is used for **backtesting only**. Live signal generation reads
Alpaca, so that the live path uses the same feed the orders are priced against.

Prices are split- and dividend-adjusted (`adj*` fields). Backtesting on raw
closes would read every split as a crash and every dividend as a loss.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import requests

from engine.data import REPO_ROOT, RateLimiter, load_env

TIINGO_BASE = "https://api.tiingo.com"


class TiingoError(RuntimeError):
    pass


class TiingoClient:
    def __init__(self, token: str | None = None):
        load_env()
        self.token = token or os.environ.get("TIINGO_API_TOKEN", "")
        if not self.token:
            raise TiingoError(
                "TIINGO_API_TOKEN not set. Add it to .env — free key at "
                "https://www.tiingo.com/account/api/token"
            )
        # Free tier is 50 symbols/hour; stay well under it.
        self._limiter = RateLimiter(max_calls=45, period=3600.0)
        self._session = requests.Session()
        self._session.headers.update(
            {"Content-Type": "application/json", "Authorization": f"Token {self.token}"}
        )

    def check_auth(self) -> dict:
        r = self._session.get(f"{TIINGO_BASE}/api/test", timeout=20)
        if r.status_code != 200:
            raise TiingoError(f"auth check failed: {r.status_code} {r.text[:200]}")
        return {"ok": True}

    def get_daily(
        self, symbol: str, start: dt.date, end: dt.date, *, adjusted: bool = True
    ) -> pd.DataFrame:
        self._limiter.acquire()
        url = f"{TIINGO_BASE}/tiingo/daily/{symbol.lower()}/prices"
        params = {"startDate": start.isoformat(), "endDate": end.isoformat()}

        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                r = self._session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if r.status_code == 404:
                return pd.DataFrame()
            if r.status_code == 429:
                time.sleep(min(60 * (attempt + 1), 180))
                continue
            if r.status_code >= 400:
                raise TiingoError(f"{symbol}: {r.status_code} {r.text[:300]}")
            rows = r.json()
            break
        else:
            raise TiingoError(f"{symbol}: exhausted retries ({last_exc})")

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["date"], utc=True)
        prefix = "adj" if adjusted else ""
        cols = {
            f"{prefix}Open" if adjusted else "open": "open",
            f"{prefix}High" if adjusted else "high": "high",
            f"{prefix}Low" if adjusted else "low": "low",
            f"{prefix}Close" if adjusted else "close": "close",
            f"{prefix}Volume" if adjusted else "volume": "volume",
        }
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise TiingoError(f"{symbol}: response missing columns {missing}")

        out = df[["timestamp", *cols.keys()]].rename(columns=cols)
        return out.set_index("timestamp").sort_index()

    def get_bars(
        self, symbols: list[str], start: dt.date, end: dt.date, *, adjusted: bool = True
    ) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for i, sym in enumerate(symbols, 1):
            df = self.get_daily(sym, start, end, adjusted=adjusted)
            if not df.empty:
                frames[sym] = df
            print(
                f"  [{i}/{len(symbols)}] {sym:6} {len(df):5} bars"
                + (f"  {df.index.min().date()} -> {df.index.max().date()}" if len(df) else "  (none)"),
                flush=True,
            )
        return frames


def backtest_universe(cfg) -> list[str]:
    """Every symbol the backtest needs, deduplicated."""
    syms = set(cfg.sleeves["momentum"]["universe"])
    syms |= set(cfg.sleeves["leveraged"]["symbols"])
    syms.add(cfg.sleeves["momentum"].get("risk_off_symbol", "SHY"))
    syms.add(cfg.sleeves["leveraged"].get("trend_symbol", "QQQ"))
    syms.add("SPY")  # regime benchmark
    return sorted(syms)


def save_parquet(frames: dict[str, pd.DataFrame], out_dir: Path | None = None) -> Path:
    out_dir = out_dir or REPO_ROOT / "state" / "history"
    out_dir.mkdir(parents=True, exist_ok=True)
    for sym, df in frames.items():
        df.to_parquet(out_dir / f"{sym}.parquet")
    return out_dir


def load_parquet(symbols: list[str], out_dir: Path | None = None) -> dict[str, pd.DataFrame]:
    out_dir = out_dir or REPO_ROOT / "state" / "history"
    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        p = out_dir / f"{sym}.parquet"
        if p.exists():
            frames[sym] = pd.read_parquet(p)
    return frames
