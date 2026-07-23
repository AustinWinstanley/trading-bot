"""Paper-only leverage recommendation from realized account volatility."""

from __future__ import annotations

import math
import sqlite3

import pandas as pd

TD = 252


def recommend_leverage(
    conn: sqlite3.Connection,
    *,
    current_ts: str,
    current_equity: float,
    fixed_leverage: float,
    settings: dict,
) -> dict:
    """Return a shadow recommendation; never mutates config or targets."""
    mode = str(settings.get("mode", "off"))
    target_vol = float(settings.get("target_vol", 0.12))
    lookback = int(settings.get("lookback_days", 63))
    min_observations = int(settings.get("min_observations", 32))
    min_scale = float(settings.get("min_scale", 0.25))
    base = {
        "mode": mode,
        "target_vol": target_vol,
        "lookback_days": lookback,
        "min_observations": min_observations,
        "observations": 0,
        "realized_vol": None,
        "recommended_scale": 1.0,
        "recommended_leverage": fixed_leverage,
        "ready": False,
        "reason": "overlay off" if mode == "off" else "insufficient observations",
    }
    if mode == "off":
        return base

    rows = conn.execute(
        "SELECT ts, equity FROM snapshots WHERE equity > 0 ORDER BY ts"
    ).fetchall()
    rows.append((current_ts, current_equity))
    frame = pd.DataFrame(rows, columns=["ts", "equity"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, errors="coerce")
    frame["equity"] = pd.to_numeric(frame["equity"], errors="coerce")
    frame = frame.dropna().sort_values("ts")
    if frame.empty:
        return base
    # A retry or manual run on the same date is not a new daily observation.
    frame["date"] = frame["ts"].dt.date
    daily = frame.groupby("date", sort=True)["equity"].last()
    returns = daily.pct_change(fill_method=None).dropna().tail(lookback)
    base["observations"] = len(returns)
    if len(returns) < min_observations:
        return base

    realized = float(returns.std(ddof=1) * math.sqrt(TD))
    if not math.isfinite(realized) or realized <= 0:
        base["reason"] = "realized volatility unavailable"
        return base
    scale = min(max(target_vol / realized, min_scale), 1.0)
    base.update({
        "realized_vol": round(realized, 6),
        "recommended_scale": round(scale, 6),
        "recommended_leverage": round(fixed_leverage * scale, 6),
        "ready": True,
        "reason": "shadow recommendation only",
    })
    return base
