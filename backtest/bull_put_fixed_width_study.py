"""Capacity-designed successor to the infeasible 90%/85% credit spread.

The prior pre-registered spread risked $2,000-$3,400 per contract because five
percentage points of SPY moneyness is a $20-$35 strike width. This study fixes
the structural sizing error without changing the signal or risk budget: sell
the standard-monthly put nearest 90% spot and buy the put exactly $5 lower.
All stage-gate restrictions from bull_put_credit_spread_study remain binding.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.bull_put_credit_spread_study import (
    MINIMUM_COMPLETED_SPREADS,
    credit_spread_pnl,
    diagnose_budget_feasibility,
)
from backtest.capital_split_study import recent_capacity_profiles
from backtest.production_portfolio import norm_index, returns_summary
from backtest.promotion import passes_gate_all_cells
from backtest.spy_put_spread_study import (
    LAST_COMPLETE_ROLL,
    START,
    SpreadPlan,
    add_dollar_pnl,
    fetch_daily_bars,
    monthly_roll_dates,
)
from engine.data import AlpacaClient
from engine.tiingo import load_parquet

OPTION_DIR = Path("state/options")
PLAN_PATH = OPTION_DIR / "spy_bull_put_90_fixed5_plan.json"
BARS_PATH = OPTION_DIR / "spy_bull_put_90_fixed5_bars.parquet"
REPORT_PATH = Path("reports/bull_put_fixed_width_study.json")


def select_fixed_width_put_spread(
    contracts: list[dict],
    *,
    roll_date: pd.Timestamp,
    spot_reference: float,
    short_moneyness: float = 0.90,
    width: float = 5.0,
) -> SpreadPlan:
    if not contracts:
        raise ValueError("no put contracts available")
    frame = pd.DataFrame(contracts)
    frame["expiration_date"] = pd.to_datetime(frame["expiration_date"])
    frame["strike"] = pd.to_numeric(frame["strike_price"])
    standard = (
        frame["expiration_date"].dt.weekday.isin((3, 4))
        & frame["expiration_date"].dt.day.between(15, 21)
        & np.isclose(frame["strike"] % 5.0, 0.0)
    )
    frame = frame[standard]
    if frame.empty:
        raise ValueError("no standard monthly contracts available")
    target_expiry = roll_date + pd.Timedelta(days=45)
    expiry = min(
        frame["expiration_date"].unique(),
        key=lambda value: abs(pd.Timestamp(value) - target_expiry),
    )
    same_expiry = frame[frame["expiration_date"] == expiry]
    short = same_expiry.loc[
        (same_expiry["strike"] - short_moneyness * spot_reference).abs().idxmin()
    ]
    long_target = float(short["strike"]) - width
    long = same_expiry.loc[(same_expiry["strike"] - long_target).abs().idxmin()]
    actual_width = float(short["strike"]) - float(long["strike"])
    if not np.isclose(actual_width, width):
        raise ValueError(f"no exact ${width:g} lower strike")
    # SpreadPlan originated with a put-debit spread and calls the higher put
    # `long`. credit_spread_pnl intentionally sells that symbol and buys the
    # lower `short_symbol`; retain the shared shape to reuse audited pricing.
    return SpreadPlan(
        roll_date=roll_date.date().isoformat(),
        expiration_date=pd.Timestamp(expiry).date().isoformat(),
        spot_reference=round(float(spot_reference), 4),
        long_symbol=str(short["symbol"]),
        long_strike=float(short["strike"]),
        short_symbol=str(long["symbol"]),
        short_strike=float(long["strike"]),
    )


def refresh_cache(spy: pd.Series) -> tuple[list[SpreadPlan], pd.DataFrame]:
    OPTION_DIR.mkdir(parents=True, exist_ok=True)
    client = AlpacaClient()
    plans = []
    for roll_date in monthly_roll_dates(spy):
        position = spy.index.get_loc(roll_date)
        if position < 1:
            continue
        spot = float(spy.iloc[position - 1])
        payload = client._get(
            client.trading_base,
            "/v2/options/contracts",
            {
                "underlying_symbols": "SPY",
                "status": "inactive",
                "type": "put",
                "expiration_date_gte": (roll_date + pd.Timedelta(days=35)).date().isoformat(),
                "expiration_date_lte": (roll_date + pd.Timedelta(days=55)).date().isoformat(),
                "strike_price_gte": round(spot * 0.86, 2),
                "strike_price_lte": round(spot * 0.93, 2),
                "limit": 10_000,
            },
        )
        plans.append(select_fixed_width_put_spread(
            payload.get("option_contracts") or [],
            roll_date=roll_date,
            spot_reference=spot,
        ))
    PLAN_PATH.write_text(json.dumps([asdict(plan) for plan in plans], indent=2))
    symbols = sorted({s for p in plans for s in (p.long_symbol, p.short_symbol)})
    bars = fetch_daily_bars(
        client,
        symbols,
        start=plans[0].roll_date,
        end=max(plan.expiration_date for plan in plans),
    )
    bars.to_parquet(BARS_PATH, index=False)
    return plans, bars


def load_cache() -> tuple[list[SpreadPlan], pd.DataFrame]:
    if not PLAN_PATH.exists() or not BARS_PATH.exists():
        raise FileNotFoundError("fixed-width option cache missing; run with --refresh")
    plans = [SpreadPlan(**row) for row in json.loads(PLAN_PATH.read_text())]
    return plans, pd.read_parquet(BARS_PATH)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    spy = norm_index(load_parquet(["SPY"], Path("state/history_deep"))["SPY"]["close"])
    plans, bars = refresh_cache(spy) if args.refresh else load_cache()
    option_pnl, logs = credit_spread_pnl(plans, bars, spy)
    completed = [row for row in logs if "pnl_dollars" in row]

    profiles = recent_capacity_profiles()
    variants = {}
    for profile in ("base", "2x"):
        control = profiles[profile].loc[START:LAST_COMPLETE_ROLL]
        variants[(profile, "control")] = control
        variants[(profile, "fixed $5 bull put spread")] = add_dollar_pnl(control, option_pnl)

    performance = {}
    cells = []
    for window, selector in {
        "design_2024": slice(None, "2024-12-31"),
        "heldout_2025_plus": slice("2025-01-01", None),
    }.items():
        rows = []
        for (profile, variant), returns in variants.items():
            row = returns_summary(returns.loc[selector], f"{profile} — {variant}")
            row.update(profile=profile, variant=variant)
            rows.append(row)
        performance[window] = rows
        for profile in ("base", "2x"):
            by_variant = {r["variant"]: r for r in rows if r["profile"] == profile}
            cells.append((
                window,
                profile,
                by_variant["control"],
                by_variant["fixed $5 bull put spread"],
            ))
    gate = passes_gate_all_cells(cells, "return_enhancer")
    enough = len(completed) >= MINIMUM_COMPLETED_SPREADS
    advance = enough and gate["passed"]
    feasibility = diagnose_budget_feasibility(
        logs, budget_rejection_reason="maximum loss exceeds 5% budget"
    )
    if feasibility["structurally_infeasible"]:
        print(
            "WARNING: every candidate that reached the budget check was "
            "rejected by it — fixed $5 width may still be structurally too "
            "tight for this construction's observed credit range "
            f"(${feasibility['observed_entry_credit_min']}-"
            f"${feasibility['observed_entry_credit_max']}) against the 5% "
            "max-loss budget, not a market-opportunity finding. See "
            "payload['feasibility']."
        )
    payload = {
        "decision": "advance_to_standard_window_proxy" if advance else "insufficient_evidence",
        "supersedes_for_capacity_only": "bull_put_credit_spread_study 90%/85% geometry",
        "pre_registration": {
            "short_put_moneyness": 0.90,
            "fixed_strike_width": 5.0,
            "signal_and_risk_policy": "Unchanged from bull_put_credit_spread_study.",
            "minimum_completed_spreads": MINIMUM_COMPLETED_SPREADS,
            "stage_gate": "Higher Sharpe, no lower CAGR, and no worse drawdown in both profiles and both exact-data windows.",
            "scope": "Passing advances only to the mandatory standard-window proxy screen.",
        },
        "completed_spreads": len(completed),
        "feasibility": feasibility,
        "stage_gate": gate,
        "performance": performance,
        "trade_summary": {
            "wins": sum(row["pnl_dollars"] > 0 for row in completed),
            "losses": sum(row["pnl_dollars"] < 0 for row in completed),
            "total_pnl_dollars": round(sum(row["pnl_dollars"] for row in completed), 2),
            "median_pnl_dollars": round(float(np.median([row["pnl_dollars"] for row in completed])), 2) if completed else None,
        },
        "trades": logs,
        "limitations": [
            "Alpaca option history begins in February 2024.",
            "Daily trades are not executable bid/ask quotes.",
            "American early assignment is not reconstructed.",
            "The exact-data stage cannot cover 2020-2022.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"completed spreads: {len(completed)}")
    print(f"stage gate: {'PASS' if gate['passed'] else 'FAIL'}")
    print(f"decision: {payload['decision']}")
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
