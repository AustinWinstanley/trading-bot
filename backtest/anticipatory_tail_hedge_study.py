"""Test buying inexpensive tail protection before volatility rises.

Pre-selected policy:

* use a 45-DTE 90%/85% SPY put debit spread;
* enter on a monthly roll only while SPY is above its 200-day average and
  trailing 20-session realized volatility is below 15%;
* risk at most 2% of account equity per spread and 4% per calendar year.

The exact-contract study uses one-contract Alpaca trade bars and a fixed
$10,000 account, so it cannot scale below one contract. A deliberately
conservative long-history proxy uses continuous strikes, Cboe VIX plus an OTM
skew premium, fractional sizing, and the 2007+ 2x portfolio proxy. The proxy is
a stress model, not evidence of executable fills.

Promotion requires the rule to improve Sharpe and drawdown while retaining at
least 85% of CAGR in both 2007-2016 and 2017+, and at least 12 completed exact
spreads with improvement in both 2024 and 2025+. Exploratory controls cannot
promote the strategy.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.capital_split_study import recent_capacity_profiles
from backtest.long_history_stress_study import (
    STRESS_WINDOWS,
    build_long_history_profiles,
    stress_window,
)
from backtest.production_portfolio import norm_index, returns_summary
from backtest.spy_put_spread_study import (
    LAST_COMPLETE_ROLL,
    START,
    TAIL_BARS_PATH,
    TAIL_PLAN_PATH,
    add_dollar_pnl,
    load_cache,
    spread_pnl,
)
from engine.cboe import series
from engine.tiingo import load_parquet

REPORT_PATH = Path("reports/anticipatory_tail_hedge_study.json")
ACCOUNT_EQUITY = 10_000.0
PER_TRADE_BUDGET = 0.02
ANNUAL_BUDGET = 0.04
FRICTION_PER_LEG = 0.10
LONG_MONEYNESS = 0.90
SHORT_MONEYNESS = 0.85
TARGET_DTE = 45


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def black_scholes_put(
    spot: float,
    strike: float,
    years: float,
    volatility: float,
    *,
    rate: float = 0.04,
    dividend_yield: float = 0.013,
) -> float:
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if years <= 0:
        return max(strike - spot, 0.0)
    if volatility <= 0:
        raise ValueError("volatility must be positive")
    root_time = math.sqrt(years)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend_yield + volatility**2 / 2.0) * years
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    return (
        strike * math.exp(-rate * years) * normal_cdf(-d2)
        - spot * math.exp(-dividend_yield * years) * normal_cdf(-d1)
    )


def modeled_spread_value(
    spot: float,
    *,
    long_strike: float,
    short_strike: float,
    days_to_expiry: int,
    vix: float,
    long_skew_addition: float = 0.05,
    short_skew_addition: float = 0.08,
) -> float:
    """Conservative mark using VIX plus a fixed downside-skew premium."""
    if long_strike <= short_strike:
        raise ValueError("long strike must exceed short strike")
    if days_to_expiry <= 0:
        return (
            max(long_strike - spot, 0.0)
            - max(short_strike - spot, 0.0)
        )
    base_vol = max(float(vix) / 100.0, 0.05)
    long_put = black_scholes_put(
        spot,
        long_strike,
        days_to_expiry / 365.25,
        base_vol + long_skew_addition,
    )
    short_put = black_scholes_put(
        spot,
        short_strike,
        days_to_expiry / 365.25,
        base_vol + short_skew_addition,
    )
    width = long_strike - short_strike
    return float(np.clip(long_put - short_put, 0.0, width))


def calm_signal(spy: pd.Series, date: pd.Timestamp) -> dict:
    prior = spy.loc[:date].iloc[:-1]
    if prior.empty:
        return {
            "enabled": False,
            "spy_prior_close": None,
            "spy_ma_200": None,
            "realized_vol_20d": None,
        }
    returns = prior.pct_change(fill_method=None)
    ma200 = float(prior.tail(200).mean()) if len(prior) >= 200 else np.nan
    realized = float(returns.tail(20).std() * np.sqrt(252))
    enabled = bool(
        len(prior) >= 200
        and prior.iloc[-1] > ma200
        and np.isfinite(realized)
        and realized < 0.15
    )
    return {
        "enabled": enabled,
        "spy_prior_close": float(prior.iloc[-1]),
        "spy_ma_200": ma200,
        "realized_vol_20d": realized,
    }


def synthetic_overlay(
    portfolio_returns: pd.Series,
    spy: pd.Series,
    vix: pd.Series,
    *,
    starting_equity: float = ACCOUNT_EQUITY,
    per_trade_budget: float = PER_TRADE_BUDGET,
    annual_budget: float = ANNUAL_BUDGET,
    long_skew_addition: float = 0.05,
    short_skew_addition: float = 0.08,
) -> tuple[pd.Series, list[dict]]:
    aligned = pd.concat(
        {
            "portfolio": portfolio_returns,
            "spy": spy,
            "vix": vix,
        },
        axis=1,
        sort=False,
    ).dropna()
    roll_dates = set(
        aligned.groupby(aligned.index.to_period("M")).head(1).index
    )
    equity = float(starting_equity)
    position = None
    active_year = None
    annual_budget_dollars = 0.0
    annual_used = 0.0
    output = []
    logs = []
    total_friction_per_unit = 4.0 * FRICTION_PER_LEG

    for date, row in aligned.iterrows():
        previous_equity = equity
        option_pnl = 0.0
        if active_year != date.year:
            active_year = date.year
            annual_budget_dollars = annual_budget * previous_equity
            annual_used = 0.0

        if position is not None:
            remaining = max((position["expiration"] - date).days, 0)
            mark = modeled_spread_value(
                float(row["spy"]),
                long_strike=position["long_strike"],
                short_strike=position["short_strike"],
                days_to_expiry=remaining,
                vix=float(row["vix"]),
                long_skew_addition=long_skew_addition,
                short_skew_addition=short_skew_addition,
            )
            option_pnl += position["units"] * (
                mark - position["last_value"]
            )
            position["last_value"] = mark

        equity = (
            previous_equity * (1.0 + float(row["portfolio"]))
            + option_pnl
        )

        if date in roll_dates:
            if position is not None:
                exit_friction = (
                    2.0 * FRICTION_PER_LEG * position["units"]
                )
                equity -= exit_friction
                position["log"]["exit_date"] = date.date().isoformat()
                position["log"]["exit_friction_dollars"] = round(
                    exit_friction, 2
                )
                position = None

            signal = calm_signal(aligned["spy"], date)
            record = {
                "roll_date": date.date().isoformat(),
                **signal,
                "vix": round(float(row["vix"]), 4),
            }
            if signal["enabled"]:
                spot = float(row["spy"])
                long_strike = LONG_MONEYNESS * spot
                short_strike = SHORT_MONEYNESS * spot
                debit = modeled_spread_value(
                    spot,
                    long_strike=long_strike,
                    short_strike=short_strike,
                    days_to_expiry=TARGET_DTE,
                    vix=float(row["vix"]),
                    long_skew_addition=long_skew_addition,
                    short_skew_addition=short_skew_addition,
                )
                maximum_loss_per_unit = debit + total_friction_per_unit
                allocation = min(
                    per_trade_budget * equity,
                    annual_budget_dollars - annual_used,
                )
                if allocation > 0 and maximum_loss_per_unit > 0:
                    units = allocation / maximum_loss_per_unit
                    entry_friction = 2.0 * FRICTION_PER_LEG * units
                    equity -= entry_friction
                    annual_used += allocation
                    record.update({
                        "entry_debit_per_unit": round(debit, 4),
                        "units": round(units, 4),
                        "maximum_loss_dollars": round(allocation, 2),
                        "annual_budget_used": round(annual_used, 2),
                    })
                    position = {
                        "expiration": date + pd.Timedelta(days=TARGET_DTE),
                        "long_strike": long_strike,
                        "short_strike": short_strike,
                        "last_value": debit,
                        "units": units,
                        "log": record,
                    }
                else:
                    record["rejected"] = "annual loss budget exhausted"
            logs.append(record)
        output.append(equity / previous_equity - 1.0)

    return pd.Series(output, index=aligned.index), logs


def exact_pricing_diagnostics(
    logs: list[dict],
    spy: pd.Series,
    vix: pd.Series,
) -> dict:
    joined_vix = vix.reindex(spy.index).ffill()
    rows = []
    for row in logs:
        if "entry_debit" not in row or row.get("rejected"):
            continue
        date = pd.Timestamp(row["entry_date"])
        modeled = modeled_spread_value(
            float(spy.loc[date]),
            long_strike=float(row["long_strike"]),
            short_strike=float(row["short_strike"]),
            days_to_expiry=max(
                (pd.Timestamp(row["expiration_date"]) - date).days, 0
            ),
            vix=float(joined_vix.loc[date]),
        )
        actual = float(row["entry_debit"])
        rows.append({
            "entry_date": row["entry_date"],
            "actual_trade_bar_debit": round(actual, 4),
            "modeled_debit": round(modeled, 4),
            "modeled_to_actual": (
                round(modeled / actual, 4) if actual > 0 else None
            ),
        })
    ratios = [
        row["modeled_to_actual"] for row in rows
        if row["modeled_to_actual"] is not None
    ]
    return {
        "observations": len(rows),
        "median_modeled_to_actual": (
            round(float(np.median(ratios)), 4) if ratios else None
        ),
        "rows": rows,
    }


def summarize_windows(
    variants: dict[str, pd.Series],
    windows: dict[str, slice],
) -> dict[str, list[dict]]:
    return {
        name: [
            returns_summary(returns.loc[selector], label)
            for label, returns in variants.items()
        ]
        for name, selector in windows.items()
    }


def cross_window_gate(
    results: dict,
    windows: tuple[str, ...],
    *,
    incumbent: str,
    candidate: str,
) -> list[str]:
    reasons = []
    for window in windows:
        rows = {row["portfolio"]: row for row in results[window]}
        base, hedge = rows[incumbent], rows[candidate]
        if hedge["sharpe"] <= base["sharpe"]:
            reasons.append(f"{window}: Sharpe did not improve")
        if hedge["max_dd"] <= base["max_dd"]:
            reasons.append(f"{window}: maximum drawdown did not improve")
        if hedge["cagr"] < 0.85 * base["cagr"]:
            reasons.append(f"{window}: retained less than 85% of CAGR")
    return reasons


def main() -> None:
    spy = norm_index(
        load_parquet(["SPY"], Path("state/history_deep"))["SPY"]["close"]
    )
    vix = norm_index(series("VIX"))
    tail_plans, tail_bars = load_cache(
        plan_path=TAIL_PLAN_PATH,
        bars_path=TAIL_BARS_PATH,
    )
    recent = recent_capacity_profiles()["2x"].loc[
        START:LAST_COMPLETE_ROLL
    ]
    exact_variants = {"no hedge": recent}
    exact_logs = {}
    exact_specs = {
        "always 90/85": {"mode": "always"},
        "reactive 90/85": {"mode": "conditional"},
        "anticipatory 90/85": {
            "mode": "calm",
            "maximum_loss_per_trade": ACCOUNT_EQUITY * PER_TRADE_BUDGET,
            "annual_loss_budget": ACCOUNT_EQUITY * ANNUAL_BUDGET,
        },
    }
    for label, spec in exact_specs.items():
        pnl, logs = spread_pnl(
            tail_plans,
            tail_bars,
            spy,
            friction_per_leg=FRICTION_PER_LEG,
            **spec,
        )
        exact_variants[label] = add_dollar_pnl(recent, pnl)
        exact_logs[label] = logs
    exact_results = summarize_windows(
        exact_variants,
        {
            "full": slice(None),
            "design_2024": slice(None, "2024-12-31"),
            "heldout_2025_plus": slice("2025-01-01", None),
        },
    )

    long_profiles, _, _ = build_long_history_profiles()
    long_incumbent = long_profiles["2x"].loc["2007":]
    synthetic, synthetic_logs = synthetic_overlay(
        long_incumbent, spy, vix
    )
    long_results = summarize_windows(
        {"no hedge": long_incumbent, "anticipatory 90/85": synthetic},
        {
            "full": slice(None),
            "design_2007_2016": slice(None, "2016-12-31"),
            "heldout_2017_plus": slice("2017-01-01", None),
        },
    )
    budget_sensitivity = {}
    for budget in (0.01, 0.02, 0.04, 0.06):
        candidate, _ = synthetic_overlay(
            long_incumbent,
            spy,
            vix,
            per_trade_budget=min(0.02, budget / 2.0),
            annual_budget=budget,
        )
        budget_sensitivity[f"{budget:.0%}_annual"] = returns_summary(
            candidate, f"{budget:.0%} annual budget"
        )
    pricing_sensitivity = {}
    for label, long_skew, short_skew in (
        ("optimistic_flat_vix", 0.00, 0.00),
        ("preselected_skew", 0.05, 0.08),
        ("severe_skew", 0.08, 0.12),
    ):
        candidate, _ = synthetic_overlay(
            long_incumbent,
            spy,
            vix,
            long_skew_addition=long_skew,
            short_skew_addition=short_skew,
        )
        pricing_sensitivity[label] = returns_summary(candidate, label)
    stress_windows = {
        label: [
            stress_window(returns, start, end, window)
            for window, (start, end) in STRESS_WINDOWS.items()
        ]
        for label, returns in (
            ("no hedge", long_incumbent),
            ("anticipatory 90/85", synthetic),
        )
    }

    completed_exact = [
        row for row in exact_logs["anticipatory 90/85"]
        if row.get("enabled") and "pnl_dollars" in row
    ]
    reasons = []
    if len(completed_exact) < 12:
        reasons.append(
            f"only {len(completed_exact)} exact spreads completed; require 12"
        )
    reasons.extend(cross_window_gate(
        exact_results,
        ("design_2024", "heldout_2025_plus"),
        incumbent="no hedge",
        candidate="anticipatory 90/85",
    ))
    reasons.extend(cross_window_gate(
        long_results,
        ("design_2007_2016", "heldout_2017_plus"),
        incumbent="no hedge",
        candidate="anticipatory 90/85",
    ))
    passed = not reasons

    payload = {
        "conclusion": (
            "Add an off-by-default anticipatory tail-hedge paper shadow."
            if passed else
            "Do not add the anticipatory hedge; it failed the preselected "
            "cross-window evidence gate."
        ),
        "decision": "promote" if passed else "insufficient_evidence",
        "evidence_note_2026_08_03": (
            "Relabeled from a flat rejection. Two independent problems, not "
            "one clean result: (1) only 6 exact-contract spreads completed "
            "against a self-declared minimum of 12 - too few to conclude "
            "anything from the exact-contract arm alone. (2) The "
            "long_history_synthetic arm supplies 5 of the 9 failure_reasons "
            "(design_2007_2016 and heldout_2017_plus rows) and its pricing "
            "model overpays for the hedge by a median 1.4311x versus the six "
            "comparable exact-contract debits observed (individual entries up "
            "to 4.11x) - see exact_contract.synthetic_entry_price_check. A "
            "hedge-cost model biased 43% high cannot produce an informative "
            "rejection of a hedge. Recalibrate the synthetic pricing against "
            "observed exact-contract debits before citing the long-history "
            "failure reasons again, and let more Alpaca option history "
            "accrue before treating the exact-contract arm as decisive."
        ),
        "promotion_rule_passed": passed,
        "failure_reasons": reasons,
        "preselected_policy": {
            "instrument": "45-DTE 90%/85% SPY put debit spread",
            "entry": (
                "first session monthly when prior SPY is above its 200DMA "
                "and prior 20-session realized volatility is below 15%"
            ),
            "per_trade_maximum_loss_pct": PER_TRADE_BUDGET,
            "annual_maximum_loss_budget_pct": ANNUAL_BUDGET,
            "friction_per_leg": FRICTION_PER_LEG,
        },
        "exact_contract": {
            "results": exact_results,
            "completed_spreads": len(completed_exact),
            "synthetic_entry_price_check": exact_pricing_diagnostics(
                completed_exact, spy, vix
            ),
            "logs": exact_logs,
        },
        "long_history_synthetic": {
            "results": long_results,
            "budget_sensitivity": budget_sensitivity,
            "pricing_sensitivity": pricing_sensitivity,
            "stress_windows": stress_windows,
            "logs": synthetic_logs,
            "pricing": {
                "base_implied_volatility": "same-day Cboe VIX close",
                "long_put_skew_addition": 0.05,
                "short_put_skew_addition": 0.08,
                "rate": 0.04,
                "dividend_yield": 0.013,
                "fractional_contract_sizing": True,
            },
        },
        "limitations": [
            "Exact Alpaca option trade bars begin only in February 2024.",
            "Daily trades are not executable bid/ask quotes.",
            "The long-history option marks are Black-Scholes stress estimates.",
            "VIX is an ATM index proxy, not the historical SPY option surface.",
            "Fractional synthetic sizing is unavailable for live contracts.",
            "Fixed rate, dividend, and skew assumptions simplify history.",
            "American assignment and taxes are not reconstructed.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2))

    for title, results in (
        ("EXACT CONTRACT", exact_results),
        ("LONG-HISTORY SYNTHETIC", long_results),
    ):
        for window, rows in results.items():
            print(f"\n{title} — {window}")
            print(pd.DataFrame(rows)[
                ["portfolio", "cagr", "sharpe", "max_dd"]
            ].to_string(index=False))
    print(
        f"\nanticipatory exact spreads: "
        f"{len(completed_exact)}/{len(tail_plans)}"
    )
    print(f"PROMOTION RULE: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"\nWrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
