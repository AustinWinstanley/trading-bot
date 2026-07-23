"""Cost and stability test for replacing the equity core with overnight exposure.

The frontier study found that close-to-open SPY/QQQ returns had attractive
standalone and marginal Sharpe, but charged no execution cost. This study asks
the investable question:

* Does the effect survive two executions per session?
* Is it present both before and after 2013?
* Does replacing some or all of the production SPY core improve the portfolio
  in both the 2020-2022 and held-out 2023+ windows?

Official adjusted close and next-session adjusted open are optimistic
execution benchmarks. Fixed per-leg costs stress the result rather than
pretending those prints are freely attainable.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    SHORT_BORROW,
    build_streams,
    norm_index,
    returns_summary,
)
from engine.tiingo import load_parquet

TD = 252
COSTS_BPS_PER_LEG = (0.0, 0.5, 1.0, 2.0, 5.0)
DEEP_DIR = Path("state/history_deep")


def overnight_returns(frame: pd.DataFrame, cost_bps_per_leg: float = 0.0) -> pd.Series:
    """Close D-1 to open D, paying one entry and one exit cost each session."""
    open_px = norm_index(frame["open"])
    close_px = norm_index(frame["close"])
    gross = open_px / close_px.shift(1) - 1
    round_trip_cost = 2 * cost_bps_per_leg / 10_000
    return (gross - round_trip_cost).dropna()


def break_even_cost_bps_per_leg(gross: pd.Series) -> float:
    """Arithmetic cost per execution that reduces mean return to zero."""
    return float(gross.dropna().mean() * 10_000 / 2)


def stream_summary(r: pd.Series, label: str) -> dict:
    row = returns_summary(r, label)
    row["trades_per_year"] = 2 * TD
    return row


def windows(r: pd.Series) -> dict[str, pd.Series]:
    return {
        "full": r,
        "early_pre_2013": r[r.index < "2013-01-01"],
        "heldout_2013_plus": r[r.index >= "2013-01-01"],
    }


def production_variants(cost_bps_per_leg: float) -> pd.DataFrame:
    streams = build_streams()
    frames = load_parquet(["SPY", "QQQ"], DEEP_DIR)
    spy_on = overnight_returns(frames["SPY"], cost_bps_per_leg)
    qqq_on = overnight_returns(frames["QQQ"], cost_bps_per_leg)

    aligned = streams.join(
        pd.DataFrame({"spy_overnight": spy_on, "qqq_overnight": qqq_on}),
        how="inner",
    )
    common = (
        0.25 * aligned["tsmom"]
        + 0.20 * aligned["trend"]
        + 0.30 * aligned["mom_ls"]
    )
    variants = pd.DataFrame(
        {
            "deployed full-session SPY core": common + 0.40 * aligned["spy"],
            "40% SPY overnight core": common + 0.40 * aligned["spy_overnight"],
            "40% QQQ overnight core": common + 0.40 * aligned["qqq_overnight"],
            "20% SPY full + 20% QQQ overnight": (
                common + 0.20 * aligned["spy"] + 0.20 * aligned["qqq_overnight"]
            ),
        }
    )
    return variants - (0.15 * SHORT_BORROW / TD)


def main() -> None:
    frames = load_parquet(["SPY", "QQQ"], DEEP_DIR)
    missing = {"SPY", "QQQ"} - set(frames)
    if missing:
        raise SystemExit(
            f"missing deep OHLC history for {sorted(missing)} in {DEEP_DIR}"
        )

    standalone: dict[str, list[dict]] = {}
    break_even: dict[str, dict[str, float]] = {}
    for symbol, frame in frames.items():
        gross = overnight_returns(frame)
        break_even[symbol] = {
            name: round(break_even_cost_bps_per_leg(sample), 3)
            for name, sample in windows(gross).items()
        }
        rows = []
        for cost in COSTS_BPS_PER_LEG:
            net = overnight_returns(frame, cost)
            for window_name, sample in windows(net).items():
                rows.append(
                    stream_summary(
                        sample,
                        f"{symbol} overnight {cost:g}bp/leg {window_name}",
                    )
                    | {"cost_bps_per_leg": cost, "window": window_name}
                )
        standalone[symbol] = rows

    portfolio: dict[str, list[dict]] = {}
    for cost in (1.0, 2.0):
        variants = production_variants(cost)
        result_rows = []
        for window_name, sample in {
            "full": variants,
            "early_2020_2022": variants.loc[:"2022-12-31"],
            "heldout_2023_plus": variants.loc["2023-01-01":],
        }.items():
            for column in sample:
                result_rows.append(
                    returns_summary(
                        sample[column],
                        f"{column} ({cost:g}bp/leg, {window_name})",
                    )
                    | {"cost_bps_per_leg": cost, "window": window_name}
                )
        portfolio[f"{cost:g}bp_per_leg"] = result_rows

    payload = {
        "conclusion": (
            "Reject an overnight equity-core sleeve. SPY is erased near 2bp "
            "per leg; QQQ survives standalone but every tested production "
            "replacement lowers full-sample and held-out Sharpe."
        ),
        "method": (
            "Adjusted close D-1 to adjusted open D; 504 executions/year. "
            "Official prints are optimistic, so costs are charged per leg."
        ),
        "break_even_cost_bps_per_leg": break_even,
        "standalone": standalone,
        "production_portfolio": portfolio,
    }
    out = Path("reports/overnight_cost_study.json")
    out.write_text(json.dumps(payload, indent=2))

    for symbol, rows in standalone.items():
        print(f"\n{symbol} OVERNIGHT")
        print(
            pd.DataFrame(rows)[
                ["cost_bps_per_leg", "window", "cagr", "sharpe", "max_dd"]
            ].to_string(index=False)
        )
        print("break-even bp/leg:", break_even[symbol])

    for cost, rows in portfolio.items():
        print(f"\nPRODUCTION PORTFOLIO — {cost}")
        print(
            pd.DataFrame(rows)[
                ["portfolio", "window", "cagr", "sharpe", "max_dd"]
            ].to_string(index=False)
        )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
