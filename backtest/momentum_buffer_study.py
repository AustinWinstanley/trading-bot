"""Pre-registered momentum rank-buffer study.

The production MOM_LS sleeve rebuilds its top/bottom 20 every week.  The only
promotable candidate in this study enters the top/bottom 20 and retains an
incumbent until it leaves the top/bottom 40.  That hysteresis is intended to
reduce trading without changing the underlying 12-1 momentum signal.

The bottom-60 buffer is reported only as a sensitivity check and cannot be
selected from this experiment.  Historical borrowability is unavailable and
the current-company universe is survivorship-biased.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.short_capacity_study import (
    MOM_ACCOUNT_MULTIPLIER,
    STARTING_EQUITY,
    profile_returns,
)
from backtest.xsec_data import load


@dataclass
class BufferResult:
    returns: pd.Series
    short_gross: pd.Series
    weights: pd.DataFrame
    rebalances: pd.DataFrame
    long_returns: pd.Series
    short_returns: pd.Series
    long_turnover: pd.Series
    short_turnover: pd.Series


def buffered_members(
    ranked: pd.Series,
    previous: list[str],
    *,
    side: str,
    enter_n: int,
    hold_n: int,
) -> list[str]:
    """Retain qualifying incumbents, then fill from the strongest entry ranks."""
    if side not in {"long", "short"}:
        raise ValueError(f"unknown side {side!r}")
    if hold_n < enter_n:
        raise ValueError("hold_n must be at least enter_n")
    ordered = list(ranked.index if side == "long" else ranked.index[::-1])
    hold = set(ordered[:hold_n])
    retained = [symbol for symbol in previous if symbol in hold]
    return retained + [
        symbol for symbol in ordered if symbol not in retained
    ][: enter_n - len(retained)]


def build_buffer_stream(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    account_equity: pd.Series | float = STARTING_EQUITY,
    account_multiplier: float = 0.30,
    enter_n: int = 20,
    hold_n: int = 40,
    lookback: int = 252,
    skip: int = 21,
    rebalance: int = 5,
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
    cost_bps: float = 15.0,
) -> BufferResult:
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    momentum = close.shift(skip) / close.shift(lookback) - 1.0
    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )
    daily_returns = close.pct_change(fill_method=None)
    rebalance_days = close.index[lookback + skip :: rebalance]
    equity = (
        pd.Series(float(account_equity), index=close.index)
        if np.isscalar(account_equity)
        else account_equity.reindex(close.index).ffill().bfill()
    )

    rows: list[pd.Series] = []
    logs: list[dict] = []
    longs: list[str] = []
    shorts: list[str] = []
    for date in rebalance_days:
        ranked = momentum.loc[date].where(eligible.loc[date]).dropna()
        ranked = ranked.sort_values(ascending=False)
        if len(ranked) < 2 * hold_n:
            continue
        old_longs, old_shorts = longs, shorts
        longs = buffered_members(
            ranked, old_longs, side="long", enter_n=enter_n, hold_n=hold_n
        )
        shorts = buffered_members(
            ranked, old_shorts, side="short", enter_n=enter_n, hold_n=hold_n
        )

        weight = pd.Series(0.0, index=close.columns, dtype="float32")
        weight.loc[longs] = 0.5 / enter_n
        desired_weight = account_multiplier * 0.5 / enter_n
        desired_dollars = float(equity.loc[date]) * desired_weight
        zero_targets = 0
        for symbol in shorts:
            price = float(close.at[date, symbol])
            shares = np.floor(desired_dollars / price)
            capacity = float(shares * price / desired_dollars) if desired_dollars else 0
            weight.loc[symbol] = -(0.5 / enter_n) * capacity
            zero_targets += int(shares == 0)
        rows.append(weight.rename(date))
        logs.append(
            {
                "date": date,
                "long_replacements": len(set(longs) - set(old_longs)),
                "short_replacements": len(set(shorts) - set(old_shorts)),
                "zero_share_targets": zero_targets,
                "realized_short_gross": float(-weight.clip(upper=0).sum()),
            }
        )

    if not rows:
        weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    else:
        weights = pd.DataFrame(rows).reindex(close.index).ffill().fillna(0.0)
    long_weights = weights.clip(lower=0)
    short_weights = weights.clip(upper=0)
    long_turnover = long_weights.diff().abs().sum(axis=1)
    short_turnover = short_weights.diff().abs().sum(axis=1)
    long_returns = (
        (long_weights.shift(1) * daily_returns).sum(axis=1)
        - long_turnover * cost_bps / 10_000
    )
    short_returns = (
        (short_weights.shift(1) * daily_returns).sum(axis=1)
        - short_turnover * cost_bps / 10_000
    )
    short_gross = -short_weights.sum(axis=1).shift(1).fillna(0.0)
    return BufferResult(
        long_returns + short_returns,
        short_gross,
        weights,
        pd.DataFrame(logs),
        long_returns,
        short_returns,
        long_turnover,
        short_turnover,
    )


def solve_dynamic_equity(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    common: pd.Series,
    *,
    profile: str,
    hold_n: int,
    iterations: int = 3,
) -> tuple[pd.Series, BufferResult]:
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result = None
    portfolio = pd.Series(dtype=float)
    for _ in range(iterations):
        result = build_buffer_stream(
            close,
            volume,
            account_equity=equity,
            account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
            hold_n=hold_n,
        )
        portfolio = profile_returns(common, result, profile=profile)
        equity = (
            STARTING_EQUITY
            * (1 + portfolio).cumprod().reindex(close.index).ffill().fillna(1)
        )
    assert result is not None
    return portfolio, result


def _metrics(series: pd.Series, label: str) -> dict:
    if series.dropna().empty:
        return {"portfolio": label}
    return returns_summary(series, label)


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
        "weekly rebuild 20": 20,
        "preselected 20/40 buffer": 40,
        "sensitivity 20/60 buffer": 60,
    }
    portfolios: dict[tuple[str, str], pd.Series] = {}
    results: dict[tuple[str, str], BufferResult] = {}
    for profile in ("base", "2x"):
        for label, hold_n in variants.items():
            print(f"Running {profile}: {label}...", flush=True)
            portfolios[(profile, label)], results[(profile, label)] = (
                solve_dynamic_equity(
                    close, volume, common, profile=profile, hold_n=hold_n
                )
            )

    windows = {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }
    performance: dict[str, list[dict]] = {}
    attribution: dict[str, list[dict]] = {}
    for window, slicer in windows.items():
        performance[window] = []
        attribution[window] = []
        for key, portfolio in portfolios.items():
            profile, label = key
            result = results[key]
            performance[window].append(
                _metrics(portfolio.loc[slicer], f"{profile} — {label}")
            )
            multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
            attribution[window].append(
                {
                    "profile": profile,
                    "variant": label,
                    "long": _metrics(
                        result.long_returns.loc[slicer] * multiplier, "long"
                    ),
                    "short_before_borrow": _metrics(
                        result.short_returns.loc[slicer] * multiplier, "short"
                    ),
                    "long_turnover": round(
                        float(result.long_turnover.loc[slicer].sum()), 2
                    ),
                    "short_turnover": round(
                        float(result.short_turnover.loc[slicer].sum()), 2
                    ),
                    "average_short_gross": round(
                        float(result.short_gross.loc[slicer].mean()) * multiplier, 4
                    ),
                }
            )

    gates: list[dict] = []
    for profile in ("base", "2x"):
        passed = True
        details = {}
        for window in ("early_2020_2022", "heldout_2023_plus"):
            rows = {
                row["portfolio"].split(" — ", 1)[1]: row
                for row in performance[window]
                if row["portfolio"].startswith(profile + " — ")
            }
            attrs = {
                row["variant"]: row
                for row in attribution[window]
                if row["profile"] == profile
            }
            control = rows["weekly rebuild 20"]
            candidate = rows["preselected 20/40 buffer"]
            control_turnover = (
                attrs["weekly rebuild 20"]["long_turnover"]
                + attrs["weekly rebuild 20"]["short_turnover"]
            )
            candidate_turnover = (
                attrs["preselected 20/40 buffer"]["long_turnover"]
                + attrs["preselected 20/40 buffer"]["short_turnover"]
            )
            checks = {
                "sharpe_improves": candidate["sharpe"] > control["sharpe"],
                "turnover_reduction_at_least_30pct": (
                    candidate_turnover <= 0.70 * control_turnover
                ),
                "cagr_retains_at_least_90pct": (
                    candidate["cagr"] >= 0.90 * control["cagr"]
                ),
                "max_drawdown_not_over_10pct_worse": (
                    abs(candidate["max_dd"]) <= 1.10 * abs(control["max_dd"])
                ),
            }
            details[window] = checks
            passed = passed and all(checks.values())
        gates.append({"profile": profile, "passed": passed, "windows": details})

    promoted = all(gate["passed"] for gate in gates)
    payload = {
        "pre_registration": {
            "candidate": "enter top/bottom 20; retain until outside top/bottom 40",
            "promotion_rule": (
                "For both account profiles and both early and held-out windows: "
                "higher net Sharpe, at least 30% lower sleeve turnover, at least "
                "90% of control CAGR, and no more than 10% worse max drawdown."
            ),
            "non_promotable_sensitivity": "20/60 buffer",
        },
        "decision": "promote" if promoted else "reject",
        "gates": gates,
        "performance": performance,
        "sleeve_attribution": attribution,
        "limitations": [
            "The operating-company universe contains current listings and is survivorship-biased.",
            "Historical easy-to-borrow and locate availability is unavailable.",
            "Costs use a fixed 15 bps per unit of one-way turnover plus the portfolio borrow assumption.",
            "Whole-share short capacity is modeled from a dynamic $10,000 starting account.",
        ],
    }
    out = Path("reports/momentum_buffer_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nDecision: {payload['decision'].upper()}")
    for gate in gates:
        print(f"{gate['profile']}: {'PASS' if gate['passed'] else 'FAIL'}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
