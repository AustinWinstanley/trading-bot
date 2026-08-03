"""Pre-registered differentiated account-mandate study.

Control: both accounts use the same underlying mix and the second account
simply doubles it.

Candidate: retain the current base mix, but give the 2x account a more
risk-balanced underlying mandate before leverage:
30% SPY, 35% asset TSMOM, 25% SPY trend, and 10% long + 10% short momentum.

The household comparison assumes 20% of capital in base and 80% in 2x.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.promotion import passes_gate
from backtest.short_capacity_study import STARTING_EQUITY, build_capacity_stream
from backtest.xsec_data import load


def solve_profile(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    streams: pd.DataFrame,
    *,
    profile: str,
    weights: dict[str, float],
) -> pd.Series:
    leverage = 2.0 if profile == "2x" else 1.0
    momentum_multiplier = leverage * weights["mom_ls"]
    common = leverage * (
        weights["spy"] * streams["spy"]
        + weights["tsmom"] * streams["tsmom"]
        + weights["trend"] * streams["trend"]
    )
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    portfolio = pd.Series(dtype=float)
    for _ in range(3):
        result = build_capacity_stream(
            close,
            volume,
            account_equity=equity,
            account_multiplier=momentum_multiplier,
            selection="ranked",
        )
        aligned = pd.concat(
            {
                "common": common,
                "momentum": result.returns * momentum_multiplier,
                "short_gross": result.short_gross * momentum_multiplier,
            },
            axis=1,
            sort=False,
        ).dropna()
        portfolio = (
            aligned["common"]
            + aligned["momentum"]
            - aligned["short_gross"] * SHORT_BORROW / TD
            - (MARGIN_RATE / TD if profile == "2x" else 0)
        )
        equity = (
            STARTING_EQUITY
            * (1 + portfolio).cumprod().reindex(close.index).ffill().fillna(1)
        )
    return portfolio


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]
    streams = build_streams()

    current = {"spy": 0.40, "tsmom": 0.25, "trend": 0.20, "mom_ls": 0.30}
    defensive_2x = {"spy": 0.30, "tsmom": 0.35, "trend": 0.25, "mom_ls": 0.20}
    print("Running base control...", flush=True)
    base = solve_profile(
        close, volume, streams, profile="base", weights=current
    )
    print("Running 2x control...", flush=True)
    control_2x = solve_profile(
        close, volume, streams, profile="2x", weights=current
    )
    print("Running differentiated 2x...", flush=True)
    candidate_2x = solve_profile(
        close, volume, streams, profile="2x", weights=defensive_2x
    )
    household_control = 0.20 * base + 0.80 * control_2x
    household_candidate = 0.20 * base + 0.80 * candidate_2x

    series = {
        "base unchanged": base,
        "2x same mandate control": control_2x,
        "2x differentiated defensive underlying": candidate_2x,
        "household same mandate control": household_control,
        "household differentiated mandates": household_candidate,
    }
    windows = {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }
    performance = {
        window: [
            returns_summary(value.loc[slicer], label)
            for label, value in series.items()
        ]
        for window, slicer in windows.items()
    }
    gates = {}
    passed = True
    for window in ("early_2020_2022", "heldout_2023_plus"):
        rows = {row["portfolio"]: row for row in performance[window]}
        control = rows["household same mandate control"]
        candidate = rows["household differentiated mandates"]
        checks = {
            "sharpe_improves": candidate["sharpe"] > control["sharpe"],
            "max_drawdown_improves": abs(candidate["max_dd"]) < abs(control["max_dd"]),
            "cagr_retains_at_least_90pct": candidate["cagr"] >= 0.90 * control["cagr"],
        }
        gates[window] = checks
        passed = passed and all(checks.values())

    # This candidate exists to trade CAGR for household drawdown reduction —
    # a risk_reducer, not a return_enhancer — but the original gate demanded
    # a strict Sharpe/drawdown improve plus an unexplained 90% CAGR floor,
    # which killed a held-out result that improved both risk metrics
    # outright. Re-judged here under backtest.promotion's risk_reducer class
    # with bounds fixed before running on the corrected panel: up to 3.0
    # CAGR points given up is an acceptable price for at least a 10%
    # relative reduction in household max drawdown. This does not replace
    # the pre-registered decision below; it answers whether the original
    # 90% floor was the actual reason this was rejected.
    risk_reducer_gates = {}
    for window in ("early_2020_2022", "heldout_2023_plus"):
        rows = {row["portfolio"]: row for row in performance[window]}
        risk_reducer_gates[window] = passes_gate(
            rows["household same mandate control"],
            rows["household differentiated mandates"],
            "risk_reducer",
            max_cagr_cost_pp=3.0,
            min_dd_improvement_pct=0.10,
        ).to_dict()

    payload = {
        "pre_registration": {
            "household_capital": {"base": 0.20, "2x": 0.80},
            "control_underlying_weights": current,
            "candidate_2x_underlying_weights": defensive_2x,
            "promotion_rule": (
                "Household Sharpe and max drawdown must improve and at least "
                "90% of CAGR must remain in both early and held-out windows."
            ),
        },
        "decision": "promote" if passed else "reject",
        "gates": gates,
        "risk_reducer_reexamination_2026_08_03": {
            "objective_class": "risk_reducer",
            "pre_declared_bounds": {"max_cagr_cost_pp": 3.0, "min_dd_improvement_pct": 0.10},
            "gates": risk_reducer_gates,
            "note": (
                "capital_split_study.json finds the base+2x household at the "
                "deployed 50/50 split behaves as roughly one 1.5x portfolio, "
                "not two diversified mandates - the 80/20 household weighting "
                "used here is a synthetic reweighting of two correlated "
                "accounts (both trade the same sleeves), not real "
                "diversification. That doesn't invalidate this test, but the "
                "'differentiated mandate' framing should not be read as "
                "adding a second independent return source."
            ),
        },
        "performance": performance,
        "limitations": [
            "Household returns assume daily rebalancing to an 80/20 account capital split.",
            "The current-company momentum universe is survivorship-biased.",
            "Historical short borrowability is unavailable.",
            "Both accounts are modeled from $10,000 to preserve current whole-share capacity tests; the household weighting is applied to returns.",
        ],
    }
    out = Path("reports/account_mandate_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nDecision: {payload['decision'].upper()}")
    print(json.dumps(gates, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
