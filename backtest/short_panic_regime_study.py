"""Pre-registered panic-rebound protection for the momentum short leg.

Momentum crashes are concentrated in rebounds following severe, volatile
market declines.  The sole promotable candidate here flattens only MOM_LS
shorts when all three lagged SPY conditions hold:

* at least 15% below the trailing 252-session high;
* 20-session annualized volatility of at least 25%;
* price above its 20-session average (a simple rebound confirmation).

Thresholds are fixed before observing this study's output. No alternate
threshold is eligible for promotion.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.momentum_buffer_study import BufferResult, build_buffer_stream
from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.short_capacity_study import MOM_ACCOUNT_MULTIPLIER, STARTING_EQUITY
from backtest.xsec_data import load


def panic_rebound_state(spy_returns: pd.Series) -> pd.Series:
    """Return a close-known state whose resulting trade earns next-day return."""
    synthetic_price = (1 + spy_returns.fillna(0)).cumprod()
    drawdown = synthetic_price / synthetic_price.rolling(252, min_periods=126).max() - 1
    volatility = spy_returns.rolling(20, min_periods=20).std() * np.sqrt(TD)
    rebound = synthetic_price > synthetic_price.rolling(20, min_periods=20).mean()
    return ((drawdown <= -0.15) & (volatility >= 0.25) & rebound).fillna(False)


def apply_short_gate(
    result: BufferResult,
    spy_returns: pd.Series,
    *,
    enabled: bool,
    cost_bps: float = 15.0,
) -> BufferResult:
    state = (
        panic_rebound_state(spy_returns)
        .reindex(result.weights.index)
        .fillna(False)
        .astype(bool)
    )
    long_weights = result.weights.clip(lower=0)
    short_weights = result.weights.clip(upper=0)
    if enabled:
        short_weights = short_weights.mask(state, 0.0)
    weights = long_weights + short_weights
    # Recover asset returns from the already-computed leg P&L is impossible;
    # callers attach them through the private attribute set by the builder below.
    asset_returns = result.asset_returns
    long_turnover = long_weights.diff().abs().sum(axis=1)
    short_turnover = short_weights.diff().abs().sum(axis=1)
    long_returns = (
        (long_weights.shift(1) * asset_returns).sum(axis=1)
        - long_turnover * cost_bps / 10_000
    )
    short_returns = (
        (short_weights.shift(1) * asset_returns).sum(axis=1)
        - short_turnover * cost_bps / 10_000
    )
    gated = BufferResult(
        long_returns + short_returns,
        -short_weights.sum(axis=1).shift(1).fillna(0),
        weights,
        result.rebalances,
        long_returns,
        short_returns,
        long_turnover,
        short_turnover,
    )
    gated.panic_state = state
    gated.asset_returns = asset_returns
    return gated


def build_result(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    spy_returns: pd.Series,
    *,
    equity: pd.Series,
    profile: str,
    enabled: bool,
) -> BufferResult:
    result = build_buffer_stream(
        close,
        volume,
        account_equity=equity,
        account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
        hold_n=20,
    )
    result.asset_returns = close.pct_change(fill_method=None)
    return apply_short_gate(result, spy_returns, enabled=enabled)


def portfolio_returns(
    common: pd.Series,
    result: BufferResult,
    *,
    profile: str,
) -> pd.Series:
    multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
    aligned = pd.concat(
        {
            "common": common * (2 if profile == "2x" else 1),
            "momentum": result.returns * multiplier,
            "short_gross": result.short_gross * multiplier,
        },
        axis=1,
        sort=False,
    ).dropna()
    return (
        aligned["common"]
        + aligned["momentum"]
        - aligned["short_gross"] * SHORT_BORROW / TD
        - (MARGIN_RATE / TD if profile == "2x" else 0)
    )


def solve(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    spy: pd.Series,
    common: pd.Series,
    *,
    profile: str,
    enabled: bool,
) -> tuple[pd.Series, BufferResult]:
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result = None
    portfolio = pd.Series(dtype=float)
    for _ in range(3):
        result = build_result(
            close, volume, spy, equity=equity, profile=profile, enabled=enabled
        )
        portfolio = portfolio_returns(common, result, profile=profile)
        equity = (
            STARTING_EQUITY
            * (1 + portfolio).cumprod().reindex(close.index).ffill().fillna(1)
        )
    assert result is not None
    return portfolio, result


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]
    production = build_streams()
    spy = production["spy"]
    common = 0.40 * spy + 0.25 * production["tsmom"] + 0.20 * production["trend"]

    portfolios = {}
    results = {}
    for profile in ("base", "2x"):
        for label, enabled in {
            "control": False,
            "preselected panic-rebound short gate": True,
        }.items():
            print(f"Running {profile}: {label}...", flush=True)
            portfolios[(profile, label)], results[(profile, label)] = solve(
                close, volume, spy, common, profile=profile, enabled=enabled
            )

    windows = {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }
    performance = {}
    attribution = {}
    for window, slicer in windows.items():
        performance[window] = [
            returns_summary(series.loc[slicer], f"{profile} — {label}")
            for (profile, label), series in portfolios.items()
        ]
        attribution[window] = []
        for (profile, label), result in results.items():
            multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
            short = result.short_returns.loc[slicer] * multiplier
            attribution[window].append(
                {
                    "profile": profile,
                    "variant": label,
                    "short_ann_return_before_borrow": round(
                        float(short.mean() * TD), 4
                    ),
                    "short_turnover": round(
                        float(result.short_turnover.loc[slicer].sum()), 2
                    ),
                    "panic_days": int(result.panic_state.loc[slicer].sum()),
                    "average_short_gross": round(
                        float(result.short_gross.loc[slicer].mean()) * multiplier, 4
                    ),
                }
            )

    gates = []
    for profile in ("base", "2x"):
        passed = True
        detail = {}
        for window in ("early_2020_2022", "heldout_2023_plus"):
            rows = {
                row["portfolio"].split(" — ", 1)[1]: row
                for row in performance[window]
                if row["portfolio"].startswith(profile + " — ")
            }
            control = rows["control"]
            candidate = rows["preselected panic-rebound short gate"]
            checks = {
                "sharpe_not_lower": candidate["sharpe"] >= control["sharpe"],
                "cagr_not_lower": candidate["cagr"] >= control["cagr"],
                "max_drawdown_not_worse": abs(candidate["max_dd"])
                <= abs(control["max_dd"]),
            }
            detail[window] = checks
            passed = passed and all(checks.values())
        gates.append({"profile": profile, "passed": passed, "windows": detail})
    promoted = all(row["passed"] for row in gates)
    payload = {
        "pre_registration": {
            "candidate": (
                "Flatten MOM_LS shorts when SPY drawdown <= -15%, 20-day "
                "annualized volatility >= 25%, and SPY is above its 20-day average."
            ),
            "promotion_rule": (
                "CAGR and Sharpe must not decline and max drawdown must not worsen "
                "in both profiles and both early and held-out windows."
            ),
        },
        "decision": "promote" if promoted else "reject",
        "gates": gates,
        "performance": performance,
        "short_leg_attribution": attribution,
        "limitations": [
            "The current-listing stock universe is survivorship-biased.",
            "Historical borrow availability is unavailable.",
            "The fixed regime thresholds are economically motivated but not independently calibrated.",
            "Daily gate changes assume close-known signals traded for next-session exposure.",
        ],
        "research_basis": {
            "paper": "Momentum Crashes",
            "url": "https://www.nber.org/papers/w20439",
        },
    }
    out = Path("reports/short_panic_regime_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nDecision: {payload['decision'].upper()}")
    for gate in gates:
        print(f"{gate['profile']}: {'PASS' if gate['passed'] else 'FAIL'}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
