"""Pre-registered TSMOM liquidity-alignment study.

Production currently computes targets without liquidity, then the risk gate
rejects an asset below its $1m 20-day dollar-volume floor.  The rejected
weight stays in cash.  This candidate applies that same floor using prior-day
data before inverse-vol normalization, so only targets the gate can accept
share the sleeve.  Objective class: return_enhancer.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    require_history,
    returns_summary,
    tsmom_stream,
)
from backtest.promotion import passes_gate_all_cells
from engine.config import load_config
from engine.tiingo import load_parquet

WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}


def build_tsmom(close: pd.DataFrame, volume: pd.DataFrame, *, liquidity_floor: float | None) -> tuple[pd.Series, pd.DataFrame]:
    returns = close.pct_change(fill_method=None)
    signal = (close / close.shift(252) - 1).gt(0)
    inverse_vol = 1 / (
        returns.rolling(63, min_periods=40).std() * np.sqrt(TD)
    ).clip(lower=0.04)
    if liquidity_floor is not None:
        liquid = (close * volume).rolling(20, min_periods=20).mean().ge(liquidity_floor)
        signal &= liquid
    weights = signal * inverse_vol
    weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    weights = weights.shift(1).fillna(0)
    gross = (weights * returns).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    return gross - turnover * 8 / 10_000, weights


def main() -> None:
    cfg = load_config()
    symbols = cfg.sleeves_paper["tsmom_universe"]
    floor = float(cfg.sleeves_paper["tsmom_min_dollar_volume"])
    frames = load_parquet(symbols, Path("state/history_assets"))
    require_history(symbols, frames, label="TSMOM")
    close = pd.DataFrame({s: norm_index(frame["close"]) for s, frame in frames.items()})
    volume = pd.DataFrame({s: norm_index(frame["volume"]) for s, frame in frames.items()})

    control, control_weights = build_tsmom(close, volume, liquidity_floor=None)
    accepted = norm_index(tsmom_stream()).reindex(control.index)
    control_error = float((control - accepted).abs().max())
    if control_error > 1e-12:
        raise AssertionError(f"TSMOM control mismatch: {control_error:.3g}")
    candidate, candidate_weights = build_tsmom(close, volume, liquidity_floor=floor)

    production = build_streams()
    core = 0.40 * production["spy"] + 0.20 * production["trend"] + 0.30 * production["mom_ls"]
    variants = {"post-target gate control": control, "pre-target liquidity alignment": candidate}
    profiles = {}
    for profile, scale in (("base", 1.0), ("2x", 2.0)):
        financing = MARGIN_RATE / TD if profile == "2x" else 0.0
        borrow = (0.30 if profile == "2x" else 0.15) * SHORT_BORROW / TD
        for label, stream in variants.items():
            aligned = pd.concat(
                {"core": core, "tsmom": stream}, axis=1, sort=False
            ).dropna()
            profiles[(profile, label)] = scale * (
                aligned["core"] + 0.25 * aligned["tsmom"]
            ) - financing - borrow

    performance = {}
    cells = []
    for window, slicer in WINDOWS.items():
        rows = []
        for (profile, label), returns in profiles.items():
            row = returns_summary(returns.loc[slicer], f"{profile} — {label}")
            row.update(profile=profile, variant=label)
            rows.append(row)
        performance[window] = rows
        for profile in ("base", "2x"):
            by_variant = {r["variant"]: r for r in rows if r["profile"] == profile}
            cells.append((
                window,
                profile,
                by_variant["post-target gate control"],
                by_variant["pre-target liquidity alignment"],
            ))

    gate = passes_gate_all_cells(cells, "return_enhancer")
    removed = control_weights.gt(0) & candidate_weights.eq(0)
    removed_days = {s: int(removed[s].sum()) for s in symbols if removed[s].any()}
    payload = {
        "decision": "promote_to_shadow" if gate["passed"] else "reject",
        "pre_registration": {
            "objective_class": "return_enhancer",
            "candidate": "Apply the existing $1m 20-day dollar-volume floor before TSMOM normalization using only information available before the trading day.",
            "promotion_rule": "Higher Sharpe, no lower CAGR and no worse max drawdown in both profiles and both screening windows.",
        },
        "control_validation_max_abs_daily_return_error": control_error,
        "promotion_gate": gate,
        "performance": performance,
        "diagnostics": {
            "liquidity_floor": floor,
            "positive_signal_asset_days_removed": removed_days,
            "total_asset_days_removed": int(removed.sum().sum()),
        },
        "limitations": [
            "Tiingo end-of-day volume may differ from Alpaca IEX volume used by the live gate.",
            "The study treats a rejected allocation as cash in the conceptual control, while the accepted headline backtest historically assumes it fills.",
            "2026-08-04 onward remains frozen final validation and is not used for tuning.",
        ],
    }
    out = Path("reports/tsmom_liquidity_alignment_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision: {payload['decision']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
