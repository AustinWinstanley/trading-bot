"""Pre-registered SPY/QQQ/IWM intraday strategy-family screen.

One fixed specification per family is evaluated; there is no parameter grid.
Every signal enters at the next five-minute open through ``backtest.intraday``.
A family advances only if every ETF in both temporal windows has positive mean
return and profit factor above one under the 5 bps-per-leg stress cost, with at
least 30 trades per window.  This is a screen, not final validation.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.intraday import (
    PRIMARY_COST_BPS_PER_LEG,
    STRESS_COST_BPS_PER_LEG,
    SYMBOLS,
    load_or_fetch,
    simulate_fixed_horizon,
    trade_summary,
)

START = dt.date(2024, 2, 1)
END = dt.date(2026, 8, 1)
WINDOWS = {
    "design_2024_through_2025q1": ("2024-02-01", "2025-03-31"),
    "screen_2025q2_through_2026q2": ("2025-04-01", "2026-07-31"),
}


def _first_true_per_session(condition: pd.Series, bars: pd.DataFrame) -> pd.Series:
    result = pd.Series(False, index=bars.index)
    for _, day in bars.groupby("session"):
        hits = condition.loc[day.index]
        if hits.any():
            result.loc[hits[hits].index[0]] = True
    return result


def opening_range_signal(bars: pd.DataFrame) -> pd.Series:
    opening = bars["session_bar"] <= 5
    high = bars["high"].where(opening).groupby(bars["session"]).transform("max")
    low = bars["low"].where(opening).groupby(bars["session"]).transform("min")
    eligible = bars["session_bar"].between(6, 23)
    volume_gate = bars["volume"] > bars.groupby("session")["volume"].transform(
        lambda value: value.expanding().median()
    )
    long = eligible & volume_gate & (bars["close"] > high)
    short = eligible & volume_gate & (bars["close"] < low)
    first = _first_true_per_session(long | short, bars)
    return (long.astype(int) - short.astype(int)).where(first, 0)


def vwap_reversion_signal(bars: pd.DataFrame) -> pd.Series:
    deviation = bars["close"] / bars["session_vwap"] - 1
    eligible = bars["session_bar"].between(11, 47)
    slowing = bars["bar_return"].abs() < bars["bar_return"].abs().shift(1)
    long = eligible & slowing & (deviation < -0.0075)
    short = eligible & slowing & (deviation > 0.0075)
    first = _first_true_per_session(long | short, bars)
    return (long.astype(int) - short.astype(int)).where(first, 0)


def gap_continuation_signal(bars: pd.DataFrame) -> pd.Series:
    sessions = list(dict.fromkeys(bars["session"]))
    previous_close = {}
    for prior, current in zip(sessions, sessions[1:]):
        previous_close[current] = float(
            bars.loc[bars["session"] == prior, "close"].iloc[-1]
        )
    prior = bars["session"].map(previous_close)
    day_open = bars.groupby("session")["open"].transform("first")
    gap = day_open / prior - 1
    checkpoint = bars["session_bar"] == 5
    long = checkpoint & (gap > 0.005) & (bars["close"] > bars["session_vwap"])
    short = checkpoint & (gap < -0.005) & (bars["close"] < bars["session_vwap"])
    return long.astype(int) - short.astype(int)


def compression_breakout_signal(bars: pd.DataFrame) -> pd.Series:
    opening = bars["session_bar"] <= 5
    high = bars["high"].where(opening).groupby(bars["session"]).transform("max")
    low = bars["low"].where(opening).groupby(bars["session"]).transform("min")
    day_open = bars.groupby("session")["open"].transform("first")
    compressed = (high - low) / day_open < 0.004
    eligible = bars["session_bar"].between(6, 23) & compressed
    long = eligible & (bars["close"] > high)
    short = eligible & (bars["close"] < low)
    first = _first_true_per_session(long | short, bars)
    return (long.astype(int) - short.astype(int)).where(first, 0)


STRATEGIES = {
    "opening_range_continuation": (opening_range_signal, 12),
    "vwap_mean_reversion": (vwap_reversion_signal, 12),
    "gap_continuation": (gap_continuation_signal, 12),
    "compression_breakout": (compression_breakout_signal, 18),
}


def main() -> None:
    frames = load_or_fetch(start=START, end=END)
    results = {}
    decisions = {}
    for strategy, (builder, hold) in STRATEGIES.items():
        rows = []
        passes = []
        for symbol in SYMBOLS:
            bars = frames[symbol]
            signal = builder(bars)
            for cost_name, cost in (
                ("primary_2bp_per_leg", PRIMARY_COST_BPS_PER_LEG),
                ("stress_5bp_per_leg", STRESS_COST_BPS_PER_LEG),
            ):
                trades = simulate_fixed_horizon(
                    bars, signal, hold_bars=hold, cost_bps_per_leg=cost
                )
                for window, (start, end) in WINDOWS.items():
                    selected = trades[
                        trades["session"].between(start, end)
                    ] if len(trades) else trades
                    summary = trade_summary(selected)
                    summary.update(
                        strategy=strategy, symbol=symbol, cost=cost_name,
                        window=window,
                    )
                    rows.append(summary)
                    if cost == STRESS_COST_BPS_PER_LEG:
                        passes.append(
                            summary["trades"] >= 30
                            and summary["mean_return"] > 0
                            and (summary["profit_factor"] or 0) > 1
                        )
        passed = len(passes) == len(SYMBOLS) * len(WINDOWS) and all(passes)
        decisions[strategy] = "advance_to_option_translation" if passed else "reject"
        results[strategy] = rows

    advanced = [name for name, decision in decisions.items()
                if decision == "advance_to_option_translation"]
    payload = {
        "decision": "advance_qualifiers" if advanced else "no_family_qualified",
        "advanced_families": advanced,
        "family_decisions": decisions,
        "pre_registration": {
            "variants": list(STRATEGIES),
            "no_parameter_grid": True,
            "gate": "Each ETF in both windows: >=30 trades, positive mean return, and profit factor >1 at 5 bp per leg.",
            "final_validation": "2026-08-04 onward remains frozen and unused.",
        },
        "results": results,
        "limitations": [
            "IEX five-minute bars are not consolidated SIP data.",
            "Two short windows do not include 2020 or 2022 stress regimes.",
            "Fixed-horizon exits do not model intrabar stops or targets.",
        ],
    }
    out = Path("reports/intraday_strategy_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(json.dumps({"decision": payload["decision"], "families": decisions}, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
