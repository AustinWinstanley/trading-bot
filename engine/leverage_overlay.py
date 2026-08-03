"""Paper leverage scaling from realized account volatility."""

from __future__ import annotations

import math
import sqlite3

import numpy as np
import pandas as pd

TD = 252


def _fixed_exposure_returns(
    conn: sqlite3.Connection,
    daily_equity: pd.Series,
) -> pd.Series:
    """Approximate fixed-profile returns after any prior active scaling.

    The return between two snapshots was earned under the scale selected at
    the preceding snapshot. Dividing by that scale avoids the feedback loop
    where de-risking lowers observed volatility and immediately recommends
    restoring full leverage.
    """
    returns = daily_equity.pct_change(fill_method=None)
    try:
        rows = conn.execute(
            "SELECT ts, mode, ready, recommended_scale "
            "FROM leverage_recommendations ORDER BY ts"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return returns
    frame = pd.DataFrame(
        rows, columns=["ts", "mode", "ready", "recommended_scale"]
    )
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["ts"]).sort_values("ts")
    frame["date"] = frame["ts"].dt.date
    frame["effective_scale"] = np.where(
        (frame["mode"] == "active") & frame["ready"].astype(bool),
        pd.to_numeric(frame["recommended_scale"], errors="coerce"),
        1.0,
    )
    scale = (
        frame.groupby("date", sort=True)["effective_scale"].last()
        .reindex(daily_equity.index)
        .ffill()
        .shift(1)
        .fillna(1.0)
        .clip(lower=0.01, upper=1.0)
    )
    return returns / scale


def apply_target_scale(
    targets: dict[str, float],
    diag: dict,
    overlay: dict,
) -> tuple[dict[str, float], dict]:
    """Apply an active overlay to target and attribution weights."""
    scale = float(overlay.get("applied_scale", 1.0))
    if scale >= 1.0:
        return targets, diag
    scaled = {symbol: weight * scale for symbol, weight in targets.items()}
    updated = dict(diag)
    updated["sleeve_targets"] = {
        sleeve: {
            symbol: round(float(weight) * scale, 8)
            for symbol, weight in weights.items()
        }
        for sleeve, weights in diag.get("sleeve_targets", {}).items()
    }
    updated["sleeve_weights"] = {
        sleeve: round(float(weight) * scale, 4)
        for sleeve, weight in diag.get("sleeve_weights", {}).items()
    }
    long_total = sum(weight for weight in scaled.values() if weight > 0)
    short_total = sum(-weight for weight in scaled.values() if weight < 0)
    updated.update({
        "long_weight": round(long_total, 4),
        "short_weight": round(short_total, 4),
        "total_weight": round(long_total, 4),
        "gross_leverage": round(float(
            overlay.get("applied_leverage", diag.get("gross_leverage", 1.0))
        ), 6),
        "cash_weight": round(1 - long_total, 4),
        "overlay_scale": round(scale, 6),
    })
    return scaled, updated


def recommend_leverage(
    conn: sqlite3.Connection,
    *,
    current_ts: str,
    current_equity: float,
    fixed_leverage: float,
    settings: dict,
) -> dict:
    """Return a bounded recommendation and the scale active mode may apply."""
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
        "applied_scale": 1.0,
        "applied_leverage": fixed_leverage,
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
    returns = _fixed_exposure_returns(conn, daily).dropna().tail(lookback)
    base["observations"] = len(returns)
    if len(returns) < min_observations:
        return base

    realized = float(returns.std(ddof=1) * math.sqrt(TD))
    if not math.isfinite(realized) or realized <= 0:
        base["reason"] = "realized volatility unavailable"
        return base
    scale = min(max(target_vol / realized, min_scale), 1.0)
    applied_scale = scale if mode == "active" else 1.0
    base.update({
        "realized_vol": round(realized, 6),
        "recommended_scale": round(scale, 6),
        "recommended_leverage": round(fixed_leverage * scale, 6),
        "applied_scale": round(applied_scale, 6),
        "applied_leverage": round(fixed_leverage * applied_scale, 6),
        "ready": True,
        "reason": (
            "active target scaling"
            if mode == "active" else "shadow recommendation only"
        ),
    })
    return base
