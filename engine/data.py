"""Market data layer.

Alpaca for bars / calendar / account, SEC EDGAR for filings. Yahoo and Stooq are
IP-blocked from this host, so nothing here may depend on them.

Free-tier constraints this module is built around:
  * IEX feed only (~8-10% of consolidated volume) — fine for daily bars, thin
    for intraday microstructure. Every strategy here is daily-bar based.
  * 200 requests/minute — the bars endpoint takes comma-separated symbols, so
    batch rather than looping one symbol at a time.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DB = REPO_ROOT / "state" / "bars_cache.db"

_ENV_LOADED = False


def load_env(path: Path | None = None) -> None:
    """Minimal .env loader — avoids a python-dotenv dependency."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    path = path or REPO_ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    _ENV_LOADED = True


class RateLimiter:
    """Token-bucket-ish limiter. Alpaca's free tier allows 200 req/min."""

    def __init__(self, max_calls: int = 180, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > self.period:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_for = self.period - (now - self._calls[0]) + 0.05
                time.sleep(max(sleep_for, 0))
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.period:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


class AlpacaError(RuntimeError):
    pass


class AlpacaClient:
    def __init__(self, key: str | None = None, secret: str | None = None, *, paper: bool = True):
        load_env()
        self.key = key or os.environ.get("ALPACA_API_KEY", "")
        self.secret = secret or os.environ.get("ALPACA_API_SECRET", "")
        if not self.key or not self.secret:
            raise AlpacaError("ALPACA_API_KEY / ALPACA_API_SECRET not set (see .env)")
        self.trading_base = os.environ.get(
            "ALPACA_PAPER_BASE" if paper else "ALPACA_LIVE_BASE",
            "https://paper-api.alpaca.markets",
        )
        self.data_base = os.environ.get("ALPACA_DATA_BASE", "https://data.alpaca.markets")
        self.feed = os.environ.get("ALPACA_FEED", "iex")
        self._limiter = RateLimiter()
        self._session = requests.Session()
        self._session.headers.update(
            {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        )

    # -- plumbing ---------------------------------------------------------

    def _get(self, base: str, path: str, params: dict | None = None, *, timeout: int = 30) -> dict:
        self._limiter.acquire()
        url = f"{base}{path}"
        for attempt in range(4):
            try:
                r = self._session.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise AlpacaError(f"GET {path} failed after retries: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if r.status_code == 429:
                time.sleep(2**attempt + 1)
                continue
            if r.status_code >= 500:
                if attempt == 3:
                    raise AlpacaError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
                time.sleep(2**attempt)
                continue
            if r.status_code >= 400:
                raise AlpacaError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise AlpacaError(f"GET {path} exhausted retries")

    # -- account ----------------------------------------------------------

    def get_account(self) -> dict:
        return self._get(self.trading_base, "/v2/account")

    def get_positions(self) -> list[dict]:
        return self._get(self.trading_base, "/v2/positions")  # type: ignore[return-value]

    def get_asset(self, symbol: str) -> dict:
        return self._get(self.trading_base, f"/v2/assets/{symbol}")

    # -- calendar ---------------------------------------------------------

    def get_calendar(self, start: dt.date, end: dt.date) -> list[dict]:
        return self._get(  # type: ignore[return-value]
            self.trading_base,
            "/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )

    def is_trading_day(self, day: dt.date) -> bool:
        cal = self.get_calendar(day, day)
        return any(c.get("date") == day.isoformat() for c in cal)

    # -- bars -------------------------------------------------------------

    def get_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        timeframe: str = "1Day",
        adjustment: str = "all",
    ) -> dict[str, pd.DataFrame]:
        """Daily bars keyed by symbol. Handles pagination and symbol batching."""
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        # The API accepts many symbols per call; keep the URL a sane length.
        for i in range(0, len(symbols), 50):
            batch = symbols[i : i + 50]
            page_token = None
            while True:
                params = {
                    "symbols": ",".join(batch),
                    "timeframe": timeframe,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "adjustment": adjustment,
                    "feed": self.feed,
                    "limit": 10000,
                }
                if page_token:
                    params["page_token"] = page_token
                payload = self._get(self.data_base, "/v2/stocks/bars", params)
                for sym, rows in (payload.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(rows)
                page_token = payload.get("next_page_token")
                if not page_token:
                    break

        frames: dict[str, pd.DataFrame] = {}
        for sym, rows in out.items():
            if not rows:
                continue
            df = pd.DataFrame(rows)
            df["t"] = pd.to_datetime(df["t"], utc=True)
            df = (
                df.rename(
                    columns={
                        "t": "timestamp",
                        "o": "open",
                        "h": "high",
                        "l": "low",
                        "c": "close",
                        "v": "volume",
                        "n": "trade_count",
                        "vw": "vwap",
                    }
                )
                .set_index("timestamp")
                .sort_index()
            )
            frames[sym] = df[~df.index.duplicated(keep="last")]
        return frames


# --------------------------------------------------------------------------
# Indicators — pure functions over a bars frame, easy to unit test
# --------------------------------------------------------------------------


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 2) -> pd.Series:
    """Wilder RSI. Period 2 is the mean-reversion sleeve's signal."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    # All-gain windows have no downside; RSI is 100 by definition there.
    return out.fillna(100.0).where(avg_loss.notna(), other=pd.NA)


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def avg_dollar_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return (df["close"] * df["volume"]).rolling(period, min_periods=period).mean()


def momentum_skip(close: pd.Series, lookback_days: int, skip_days: int) -> pd.Series:
    """Return over `lookback_days`, skipping the most recent `skip_days`.

    Skipping the last month is what separates the momentum anomaly from
    short-term reversal, which points the other way.
    """
    past = close.shift(lookback_days)
    recent = close.shift(skip_days)
    return (recent / past) - 1.0


# --------------------------------------------------------------------------
# Bar cache — avoids re-pulling history on every backtest run
# --------------------------------------------------------------------------


def _cache_conn() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
               symbol TEXT NOT NULL,
               ts     TEXT NOT NULL,
               open REAL, high REAL, low REAL, close REAL,
               volume REAL, trade_count REAL, vwap REAL,
               PRIMARY KEY (symbol, ts)
           )"""
    )
    return conn


def cache_bars(frames: dict[str, pd.DataFrame]) -> int:
    written = 0
    with _cache_conn() as conn:
        for sym, df in frames.items():
            rows = [
                (
                    sym,
                    ts.isoformat(),
                    float(r.get("open", 0)),
                    float(r.get("high", 0)),
                    float(r.get("low", 0)),
                    float(r.get("close", 0)),
                    float(r.get("volume", 0)),
                    float(r.get("trade_count", 0) or 0),
                    float(r.get("vwap", 0) or 0),
                )
                for ts, r in df.to_dict("index").items()
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
            written += len(rows)
    return written


def load_cached_bars(symbol: str) -> pd.DataFrame:
    with _cache_conn() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM bars WHERE symbol = ? ORDER BY ts", conn, params=(symbol,)
        )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["ts"], utc=True)
    return df.drop(columns=["ts", "symbol"]).set_index("timestamp")
