"""Shared, causality-safe intraday ETF research machinery.

Signals are observed on completed five-minute bars and can enter only at the
next bar's open.  Positions always close in the same regular session.  The
primary convention charges 2 bps per leg for highly liquid index ETFs; studies
must also report a 5 bps-per-leg stress case before promotion.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from engine.data import AlpacaClient, REPO_ROOT

SYMBOLS = ("SPY", "QQQ", "IWM")
PRIMARY_COST_BPS_PER_LEG = 2.0
STRESS_COST_BPS_PER_LEG = 5.0


def prepare_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Return regular-session bars with stable session/bar coordinates."""
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    index = pd.DatetimeIndex(out.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    out.index = index.tz_convert("America/New_York")
    out = out.between_time("09:30", "15:59", inclusive="both")
    out["session"] = pd.Index(out.index.date).astype(str)
    out["session_bar"] = out.groupby("session").cumcount()
    typical = (out["high"] + out["low"] + out["close"]) / 3
    cumulative_value = (typical * out["volume"]).groupby(out["session"]).cumsum()
    cumulative_volume = out["volume"].groupby(out["session"]).cumsum()
    out["session_vwap"] = cumulative_value / cumulative_volume.replace(0, np.nan)
    out["bar_return"] = out.groupby("session")["close"].pct_change()
    return out


def cache_path(symbol: str, start: dt.date, end: dt.date) -> Path:
    return REPO_ROOT / "state/intraday" / f"{symbol}_5Min_{start}_{end}.parquet"


def load_or_fetch(
    symbols: tuple[str, ...] = SYMBOLS,
    *,
    start: dt.date,
    end: dt.date,
    refresh: bool = False,
    client: AlpacaClient | None = None,
) -> dict[str, pd.DataFrame]:
    cached = {}
    missing = []
    for symbol in symbols:
        path = cache_path(symbol, start, end)
        if path.exists() and not refresh:
            cached[symbol] = prepare_bars(pd.read_parquet(path))
        else:
            missing.append(symbol)
    if missing:
        fetched = (client or AlpacaClient()).get_bars(
            missing, start, end, timeframe="5Min", adjustment="all"
        )
        for symbol in missing:
            frame = fetched.get(symbol, pd.DataFrame())
            path = cache_path(symbol, start, end)
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(path)
            cached[symbol] = prepare_bars(frame)
    return cached


def simulate_fixed_horizon(
    bars: pd.DataFrame,
    signal: pd.Series,
    *,
    hold_bars: int,
    cost_bps_per_leg: float = PRIMARY_COST_BPS_PER_LEG,
) -> pd.DataFrame:
    """Enter next open after each nonzero signal and close in-session.

    Only one trade per session is accepted.  This keeps candidates comparable
    and prevents repeated correlated signals from manufacturing sample size.
    """
    if hold_bars < 1:
        raise ValueError("hold_bars must be positive")
    rows = []
    aligned = signal.reindex(bars.index).fillna(0)
    for session, day in bars.groupby("session", sort=True):
        candidates = np.flatnonzero(aligned.loc[day.index].to_numpy() != 0)
        if not len(candidates):
            continue
        signal_pos = int(candidates[0])
        entry_pos = signal_pos + 1
        if entry_pos >= len(day):
            continue
        exit_pos = min(entry_pos + hold_bars - 1, len(day) - 1)
        direction = int(np.sign(aligned.loc[day.index[signal_pos]]))
        entry = float(day.iloc[entry_pos]["open"])
        exit_price = float(day.iloc[exit_pos]["close"])
        gross = direction * (exit_price / entry - 1)
        net = gross - 2 * cost_bps_per_leg / 10_000
        rows.append({
            "session": session,
            "signal_ts": day.index[signal_pos].isoformat(),
            "entry_ts": day.index[entry_pos].isoformat(),
            "exit_ts": day.index[exit_pos].isoformat(),
            "direction": direction,
            "entry_price": entry,
            "exit_price": exit_price,
            "gross_return": gross,
            "net_return": net,
        })
    return pd.DataFrame(rows)


def trade_summary(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "mean_return": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "max_drawdown": 0.0}
    returns = trades["net_return"].astype(float)
    equity = (1 + returns).cumprod()
    losses = -returns[returns < 0].sum()
    return {
        "trades": len(returns),
        "mean_return": round(float(returns.mean()), 6),
        "win_rate": round(float((returns > 0).mean()), 4),
        "profit_factor": round(float(returns[returns > 0].sum() / losses), 3)
        if losses > 0 else None,
        "max_drawdown": round(float((equity / equity.cummax() - 1).min()), 4),
        "total_return": round(float(equity.iloc[-1] - 1), 4),
    }
