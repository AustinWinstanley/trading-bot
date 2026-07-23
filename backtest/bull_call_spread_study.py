"""Exact and synthetic study of a defined-risk bullish SPY call spread.

The pre-selected policy buys one 60-DTE 105%/110% call debit spread on a
monthly roll while SPY is above its 200-day average and trailing 20-session
realized volatility is below 20%. Maximum modeled loss is capped at 4% of the
$10,000 account per trade and 8% per calendar year.

This is a convex return satellite, not a hedge or a replacement for the
portfolio. Promotion requires improved Sharpe and CAGR without more than 10%
relative drawdown deterioration in both exact-contract windows and both
long-history proxy windows. At least 12 exact spreads must complete.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.anticipatory_tail_hedge_study import normal_cdf
from backtest.capital_split_study import recent_capacity_profiles
from backtest.long_history_stress_study import (
    STRESS_WINDOWS,
    build_long_history_profiles,
    stress_window,
)
from backtest.production_portfolio import norm_index, returns_summary
from backtest.spy_put_spread_study import (
    START,
    SpreadPlan,
    add_dollar_pnl,
    fetch_daily_bars,
    monthly_roll_dates,
)
from engine.cboe import series
from engine.data import AlpacaClient
from engine.tiingo import load_parquet

OPTION_DIR = Path("state/options")
PLAN_PATH = OPTION_DIR / "spy_call_spread_105_110_plan.json"
BARS_PATH = OPTION_DIR / "spy_call_spread_105_110_bars.parquet"
REPORT_PATH = Path("reports/bull_call_spread_study.json")
LAST_COMPLETE_ROLL = pd.Timestamp("2026-05-31")
ACCOUNT_EQUITY = 10_000.0
TARGET_DTE = 60
LONG_MONEYNESS = 1.05
SHORT_MONEYNESS = 1.10
PER_TRADE_BUDGET = 0.04
ANNUAL_BUDGET = 0.08
FRICTION_PER_LEG = 0.10


def select_call_spread(
    contracts: list[dict],
    *,
    roll_date: pd.Timestamp,
    spot_reference: float,
) -> SpreadPlan:
    if not contracts:
        raise ValueError("no call contracts available")
    frame = pd.DataFrame(contracts)
    frame["expiration_date"] = pd.to_datetime(frame["expiration_date"])
    frame["strike"] = pd.to_numeric(frame["strike_price"])
    standard_monthly = (
        frame["expiration_date"].dt.weekday.isin((3, 4))
        & frame["expiration_date"].dt.day.between(15, 21)
    )
    established_increment = np.isclose(frame["strike"] % 5.0, 0.0)
    frame = frame[standard_monthly & established_increment]
    if frame.empty:
        raise ValueError("no standard monthly call contracts available")
    target_expiry = roll_date + pd.Timedelta(days=TARGET_DTE)
    expiry = min(
        frame["expiration_date"].unique(),
        key=lambda value: abs(pd.Timestamp(value) - target_expiry),
    )
    same_expiry = frame[frame["expiration_date"] == expiry]

    def nearest(target: float) -> pd.Series:
        return same_expiry.loc[
            (same_expiry["strike"] - target).abs().idxmin()
        ]

    long = nearest(LONG_MONEYNESS * spot_reference)
    short = nearest(SHORT_MONEYNESS * spot_reference)
    if float(long["strike"]) >= float(short["strike"]):
        raise ValueError("call spread strikes are not ordered")
    return SpreadPlan(
        roll_date=roll_date.date().isoformat(),
        expiration_date=pd.Timestamp(expiry).date().isoformat(),
        spot_reference=round(float(spot_reference), 4),
        long_symbol=str(long["symbol"]),
        long_strike=float(long["strike"]),
        short_symbol=str(short["symbol"]),
        short_strike=float(short["strike"]),
    )


def fetch_contract_plan(
    client: AlpacaClient,
    spy: pd.Series,
) -> list[SpreadPlan]:
    plans = []
    for roll_date in monthly_roll_dates(spy):
        if roll_date > LAST_COMPLETE_ROLL:
            continue
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
                "type": "call",
                "expiration_date_gte": (
                    roll_date + pd.Timedelta(days=40)
                ).date().isoformat(),
                "expiration_date_lte": (
                    roll_date + pd.Timedelta(days=85)
                ).date().isoformat(),
                "strike_price_gte": round(spot * 1.02, 2),
                "strike_price_lte": round(spot * 1.13, 2),
                "limit": 10_000,
            },
        )
        try:
            plan = select_call_spread(
                payload.get("option_contracts") or [],
                roll_date=roll_date,
                spot_reference=spot,
            )
        except ValueError as exc:
            raise ValueError(f"{roll_date.date()}: {exc}") from exc
        plans.append(plan)
    return plans


def refresh_cache(
    spy: pd.Series,
) -> tuple[list[SpreadPlan], pd.DataFrame]:
    OPTION_DIR.mkdir(parents=True, exist_ok=True)
    client = AlpacaClient()
    plans = fetch_contract_plan(client, spy)
    PLAN_PATH.write_text(
        json.dumps([asdict(plan) for plan in plans], indent=2)
    )
    symbols = sorted({
        symbol
        for plan in plans
        for symbol in (plan.long_symbol, plan.short_symbol)
    })
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
        raise FileNotFoundError("call-spread cache missing; run with --refresh")
    plans = [
        SpreadPlan(**row) for row in json.loads(PLAN_PATH.read_text())
    ]
    return plans, pd.read_parquet(BARS_PATH)


def bullish_signal(spy: pd.Series, date: pd.Timestamp) -> dict:
    prior = spy.loc[:date].iloc[:-1]
    if len(prior) < 200:
        return {
            "enabled": False,
            "spy_prior_close": None,
            "spy_ma_200": None,
            "realized_vol_20d": None,
        }
    returns = prior.pct_change(fill_method=None)
    ma200 = float(prior.tail(200).mean())
    realized = float(returns.tail(20).std() * np.sqrt(252))
    return {
        "enabled": bool(
            prior.iloc[-1] > ma200
            and np.isfinite(realized)
            and realized < 0.20
        ),
        "spy_prior_close": float(prior.iloc[-1]),
        "spy_ma_200": ma200,
        "realized_vol_20d": realized,
    }


def bar_panels(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    frame = bars.copy()
    index = pd.DatetimeIndex(frame["timestamp"])
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    frame["date"] = index.normalize()
    return {
        column: frame.pivot(
            index="date", columns="symbol", values=column
        ).sort_index()
        for column in ("o", "c")
    }


def exact_call_pnl(
    plans: list[SpreadPlan],
    bars: pd.DataFrame,
    spy: pd.Series,
    *,
    use_signal: bool,
    maximum_loss_per_trade: float | None = None,
    annual_loss_budget: float | None = None,
) -> tuple[pd.Series, list[dict]]:
    panels = bar_panels(bars)
    backtest_end = max(
        pd.Timestamp(plan.expiration_date) for plan in plans
    )
    dates = spy.loc[START:backtest_end].index
    pnl = pd.Series(0.0, index=dates)
    logs = []
    budget_used: dict[int, float] = {}
    for number, plan in enumerate(plans):
        roll = pd.Timestamp(plan.roll_date)
        signal = bullish_signal(spy, roll)
        enabled = signal["enabled"] if use_signal else True
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
        candidates = dates[
            (dates >= roll) & (dates <= roll + pd.Timedelta(days=7))
        ]
        entry_date = None
        entry_value = np.nan
        for candidate in candidates:
            long_open = panels["o"].get(
                plan.long_symbol, pd.Series(dtype=float)
            ).get(candidate, np.nan)
            short_open = panels["o"].get(
                plan.short_symbol, pd.Series(dtype=float)
            ).get(candidate, np.nan)
            if np.isfinite(long_open) and np.isfinite(short_open):
                entry_value = float(long_open - short_open)
                entry_date = candidate
                break
        if entry_date is None:
            record["rejected"] = "missing entry bar"
            logs.append(record)
            continue
        width = plan.short_strike - plan.long_strike
        entry_value = float(np.clip(entry_value, 0.0, width))
        if entry_value <= 0:
            record["rejected"] = "non-positive entry debit"
            logs.append(record)
            continue
        maximum_loss = (
            entry_value + 4.0 * FRICTION_PER_LEG
        ) * 100.0
        year = entry_date.year
        used = budget_used.get(year, 0.0)
        record.update({
            "entry_date": entry_date.date().isoformat(),
            "entry_debit": round(entry_value, 4),
            "maximum_loss_dollars": round(maximum_loss, 2),
        })
        if (
            maximum_loss_per_trade is not None
            and maximum_loss > maximum_loss_per_trade
        ):
            record["rejected"] = "maximum loss exceeds per-trade budget"
            logs.append(record)
            continue
        if (
            annual_loss_budget is not None
            and used + maximum_loss > annual_loss_budget
        ):
            record["rejected"] = "annual loss budget exhausted"
            record["annual_loss_budget_used"] = round(used, 2)
            logs.append(record)
            continue
        budget_used[year] = used + maximum_loss

        active_dates = dates[
            (dates >= entry_date) & (dates <= exit_date)
        ]
        values = pd.Series(index=active_dates, dtype=float)
        for date in active_dates:
            long_close = panels["c"].get(
                plan.long_symbol, pd.Series(dtype=float)
            ).get(date, np.nan)
            short_close = panels["c"].get(
                plan.short_symbol, pd.Series(dtype=float)
            ).get(date, np.nan)
            if np.isfinite(long_close) and np.isfinite(short_close):
                values.loc[date] = np.clip(
                    float(long_close - short_close), 0.0, width
                )
        if values.notna().sum() == 0:
            record["rejected"] = "no valuation bars"
            logs.append(record)
            continue
        if pd.isna(values.iloc[0]):
            values.iloc[0] = entry_value
        values = values.ffill()
        if exit_date == pd.Timestamp(plan.expiration_date):
            spot = float(spy.reindex(dates).ffill().loc[exit_date])
            values.iloc[-1] = (
                max(spot - plan.long_strike, 0.0)
                - max(spot - plan.short_strike, 0.0)
            )
        else:
            long_exit = panels["o"].get(
                plan.long_symbol, pd.Series(dtype=float)
            ).get(exit_date, np.nan)
            short_exit = panels["o"].get(
                plan.short_symbol, pd.Series(dtype=float)
            ).get(exit_date, np.nan)
            if np.isfinite(long_exit) and np.isfinite(short_exit):
                values.iloc[-1] = np.clip(
                    float(long_exit - short_exit), 0.0, width
                )
        values = values.ffill()
        trade_pnl = values.diff() * 100.0
        trade_pnl.iloc[0] = (
            (values.iloc[0] - entry_value) * 100.0
            - 2.0 * FRICTION_PER_LEG * 100.0
        )
        trade_pnl.iloc[-1] -= 2.0 * FRICTION_PER_LEG * 100.0
        pnl.loc[trade_pnl.index] += trade_pnl
        record.update({
            "annual_loss_budget_used": round(budget_used[year], 2),
            "pnl_dollars": round(float(trade_pnl.sum()), 2),
        })
        logs.append(record)
    return pnl, logs


def black_scholes_call(
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
        return max(spot - strike, 0.0)
    root_time = math.sqrt(years)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend_yield + volatility**2 / 2.0) * years
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    return (
        spot * math.exp(-dividend_yield * years) * normal_cdf(d1)
        - strike * math.exp(-rate * years) * normal_cdf(d2)
    )


def modeled_call_spread(
    spot: float,
    *,
    long_strike: float,
    short_strike: float,
    days_to_expiry: int,
    vix: float,
    long_vol_adjustment: float = -0.01,
    short_vol_adjustment: float = -0.02,
) -> float:
    if long_strike >= short_strike:
        raise ValueError("long strike must be below short strike")
    if days_to_expiry <= 0:
        return (
            max(spot - long_strike, 0.0)
            - max(spot - short_strike, 0.0)
        )
    base = max(float(vix) / 100.0, 0.05)
    long_call = black_scholes_call(
        spot,
        long_strike,
        days_to_expiry / 365.25,
        max(base + long_vol_adjustment, 0.03),
    )
    short_call = black_scholes_call(
        spot,
        short_strike,
        days_to_expiry / 365.25,
        max(base + short_vol_adjustment, 0.03),
    )
    return float(np.clip(
        long_call - short_call, 0.0, short_strike - long_strike
    ))


def synthetic_overlay(
    portfolio_returns: pd.Series,
    spy: pd.Series,
    vix: pd.Series,
    *,
    starting_equity: float = ACCOUNT_EQUITY,
    per_trade_budget: float = PER_TRADE_BUDGET,
    annual_budget: float = ANNUAL_BUDGET,
    long_vol_adjustment: float = -0.01,
    short_vol_adjustment: float = -0.02,
) -> tuple[pd.Series, list[dict]]:
    aligned = pd.concat({
        "portfolio": portfolio_returns,
        "spy": spy,
        "vix": vix,
    }, axis=1, sort=False).dropna()
    rolls = set(
        aligned.groupby(aligned.index.to_period("M")).head(1).index
    )
    equity = float(starting_equity)
    position = None
    active_year = None
    annual_limit = annual_used = 0.0
    output = []
    logs = []
    total_friction = 4.0 * FRICTION_PER_LEG

    for date, row in aligned.iterrows():
        previous = equity
        if active_year != date.year:
            active_year = date.year
            annual_limit = annual_budget * previous
            annual_used = 0.0
        option_pnl = 0.0
        if position is not None:
            remaining = max((position["expiration"] - date).days, 0)
            mark = modeled_call_spread(
                float(row["spy"]),
                long_strike=position["long_strike"],
                short_strike=position["short_strike"],
                days_to_expiry=remaining,
                vix=float(row["vix"]),
                long_vol_adjustment=long_vol_adjustment,
                short_vol_adjustment=short_vol_adjustment,
            )
            option_pnl = position["units"] * (
                mark - position["last_value"]
            )
            position["last_value"] = mark
        equity = previous * (1.0 + float(row["portfolio"])) + option_pnl

        if date in rolls:
            if position is not None:
                equity -= (
                    2.0 * FRICTION_PER_LEG * position["units"]
                )
                position["log"]["exit_date"] = date.date().isoformat()
                position = None
            signal = bullish_signal(aligned["spy"], date)
            record = {
                "roll_date": date.date().isoformat(),
                **signal,
                "vix": round(float(row["vix"]), 4),
            }
            if signal["enabled"]:
                spot = float(row["spy"])
                long_strike = LONG_MONEYNESS * spot
                short_strike = SHORT_MONEYNESS * spot
                debit = modeled_call_spread(
                    spot,
                    long_strike=long_strike,
                    short_strike=short_strike,
                    days_to_expiry=TARGET_DTE,
                    vix=float(row["vix"]),
                    long_vol_adjustment=long_vol_adjustment,
                    short_vol_adjustment=short_vol_adjustment,
                )
                loss_per_unit = debit + total_friction
                allocation = min(
                    per_trade_budget * equity,
                    annual_limit - annual_used,
                )
                if allocation > 0 and loss_per_unit > 0:
                    units = allocation / loss_per_unit
                    equity -= 2.0 * FRICTION_PER_LEG * units
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
        output.append(equity / previous - 1.0)
    return pd.Series(output, index=aligned.index), logs


def summarize(
    variants: dict[str, pd.Series],
    windows: dict[str, slice],
) -> dict[str, list[dict]]:
    return {
        window: [
            returns_summary(returns.loc[selector], label)
            for label, returns in variants.items()
        ]
        for window, selector in windows.items()
    }


def exact_pricing_diagnostics(
    logs: list[dict],
    spy: pd.Series,
    vix: pd.Series,
) -> dict:
    joined_vix = vix.reindex(spy.index).ffill()
    rows = []
    for row in logs:
        date = pd.Timestamp(row["entry_date"])
        modeled = modeled_call_spread(
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
            "modeled_to_actual": round(modeled / actual, 4),
        })
    return {
        "observations": len(rows),
        "median_modeled_to_actual": round(
            float(np.median([
                row["modeled_to_actual"] for row in rows
            ])),
            4,
        ) if rows else None,
        "rows": rows,
    }


def gate_reasons(
    results: dict,
    windows: tuple[str, ...],
    *,
    candidate: str,
) -> list[str]:
    reasons = []
    for window in windows:
        rows = {row["portfolio"]: row for row in results[window]}
        base, proposed = rows["no overlay"], rows[candidate]
        if proposed["sharpe"] <= base["sharpe"]:
            reasons.append(f"{window}: Sharpe did not improve")
        if proposed["cagr"] <= base["cagr"]:
            reasons.append(f"{window}: CAGR did not improve")
        if proposed["max_dd"] < 1.10 * base["max_dd"]:
            reasons.append(
                f"{window}: drawdown worsened by more than 10% relatively"
            )
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    spy = norm_index(
        load_parquet(["SPY"], Path("state/history_deep"))["SPY"]["close"]
    )
    vix = norm_index(series("VIX"))
    plans, bars = refresh_cache(spy) if args.refresh else load_cache()
    backtest_end = max(
        pd.Timestamp(plan.expiration_date) for plan in plans
    )
    recent = recent_capacity_profiles()["2x"].loc[START:backtest_end]

    exact_variants = {"no overlay": recent}
    exact_logs = {}
    for label, use_signal, capped in (
        ("always call spread", False, False),
        ("bull call spread", True, True),
    ):
        pnl, logs = exact_call_pnl(
            plans,
            bars,
            spy,
            use_signal=use_signal,
            maximum_loss_per_trade=(
                ACCOUNT_EQUITY * PER_TRADE_BUDGET if capped else None
            ),
            annual_loss_budget=(
                ACCOUNT_EQUITY * ANNUAL_BUDGET if capped else None
            ),
        )
        exact_variants[label] = add_dollar_pnl(recent, pnl)
        exact_logs[label] = logs
    exact_results = summarize(
        exact_variants,
        {
            "full": slice(None),
            "design_2024": slice(None, "2024-12-31"),
            "heldout_2025_plus": slice("2025-01-01", None),
        },
    )

    long_profiles, _, _ = build_long_history_profiles()
    incumbent = long_profiles["2x"].loc["2007":]
    synthetic, synthetic_logs = synthetic_overlay(incumbent, spy, vix)
    long_results = summarize(
        {"no overlay": incumbent, "bull call spread": synthetic},
        {
            "full": slice(None),
            "design_2007_2016": slice(None, "2016-12-31"),
            "heldout_2017_plus": slice("2017-01-01", None),
        },
    )

    completed = [
        row for row in exact_logs["bull call spread"]
        if row.get("enabled") and "pnl_dollars" in row
    ]
    reasons = []
    if len(completed) < 12:
        reasons.append(
            f"only {len(completed)} exact spreads completed; require 12"
        )
    reasons.extend(gate_reasons(
        exact_results,
        ("design_2024", "heldout_2025_plus"),
        candidate="bull call spread",
    ))
    reasons.extend(gate_reasons(
        long_results,
        ("design_2007_2016", "heldout_2017_plus"),
        candidate="bull call spread",
    ))
    passed = not reasons

    budget_sensitivity = {}
    for annual in (0.02, 0.04, 0.08, 0.12):
        candidate, _ = synthetic_overlay(
            incumbent,
            spy,
            vix,
            per_trade_budget=min(PER_TRADE_BUDGET, annual / 2),
            annual_budget=annual,
        )
        budget_sensitivity[f"{annual:.0%}_annual"] = returns_summary(
            candidate, f"{annual:.0%} annual budget"
        )
    pricing_sensitivity = {}
    for label, long_adjustment, short_adjustment in (
        ("optimistic_flat_vix", 0.00, 0.00),
        ("preselected_call_skew", -0.01, -0.02),
        ("expensive_long_call", 0.02, -0.02),
    ):
        candidate, _ = synthetic_overlay(
            incumbent,
            spy,
            vix,
            long_vol_adjustment=long_adjustment,
            short_vol_adjustment=short_adjustment,
        )
        pricing_sensitivity[label] = returns_summary(candidate, label)
    stress = {
        label: [
            stress_window(returns, start, end, window)
            for window, (start, end) in STRESS_WINDOWS.items()
        ]
        for label, returns in (
            ("no overlay", incumbent),
            ("bull call spread", synthetic),
        )
    }

    payload = {
        "conclusion": (
            "Add an off-by-default bullish call-spread paper shadow."
            if passed else
            "Do not add the bullish call spread; it failed the preselected "
            "cross-window evidence or executable-sizing gate."
        ),
        "promotion_rule_passed": passed,
        "failure_reasons": reasons,
        "preselected_policy": {
            "instrument": "60-DTE 105%/110% SPY call debit spread",
            "entry": (
                "monthly when prior SPY is above its 200DMA and prior "
                "20-session realized volatility is below 20%"
            ),
            "per_trade_maximum_loss_pct": PER_TRADE_BUDGET,
            "annual_maximum_loss_budget_pct": ANNUAL_BUDGET,
            "friction_per_leg": FRICTION_PER_LEG,
        },
        "feasibility_snapshot_2026_07_23": {
            "spy_reference_price": 737.81,
            "expiration": "2026-09-18",
            "long_call": "775 call indicative ask 3.96",
            "short_call": "810 call indicative bid 0.37",
            "marketable_debit": 3.59,
            "one_contract_maximum_loss_with_friction": 399.0,
        },
        "exact_contract": {
            "contract_plans": len(plans),
            "completed_spreads": len(completed),
            "winning_spreads": sum(
                float(row["pnl_dollars"]) > 0 for row in completed
            ),
            "total_option_pnl_dollars": round(sum(
                float(row["pnl_dollars"]) for row in completed
            ), 2),
            "synthetic_entry_price_check": exact_pricing_diagnostics(
                completed, spy, vix
            ),
            "results": exact_results,
            "logs": exact_logs,
        },
        "long_history_synthetic": {
            "results": long_results,
            "budget_sensitivity": budget_sensitivity,
            "pricing_sensitivity": pricing_sensitivity,
            "stress_windows": stress,
            "logs": synthetic_logs,
        },
        "limitations": [
            "Alpaca option trade-bar history begins only in February 2024.",
            "Daily trades are not executable bid/ask quotes.",
            "Expired metadata cannot prove when every strike was first listed.",
            "The long-history option marks are Black-Scholes stress estimates.",
            "VIX is not the historical SPY option surface.",
            "Synthetic fractional contracts cannot be traded live.",
            "American assignment, taxes, and exact margin are not replayed.",
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
    print(f"\ncompleted exact spreads: {len(completed)}/{len(plans)}")
    print(f"PROMOTION RULE: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"\nWrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
