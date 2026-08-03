"""Whole-share short capacity at the two $10,000 paper-account sizes.

Alpaca accepts fractional long notional orders but short orders must be whole
shares.  The production backtest historically assumed fractional shorts, even
though a 15% short sleeve spread over 20 names is only $75 per name in the base
account ($150 in the 2x account).  This study measures that execution gap and
tests two simple, pre-specified alternatives:

* price-aware 20: continue down the loser ranking until 20 affordable names
  have been found;
* bottom 10: concentrate the sleeve in the 10 worst-ranked names.

Historical borrowability is unavailable.  All variants therefore remain an
optimistic execution bound even though production checks easy-to-borrow status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.xsec_data import load

TD = 252
STARTING_EQUITY = 10_000.0
MOM_ACCOUNT_MULTIPLIER = {"base": 0.30, "2x": 0.60}


@dataclass
class CapacityResult:
    returns: pd.Series
    short_gross: pd.Series
    rebalances: pd.DataFrame
    weights: pd.DataFrame


def build_capacity_stream(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    account_equity: pd.Series | float = STARTING_EQUITY,
    account_multiplier: float = 0.30,
    short_n: int = 20,
    selection: str = "ranked",
    lookback: int = 252,
    skip: int = 21,
    long_n: int = 20,
    rebalance: int = 5,
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
    cost_bps: float = 15.0,
) -> CapacityResult:
    """Build a normalized 50%-long/50%-short MOM_LS return stream.

    ``account_multiplier`` maps the normalized stream into account exposure:
    0.30 for the base profile and 0.60 for 2x.  Whole-share capacity is
    evaluated against the prior account equity at each weekly rebalance.
    """
    if selection not in {"fractional", "ranked", "price_aware"}:
        raise ValueError(f"unknown selection mode {selection!r}")
    if long_n < 1 or short_n < 1:
        raise ValueError("long_n and short_n must be positive")

    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    momentum = close.shift(skip) / close.shift(lookback) - 1.0
    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )
    daily_returns = close.pct_change()
    rebalance_days = close.index[lookback + skip :: rebalance]
    if np.isscalar(account_equity):
        equity = pd.Series(float(account_equity), index=close.index)
    else:
        equity = account_equity.reindex(close.index).ffill().bfill()

    rows: list[pd.Series] = []
    logs: list[dict] = []
    for date in rebalance_days:
        ranked = momentum.loc[date].where(eligible.loc[date]).dropna()
        ranked = ranked.sort_values(ascending=False)
        if len(ranked) < long_n + short_n:
            continue

        weight = pd.Series(0.0, index=close.columns, dtype="float32")
        longs = list(ranked.head(long_n).index)
        weight.loc[longs] = 0.5 / long_n

        desired_weight = account_multiplier * 0.5 / short_n
        desired_dollars = float(equity.loc[date]) * desired_weight
        descending_losers = list(ranked.index[::-1])
        if selection == "price_aware":
            shorts = [
                symbol
                for symbol in descending_losers
                if float(close.at[date, symbol]) <= desired_dollars
            ][:short_n]
        else:
            shorts = descending_losers[:short_n]

        realized_dollars = []
        zero_targets = 0
        for symbol in shorts:
            price = float(close.at[date, symbol])
            if selection == "fractional":
                capacity = 1.0
                dollars = desired_dollars
            else:
                shares = np.floor(desired_dollars / price)
                dollars = float(shares * price)
                capacity = dollars / desired_dollars if desired_dollars else 0.0
            weight.loc[symbol] = -(0.5 / short_n) * capacity
            realized_dollars.append(dollars)
            zero_targets += int(dollars == 0)

        rows.append(weight.rename(date))
        logs.append(
            {
                "date": date,
                "account_equity": float(equity.loc[date]),
                "desired_dollars_per_short": desired_dollars,
                "selected_shorts": len(shorts),
                "zero_share_targets": zero_targets,
                "realized_short_gross": float(-weight.clip(upper=0).sum()),
                "realized_short_dollars": float(sum(realized_dollars)),
            }
        )

    if not rows:
        empty = pd.Series(0.0, index=close.index)
        return CapacityResult(
            empty,
            empty,
            pd.DataFrame(logs),
            pd.DataFrame(0.0, index=close.index, columns=close.columns),
        )

    rebalance_weights = pd.DataFrame(rows)
    weights = rebalance_weights.reindex(close.index).ffill().fillna(0.0)
    gross = (weights.shift(1) * daily_returns).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    returns = gross - turnover * cost_bps / 10_000.0
    short_gross = -weights.clip(upper=0).sum(axis=1).shift(1).fillna(0.0)
    return CapacityResult(
        returns,
        short_gross,
        pd.DataFrame(logs),
        weights,
    )


def profile_returns(
    common: pd.Series,
    result: CapacityResult,
    *,
    profile: str,
) -> pd.Series:
    multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
    aligned = pd.concat(
        {
            "common": common * (2.0 if profile == "2x" else 1.0),
            "momentum": result.returns * multiplier,
            "short_gross": result.short_gross * multiplier,
        },
        axis=1,
        sort=False,
    ).dropna()
    financing = MARGIN_RATE / TD if profile == "2x" else 0.0
    return (
        aligned["common"]
        + aligned["momentum"]
        - aligned["short_gross"] * SHORT_BORROW / TD
        - financing
    )


def solve_dynamic_equity(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    common: pd.Series,
    *,
    profile: str,
    selection: str,
    short_n: int,
    iterations: int = 3,
) -> tuple[pd.Series, CapacityResult]:
    """Iterate capacity against the equity curve generated by that capacity."""
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result = None
    profile_r = pd.Series(dtype=float)
    for _ in range(iterations):
        result = build_capacity_stream(
            close,
            volume,
            account_equity=equity,
            account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
            selection=selection,
            short_n=short_n,
        )
        profile_r = profile_returns(common, result, profile=profile)
        curve = STARTING_EQUITY * (1 + profile_r).cumprod()
        equity = curve.reindex(close.index).ffill().fillna(STARTING_EQUITY)
    assert result is not None
    return profile_r, result


def capacity_summary(
    result: CapacityResult,
    *,
    profile: str,
    label: str,
) -> dict:
    logs = result.rebalances
    multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
    target_gross = multiplier * 0.5
    target_count = int(logs["selected_shorts"].sum()) if len(logs) else 0
    zeros = int(logs["zero_share_targets"].sum()) if len(logs) else 0
    return {
        "variant": label,
        "profile": profile,
        "rebalances": len(logs),
        "target_short_gross": round(target_gross, 4),
        "average_realized_short_gross": round(
            float(logs["realized_short_gross"].mean()) * multiplier, 4
        ) if len(logs) else 0.0,
        "average_capacity_pct": round(
            float(logs["realized_short_gross"].mean()) / 0.5 * 100, 1
        ) if len(logs) else 0.0,
        "zero_share_target_pct": round(zeros / target_count * 100, 1)
        if target_count else 0.0,
        "average_selected_shorts": round(
            float(logs["selected_shorts"].mean()), 1
        ) if len(logs) else 0.0,
    }


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]

    production = build_streams()
    common = (
        0.40 * production["spy"]
        + 0.25 * production["tsmom"]
        + 0.20 * production["trend"]
    )
    variants = {
        "fractional bottom 20": ("fractional", 20),
        "current whole-share bottom 20": ("ranked", 20),
        "price-aware whole-share bottom 20": ("price_aware", 20),
        "whole-share bottom 10": ("ranked", 10),
    }

    returns: dict[str, pd.Series] = {}
    capacity: list[dict] = []
    for profile in ("base", "2x"):
        for label, (selection, short_n) in variants.items():
            print(f"Running {profile}: {label}...")
            profile_r, result = solve_dynamic_equity(
                close,
                volume,
                common,
                profile=profile,
                selection=selection,
                short_n=short_n,
            )
            key = f"{profile} — {label}"
            returns[key] = profile_r
            capacity.append(capacity_summary(result, profile=profile, label=label))

    windows = {}
    for window, slicer in {
        "full": slice(None),
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
    }.items():
        windows[window] = [
            returns_summary(series.loc[slicer], label)
            for label, series in returns.items()
        ]

    payload = {
        "conclusion": (
            "Keep the current bottom-20 construction while paper data "
            "accumulates. Whole-share constraints materially reduce short "
            "exposure at $10,000, especially in the base account, but the "
            "portfolio-level regression is modest and neither price-aware "
            "selection nor bottom-10 concentration dominates in both the "
            "early and held-out windows."
        ),
        "limitations": [
            "Historical easy-to-borrow and locate availability is unavailable.",
            "The universe of currently listed companies is survivorship-biased.",
            "Price-aware selection changes the signal by excluding expensive losers.",
            "The simulation rebalances weekly and does not model daily drift-band trades.",
        ],
        "starting_equity": STARTING_EQUITY,
        "capacity": capacity,
        "production_portfolio": windows,
    }
    out = Path("reports/short_capacity_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print("\nCAPACITY")
    print(pd.DataFrame(capacity).to_string(index=False))
    for window, rows in windows.items():
        print(f"\nPRODUCTION PORTFOLIO — {window}")
        print(
            pd.DataFrame(rows)[
                ["portfolio", "cagr", "sharpe", "max_dd"]
            ].to_string(index=False)
        )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
