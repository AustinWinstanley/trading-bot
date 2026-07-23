"""Pre-registered conservative liquid ETF-pairs study.

Pairs are selected structurally, not by searching historical correlations:
SPY/IVV track US large caps and GLD/IAU track gold. Each pair uses a 60-session
log-price-ratio z-score, enters at 2 standard deviations, exits inside 0.5,
stops at 4, and has a 20-session time stop.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.account_mandate_study import solve_profile
from backtest.production_portfolio import SHORT_BORROW, TD, build_streams, norm_index, returns_summary
from backtest.xsec_data import load

PAIRS = (("SPY", "IVV"), ("GLD", "IAU"))


def pair_state(
    left: pd.Series,
    right: pd.Series,
    *,
    lookback: int = 60,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    stop_z: float = 4.0,
    max_hold: int = 20,
) -> tuple[pd.Series, pd.Series]:
    ratio = np.log(left) - np.log(right)
    z = (ratio - ratio.rolling(lookback).mean()) / ratio.rolling(lookback).std()
    state = pd.Series(0, index=ratio.index, dtype="int8")
    current = 0
    held = 0
    for date, value in z.items():
        if pd.isna(value):
            state.at[date] = 0
            continue
        if current:
            held += 1
            if abs(value) <= exit_z or abs(value) >= stop_z or held >= max_hold:
                current, held = 0, 0
        if current == 0:
            if value >= entry_z:
                current, held = -1, 0
            elif value <= -entry_z:
                current, held = 1, 0
        state.at[date] = current
    return state, z


def build_pairs_overlay(
    close: pd.DataFrame,
    *,
    equity: pd.Series | float = 10_000.0,
    gross_limit: float = 0.20,
    cost_bps: float = 2.0,
) -> tuple[pd.Series, pd.DataFrame, dict]:
    index = close.index
    equity_series = (
        pd.Series(float(equity), index=index)
        if np.isscalar(equity)
        else equity.reindex(index).ffill().bfill()
    )
    weights = pd.DataFrame(0.0, index=index, columns=close.columns)
    diagnostics = {}
    pair_gross = gross_limit / len(PAIRS)
    leg_weight = pair_gross / 2
    for left, right in PAIRS:
        state, z = pair_state(close[left], close[right])
        left_desired = state * leg_weight
        right_desired = -state * leg_weight
        for symbol, desired in ((left, left_desired), (right, right_desired)):
            long = desired.clip(lower=0)
            short = desired.clip(upper=0)
            desired_dollars = equity_series * short.abs()
            shares = np.floor(desired_dollars / close[symbol])
            capacity = (shares * close[symbol]).div(
                desired_dollars.where(desired_dollars > 0)
            ).fillna(0)
            weights[symbol] += long + short * capacity
        diagnostics[f"{left}/{right}"] = {
            "active_days": int(state.ne(0).sum()),
            "entries": int((state.ne(0) & state.shift(1).fillna(0).eq(0)).sum()),
            "max_abs_z": round(float(z.abs().max()), 3),
        }
    returns = close.pct_change(fill_method=None)
    gross = (weights.shift(1) * returns).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    short_gross = -weights.clip(upper=0).sum(axis=1).shift(1).fillna(0)
    net = gross - turnover * cost_bps / 10_000 - short_gross * SHORT_BORROW / TD
    diagnostics["portfolio"] = {
        "one_way_turnover": round(float(turnover.sum()), 2),
        "average_gross": round(float(weights.abs().sum(axis=1).mean()), 4),
        "average_short_gross": round(float(short_gross.mean()), 4),
        "zero_short_pair_days": int(
            ((weights.clip(upper=0).eq(0).all(axis=1)) & (weights.abs().sum(axis=1) > 0)).sum()
        ),
    }
    return net, weights, diagnostics


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    pair_close = close_all[[symbol for pair in PAIRS for symbol in pair]]
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    streams = build_streams()
    current = {"spy": 0.40, "tsmom": 0.25, "trend": 0.20, "mom_ls": 0.30}
    base = solve_profile(
        close_all[stocks],
        volume_all[stocks],
        streams,
        profile="base",
        weights=current,
    )
    overlay, _, diagnostics = build_pairs_overlay(pair_close)
    candidate = base.add(overlay, fill_value=0).reindex(base.index)
    windows = {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }
    performance = {}
    gates = {}
    passed = True
    for window, slicer in windows.items():
        performance[window] = [
            returns_summary(base.loc[slicer], "base control"),
            returns_summary(candidate.loc[slicer], "base plus liquid pairs"),
            returns_summary(overlay.loc[slicer], "pairs standalone"),
        ]
        if window != "full":
            control, proposed = performance[window][:2]
            checks = {
                "cagr_improves": proposed["cagr"] > control["cagr"],
                "sharpe_improves": proposed["sharpe"] > control["sharpe"],
                "max_drawdown_not_worse": abs(proposed["max_dd"])
                <= abs(control["max_dd"]),
            }
            gates[window] = checks
            passed = passed and all(checks.values())
    payload = {
        "pre_registration": {
            "pairs": [list(pair) for pair in PAIRS],
            "rules": {
                "ratio_lookback": 60,
                "entry_z": 2.0,
                "exit_z": 0.5,
                "stop_z": 4.0,
                "max_hold_sessions": 20,
                "maximum_total_gross": 0.20,
            },
            "promotion_rule": "Higher base CAGR and Sharpe with no worse max drawdown in both early and held-out windows.",
        },
        "decision": "promote" if passed else "reject",
        "gates": gates,
        "performance": performance,
        "diagnostics": diagnostics,
        "limitations": [
            "ETF adjusted closes are used; intraday entry slippage is not modeled beyond 2 bps turnover cost.",
            "Whole-share capacity is modeled only for shorts; Alpaca permits fractional ETF longs.",
            "Historical easy-to-borrow availability is unavailable.",
            "A fixed $10,000 account is used for pair short capacity rather than iterating capacity with equity.",
        ],
        "research_basis": {
            "paper": "Pairs Trading: Performance of a Relative-Value Arbitrage Rule",
            "url": "https://www.nber.org/papers/w7032",
        },
    }
    out = Path("reports/liquid_pairs_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision: {payload['decision'].upper()}")
    print(json.dumps(gates, indent=2))
    print(json.dumps(diagnostics, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
