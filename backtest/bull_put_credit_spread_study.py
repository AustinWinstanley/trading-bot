"""Exact-contract screen for an aggressive, defined-risk bull-put spread.

This is a distinct hypothesis from the rejected passive put-write indices and
the failed bull-call debit spread: sell volatility only while SPY is above its
200-day average and 20-day realized volatility is below 20%, using a 45-DTE
90%/85% put credit spread. One spread may be open at a time. Maximum loss is
capped at 5% of account equity and new entries stop after realized option
losses reach 10% of starting equity in a calendar year.

This exact-contract stage can only advance the idea to a standard-window
proxy study. It cannot promote live or paper-active trading by itself because
Alpaca option history begins in 2024 and therefore cannot cover the required
2020-2022 screen.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.capital_split_study import recent_capacity_profiles
from backtest.production_portfolio import norm_index, returns_summary
from backtest.promotion import passes_gate_all_cells
from backtest.spy_put_spread_study import (
    LAST_COMPLETE_ROLL,
    START,
    TAIL_BARS_PATH,
    TAIL_PLAN_PATH,
    SpreadPlan,
    add_dollar_pnl,
    bar_panels,
    hedge_signal,
    load_cache,
)
from engine.tiingo import load_parquet

REPORT_PATH = Path("reports/bull_put_credit_spread_study.json")
FRICTION_PER_LEG = 0.10
STARTING_EQUITY = 10_000.0
MAX_LOSS_PER_TRADE = 0.05 * STARTING_EQUITY
ANNUAL_REALIZED_LOSS_LIMIT = 0.10 * STARTING_EQUITY
MINIMUM_COMPLETED_SPREADS = 12


def credit_spread_terms(width: float, entry_credit: float, friction_per_leg: float) -> tuple[float, float]:
    """Return net maximum profit/loss dollars for one contract."""
    if width <= 0 or not 0 < entry_credit < width:
        raise ValueError("credit must be positive and below spread width")
    round_trip_friction = 4 * friction_per_leg
    maximum_profit = (entry_credit - round_trip_friction) * 100
    maximum_loss = (width - entry_credit + round_trip_friction) * 100
    return maximum_profit, maximum_loss


def diagnose_budget_feasibility(
    logs: list[dict],
    *,
    budget_rejection_reason: str,
) -> dict:
    """Distinguish "this construction is structurally too tight for its own
    max-loss budget" from "the market rarely offered this setup" — two
    different findings that a bare `completed_spreads: 0` count collapses
    into one. Both bull-put studies hit exactly this: every candidate that
    reached the budget check failed it (0 of N), which reads identically to
    "signal never fired" in the trade count alone, but is a design bug
    (width/friction/budget combination), not a market-opportunity finding.

    Reads `logs` as already produced by credit_spread_pnl — every entry that
    reached the maximum_loss comparison carries `maximum_loss_dollars`
    regardless of whether it passed, so this needs no re-simulation.
    """
    checked = [row for row in logs if "maximum_loss_dollars" in row]
    rejected_for_budget = [
        row for row in checked if row.get("rejected") == budget_rejection_reason
    ]
    all_budget_rejected = bool(checked) and len(rejected_for_budget) == len(checked)
    credits = [row["entry_credit"] for row in checked if "entry_credit" in row]
    return {
        "candidates_reaching_budget_check": len(checked),
        "rejected_for_budget": len(rejected_for_budget),
        "structurally_infeasible": all_budget_rejected,
        "observed_entry_credit_min": round(min(credits), 4) if credits else None,
        "observed_entry_credit_max": round(max(credits), 4) if credits else None,
    }


def credit_spread_pnl(
    plans: list[SpreadPlan],
    bars: pd.DataFrame,
    spy: pd.Series,
) -> tuple[pd.Series, list[dict]]:
    panels = bar_panels(bars)
    dates = spy.loc[START:LAST_COMPLETE_ROLL].index
    pnl = pd.Series(0.0, index=dates)
    logs = []
    realized_losses: dict[int, float] = {}

    for number, plan in enumerate(plans):
        roll = pd.Timestamp(plan.roll_date)
        if roll not in dates:
            continue
        signal = hedge_signal(spy, roll)
        enabled = not signal["below_trend"] and signal["realized_vol_20d"] < 0.20
        next_roll = (
            pd.Timestamp(plans[number + 1].roll_date)
            if number + 1 < len(plans)
            else pd.Timestamp(plan.expiration_date)
        )
        exit_date = min(next_roll, pd.Timestamp(plan.expiration_date))
        record = {
            **asdict(plan),
            **signal,
            "enabled": bool(enabled),
            "exit_date": exit_date.date().isoformat(),
        }
        if not enabled:
            logs.append(record)
            continue
        if realized_losses.get(roll.year, 0.0) >= ANNUAL_REALIZED_LOSS_LIMIT:
            record["rejected"] = "annual realized loss limit reached"
            logs.append(record)
            continue

        entry_date = None
        high_open = low_open = np.nan
        for candidate in dates[(dates >= roll) & (dates <= roll + pd.Timedelta(days=7))]:
            high_open = panels["o"].get(plan.long_symbol, pd.Series(dtype=float)).get(candidate, np.nan)
            low_open = panels["o"].get(plan.short_symbol, pd.Series(dtype=float)).get(candidate, np.nan)
            if np.isfinite(high_open) and np.isfinite(low_open):
                entry_date = candidate
                break
        if entry_date is None:
            record["rejected"] = "missing entry bar"
            logs.append(record)
            continue

        width = plan.long_strike - plan.short_strike
        entry_credit = float(np.clip(high_open - low_open, 0.0, width))
        try:
            maximum_profit, maximum_loss = credit_spread_terms(
                width, entry_credit, FRICTION_PER_LEG
            )
        except ValueError:
            record["rejected"] = "invalid entry credit"
            logs.append(record)
            continue
        if maximum_loss > MAX_LOSS_PER_TRADE:
            record.update(
                entry_date=entry_date.date().isoformat(),
                entry_credit=round(entry_credit, 4),
                maximum_loss_dollars=round(maximum_loss, 2),
                rejected="maximum loss exceeds 5% budget",
            )
            logs.append(record)
            continue

        active_dates = dates[(dates >= entry_date) & (dates <= exit_date)]
        values = pd.Series(index=active_dates, dtype=float)
        for date in active_dates:
            high_close = panels["c"].get(plan.long_symbol, pd.Series(dtype=float)).get(date, np.nan)
            low_close = panels["c"].get(plan.short_symbol, pd.Series(dtype=float)).get(date, np.nan)
            if np.isfinite(high_close) and np.isfinite(low_close):
                values.loc[date] = np.clip(float(high_close - low_close), 0.0, width)
        values = values.ffill()
        if values.isna().all():
            record["rejected"] = "no valuation bars"
            logs.append(record)
            continue
        if pd.isna(values.iloc[0]):
            values.iloc[0] = entry_credit
        if exit_date == pd.Timestamp(plan.expiration_date):
            spot = float(spy.reindex(dates).ffill().loc[exit_date])
            values.iloc[-1] = (
                max(plan.long_strike - spot, 0.0)
                - max(plan.short_strike - spot, 0.0)
            )
        else:
            high_exit = panels["o"].get(plan.long_symbol, pd.Series(dtype=float)).get(exit_date, np.nan)
            low_exit = panels["o"].get(plan.short_symbol, pd.Series(dtype=float)).get(exit_date, np.nan)
            if np.isfinite(high_exit) and np.isfinite(low_exit):
                values.iloc[-1] = np.clip(float(high_exit - low_exit), 0.0, width)
        values = values.ffill()

        trade_pnl = -values.diff() * 100
        trade_pnl.iloc[0] = -2 * FRICTION_PER_LEG * 100
        trade_pnl.iloc[-1] -= 2 * FRICTION_PER_LEG * 100
        pnl.loc[trade_pnl.index] += trade_pnl
        total = float(trade_pnl.sum())
        if total < 0:
            realized_losses[entry_date.year] = realized_losses.get(entry_date.year, 0.0) - total
        record.update(
            entry_date=entry_date.date().isoformat(),
            entry_credit=round(entry_credit, 4),
            maximum_profit_dollars=round(maximum_profit, 2),
            maximum_loss_dollars=round(maximum_loss, 2),
            pnl_dollars=round(total, 2),
            annual_realized_loss_used=round(realized_losses.get(entry_date.year, 0.0), 2),
        )
        logs.append(record)
    return pnl, logs


def main() -> None:
    plans, bars = load_cache(plan_path=TAIL_PLAN_PATH, bars_path=TAIL_BARS_PATH)
    spy = norm_index(load_parquet(["SPY"], Path("state/history_deep"))["SPY"]["close"])
    option_pnl, logs = credit_spread_pnl(plans, bars, spy)
    completed = [row for row in logs if "pnl_dollars" in row]
    profiles = recent_capacity_profiles()
    variants = {}
    for profile in ("base", "2x"):
        control = profiles[profile].loc[START:LAST_COMPLETE_ROLL]
        variants[(profile, "control")] = control
        variants[(profile, "bull put spread")] = add_dollar_pnl(control, option_pnl)

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
            cells.append((window, profile, by_variant["control"], by_variant["bull put spread"]))

    exact_gate = passes_gate_all_cells(cells, "return_enhancer")
    enough = len(completed) >= MINIMUM_COMPLETED_SPREADS
    advance = enough and exact_gate["passed"]
    feasibility = diagnose_budget_feasibility(
        logs, budget_rejection_reason="maximum loss exceeds 5% budget"
    )
    if feasibility["structurally_infeasible"]:
        print(
            "WARNING: every candidate that reached the budget check was "
            "rejected by it — this width/friction/max-loss combination may "
            "be structurally too tight for this construction's observed "
            f"credit range (${feasibility['observed_entry_credit_min']}-"
            f"${feasibility['observed_entry_credit_max']}), not a market-"
            "opportunity finding. See payload['feasibility']."
        )
    payload = {
        "decision": "advance_to_standard_window_proxy" if advance else "insufficient_evidence",
        "pre_registration": {
            "hypothesis": "A deeply OTM defined-risk SPY put credit spread captures volatility premium during established low-volatility uptrends.",
            "short_put_moneyness": 0.90,
            "long_put_moneyness": 0.85,
            "target_dte": 45,
            "entry_rule": "First session monthly when prior SPY is above its 200DMA and prior 20-session realized volatility is below 20%.",
            "maximum_loss_per_trade_pct": 0.05,
            "annual_realized_loss_limit_pct": 0.10,
            "minimum_completed_spreads": MINIMUM_COMPLETED_SPREADS,
            "stage_gate": "Higher Sharpe, no lower CAGR, and no worse drawdown in both profiles and both exact-data windows.",
            "scope": "Passing advances only to a standard 2020-2022/2023+ proxy screen; it cannot activate paper orders.",
        },
        "completed_spreads": len(completed),
        "feasibility": feasibility,
        "stage_gate": exact_gate,
        "performance": performance,
        "trade_summary": {
            "wins": sum(row["pnl_dollars"] > 0 for row in completed),
            "losses": sum(row["pnl_dollars"] < 0 for row in completed),
            "total_pnl_dollars": round(sum(row["pnl_dollars"] for row in completed), 2),
            "median_pnl_dollars": round(float(np.median([row["pnl_dollars"] for row in completed])), 2) if completed else None,
        },
        "trades": logs,
        "limitations": [
            "Alpaca option history begins in February 2024 and cannot cover the required early screen.",
            "Daily trade bars are not executable bid/ask quotes.",
            "American early assignment is not reconstructed.",
            "One contract creates lumpy exposure in a $10,000 account.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"completed spreads: {len(completed)}")
    print(f"stage gate: {'PASS' if exact_gate['passed'] else 'FAIL'}")
    print(f"decision: {payload['decision']}")
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
