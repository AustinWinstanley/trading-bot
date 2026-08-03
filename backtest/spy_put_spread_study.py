"""Exact-contract study of a conditional SPY put-debit-spread hedge.

At the first SPY session of each month, select an expiry closest to 45 calendar
days and the listed puts nearest 95% and 90% of the prior SPY close. The
pre-selected rule buys one spread for the $10k 2x account only when prior SPY
is below its 200-day average or prior 20-session realized volatility exceeds
20%. Always-on, trend-only, and volatility-only variants are controls.

Historical Alpaca option bars begin in February 2024 and contain trades rather
than executable bid/ask quotes. Entry and exit therefore use bar opens plus a
configurable per-leg friction penalty. No paper execution feature may be
promoted unless the pre-selected rule improves Sharpe and drawdown in both
2024 and 2025+ while retaining 80% of CAGR and observing at least 36 completed
spreads. The observation minimum deliberately prevents a short smooth sample
from authorizing an options strategy.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.capital_split_study import recent_capacity_profiles
from backtest.production_portfolio import norm_index, returns_summary
from engine.data import AlpacaClient
from engine.tiingo import load_parquet

OPTION_DIR = Path("state/options")
PLAN_PATH = OPTION_DIR / "spy_put_spread_plan.json"
BARS_PATH = OPTION_DIR / "spy_put_spread_bars.parquet"
TAIL_PLAN_PATH = OPTION_DIR / "spy_put_spread_90_85_plan.json"
TAIL_BARS_PATH = OPTION_DIR / "spy_put_spread_90_85_bars.parquet"
REPORT_PATH = Path("reports/spy_put_spread_study.json")
START = pd.Timestamp("2024-02-01")
LAST_COMPLETE_ROLL = pd.Timestamp("2026-06-30")


@dataclass(frozen=True)
class SpreadPlan:
    roll_date: str
    expiration_date: str
    spot_reference: float
    long_symbol: str
    long_strike: float
    short_symbol: str
    short_strike: float


def monthly_roll_dates(spy: pd.Series) -> pd.DatetimeIndex:
    eligible = spy.loc[START:LAST_COMPLETE_ROLL].dropna()
    return pd.DatetimeIndex(
        eligible.groupby(eligible.index.to_period("M")).head(1).index
    )


def select_put_spread(
    contracts: list[dict],
    *,
    roll_date: pd.Timestamp,
    spot_reference: float,
    long_moneyness: float = 0.95,
    short_moneyness: float = 0.90,
) -> SpreadPlan:
    if not contracts:
        raise ValueError("no put contracts available")
    frame = pd.DataFrame(contracts)
    frame["expiration_date"] = pd.to_datetime(frame["expiration_date"])
    frame["strike"] = pd.to_numeric(frame["strike_price"])
    # Expired chains include daily/event expirations and strikes introduced
    # after the historical decision date. Standard third-Friday expirations
    # and $5 strike increments are the conservative subset most likely to have
    # existed and been liquid when the signal fired.
    # Holiday calendars can move the standard Friday expiration to Thursday.
    standard_monthly = (
        frame["expiration_date"].dt.weekday.isin((3, 4))
        & frame["expiration_date"].dt.day.between(15, 21)
    )
    established_increment = np.isclose(frame["strike"] % 5.0, 0.0)
    frame = frame[standard_monthly & established_increment]
    if frame.empty:
        raise ValueError("no standard monthly put contracts available")
    target_expiry = roll_date + pd.Timedelta(days=45)
    expiry = min(
        frame["expiration_date"].unique(),
        key=lambda value: abs(pd.Timestamp(value) - target_expiry),
    )
    same_expiry = frame[frame["expiration_date"] == expiry]

    def nearest(target: float) -> pd.Series:
        return same_expiry.loc[
            (same_expiry["strike"] - target).abs().idxmin()
        ]

    long = nearest(long_moneyness * spot_reference)
    short = nearest(short_moneyness * spot_reference)
    if float(long["strike"]) <= float(short["strike"]):
        raise ValueError("put spread strikes are not ordered")
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
    *,
    long_moneyness: float = 0.95,
    short_moneyness: float = 0.90,
) -> list[SpreadPlan]:
    plans = []
    for roll_date in monthly_roll_dates(spy):
        position = spy.index.get_loc(roll_date)
        if position < 1:
            continue
        spot = float(spy.iloc[position - 1])
        params = {
            "underlying_symbols": "SPY",
            "status": "inactive",
            "type": "put",
            "expiration_date_gte": (
                roll_date + pd.Timedelta(days=35)
            ).date().isoformat(),
            "expiration_date_lte": (
                roll_date + pd.Timedelta(days=55)
            ).date().isoformat(),
            "strike_price_gte": round(
                spot * (short_moneyness - 0.03), 2
            ),
            "strike_price_lte": round(
                spot * (long_moneyness + 0.03), 2
            ),
            "limit": 10_000,
        }
        payload = client._get(
            client.trading_base, "/v2/options/contracts", params
        )
        contracts = payload.get("option_contracts") or []
        try:
            plan = select_put_spread(
                contracts,
                roll_date=roll_date,
                spot_reference=spot,
                long_moneyness=long_moneyness,
                short_moneyness=short_moneyness,
            )
        except ValueError as exc:
            raise ValueError(f"{roll_date.date()}: {exc}") from exc
        plans.append(plan)
    return plans


def fetch_daily_bars(
    client: AlpacaClient,
    symbols: list[str],
    *,
    start: str,
    end: str,
) -> pd.DataFrame:
    rows = []
    for offset in range(0, len(symbols), 100):
        batch = symbols[offset:offset + 100]
        token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Day",
                "start": start,
                "end": end,
                "limit": 10_000,
            }
            if token:
                params["page_token"] = token
            payload = client._get(
                client.data_base, "/v1beta1/options/bars", params
            )
            for symbol, bars in (payload.get("bars") or {}).items():
                for bar in bars:
                    rows.append({"symbol": symbol, **bar})
            token = payload.get("next_page_token")
            if not token:
                break
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("Alpaca returned no option bars")
    frame["timestamp"] = pd.to_datetime(frame.pop("t"), utc=True)
    return frame.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def refresh_cache(
    spy: pd.Series,
    *,
    long_moneyness: float = 0.95,
    short_moneyness: float = 0.90,
    plan_path: Path = PLAN_PATH,
    bars_path: Path = BARS_PATH,
) -> tuple[list[SpreadPlan], pd.DataFrame]:
    OPTION_DIR.mkdir(parents=True, exist_ok=True)
    client = AlpacaClient()
    plans = fetch_contract_plan(
        client,
        spy,
        long_moneyness=long_moneyness,
        short_moneyness=short_moneyness,
    )
    plan_path.write_text(
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
    bars.to_parquet(bars_path, index=False)
    return plans, bars


def load_cache(
    *,
    plan_path: Path = PLAN_PATH,
    bars_path: Path = BARS_PATH,
) -> tuple[list[SpreadPlan], pd.DataFrame]:
    if not plan_path.exists() or not bars_path.exists():
        raise FileNotFoundError(
            "option cache missing; run with --refresh"
        )
    plans = [
        SpreadPlan(**row) for row in json.loads(plan_path.read_text())
    ]
    return plans, pd.read_parquet(bars_path)


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
        for column in ("o", "c", "v")
    }


def hedge_signal(
    spy: pd.Series,
    roll_date: pd.Timestamp,
) -> dict[str, bool | float]:
    prior = spy.loc[:roll_date].iloc[:-1]
    returns = prior.pct_change(fill_method=None)
    ma200 = float(prior.tail(200).mean()) if len(prior) >= 200 else np.nan
    vol20 = float(returns.tail(20).std() * np.sqrt(252))
    below_trend = bool(len(prior) >= 200 and prior.iloc[-1] < ma200)
    high_vol = bool(np.isfinite(vol20) and vol20 > 0.20)
    return {
        "below_trend": below_trend,
        "high_vol": high_vol,
        "realized_vol_20d": vol20,
        "spy_prior_close": float(prior.iloc[-1]),
        "spy_ma_200": ma200,
    }


def spread_pnl(
    plans: list[SpreadPlan],
    bars: pd.DataFrame,
    spy: pd.Series,
    *,
    mode: str,
    friction_per_leg: float,
    maximum_loss_per_trade: float | None = None,
    annual_loss_budget: float | None = None,
) -> tuple[pd.Series, list[dict]]:
    if mode not in {
        "always", "conditional", "trend_only", "vol_only", "calm",
    }:
        raise ValueError(f"unknown hedge mode {mode!r}")
    panels = bar_panels(bars)
    dates = spy.loc[START:LAST_COMPLETE_ROLL].index
    pnl = pd.Series(0.0, index=dates)
    logs = []
    loss_budget_used: dict[int, float] = {}
    for number, plan in enumerate(plans):
        roll = pd.Timestamp(plan.roll_date)
        if roll not in dates:
            continue
        signal = hedge_signal(spy, roll)
        enabled = {
            "always": True,
            "conditional": signal["below_trend"] or signal["high_vol"],
            "trend_only": signal["below_trend"],
            "vol_only": signal["high_vol"],
            "calm": (
                not signal["below_trend"]
                and signal["realized_vol_20d"] < 0.15
            ),
        }[mode]
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
        entry_candidates = dates[
            (dates >= roll) & (dates <= roll + pd.Timedelta(days=7))
        ]
        entry_date = None
        long_open = short_open = np.nan
        for candidate in entry_candidates:
            long_open = panels["o"].get(
                plan.long_symbol, pd.Series(dtype=float)
            ).get(candidate, np.nan)
            short_open = panels["o"].get(
                plan.short_symbol, pd.Series(dtype=float)
            ).get(candidate, np.nan)
            if np.isfinite(long_open) and np.isfinite(short_open):
                entry_date = candidate
                break
        if entry_date is None:
            record["rejected"] = "missing entry bar"
            logs.append(record)
            continue
        width = plan.long_strike - plan.short_strike
        entry_value = float(np.clip(long_open - short_open, 0.0, width))
        if entry_value <= 0:
            record["rejected"] = "non-positive entry debit"
            logs.append(record)
            continue
        maximum_loss = (entry_value + 4 * friction_per_leg) * 100
        year = entry_date.year
        used = loss_budget_used.get(year, 0.0)
        if (
            maximum_loss_per_trade is not None
            and maximum_loss > maximum_loss_per_trade
        ):
            record.update({
                "entry_date": entry_date.date().isoformat(),
                "entry_debit": round(entry_value, 4),
                "maximum_loss_dollars": round(maximum_loss, 2),
                "rejected": "maximum loss exceeds per-trade budget",
            })
            logs.append(record)
            continue
        if (
            annual_loss_budget is not None
            and used + maximum_loss > annual_loss_budget
        ):
            record.update({
                "entry_date": entry_date.date().isoformat(),
                "entry_debit": round(entry_value, 4),
                "maximum_loss_dollars": round(maximum_loss, 2),
                "annual_loss_budget_used": round(used, 2),
                "rejected": "annual loss budget exhausted",
            })
            logs.append(record)
            continue
        loss_budget_used[year] = used + maximum_loss

        active_dates = dates[
            (dates >= entry_date) & (dates <= exit_date)
        ]
        values = pd.Series(index=active_dates, dtype=float)
        for date in active_dates:
            long_close = panels["c"].get(
                plan.long_symbol, pd.Series(dtype=float)
            ).get(
                date, np.nan
            )
            short_close = panels["c"].get(
                plan.short_symbol, pd.Series(dtype=float)
            ).get(
                date, np.nan
            )
            if np.isfinite(long_close) and np.isfinite(short_close):
                values.loc[date] = np.clip(
                    float(long_close - short_close), 0.0, width
                )
        values = values.ffill()
        if values.isna().all():
            record["rejected"] = "no valuation bars"
            logs.append(record)
            continue
        if pd.isna(values.iloc[0]):
            values.iloc[0] = entry_value
        if exit_date == pd.Timestamp(plan.expiration_date):
            spot = float(spy.reindex(dates).ffill().loc[exit_date])
            values.iloc[-1] = (
                max(plan.long_strike - spot, 0.0)
                - max(plan.short_strike - spot, 0.0)
            )
        else:
            long_exit = panels["o"].get(
                plan.long_symbol, pd.Series(dtype=float)
            ).get(
                exit_date, np.nan
            )
            short_exit = panels["o"].get(
                plan.short_symbol, pd.Series(dtype=float)
            ).get(
                exit_date, np.nan
            )
            if np.isfinite(long_exit) and np.isfinite(short_exit):
                values.iloc[-1] = np.clip(
                    float(long_exit - short_exit), 0.0, width
                )
        values = values.ffill()
        trade_pnl = values.diff() * 100.0
        trade_pnl.iloc[0] = (
            (values.iloc[0] - entry_value) * 100.0
            - 2.0 * friction_per_leg * 100.0
        )
        trade_pnl.iloc[-1] -= 2.0 * friction_per_leg * 100.0
        pnl.loc[trade_pnl.index] += trade_pnl
        record.update({
            "entry_date": entry_date.date().isoformat(),
            "entry_delay_sessions": int(
                dates.get_loc(entry_date) - dates.get_loc(roll)
            ),
            "entry_debit": round(entry_value, 4),
            "maximum_loss_dollars": round(maximum_loss, 2),
            "annual_loss_budget_used": round(
                loss_budget_used[year], 2
            ),
            "pnl_dollars": round(float(trade_pnl.sum()), 2),
        })
        logs.append(record)
    return pnl, logs


def add_dollar_pnl(
    portfolio_returns: pd.Series,
    dollar_pnl: pd.Series,
    *,
    starting_equity: float = 10_000.0,
) -> pd.Series:
    aligned = pd.concat({
        "portfolio": portfolio_returns,
        "option_pnl": dollar_pnl,
    }, axis=1, sort=False).dropna()
    equity = float(starting_equity)
    output = []
    for _, row in aligned.iterrows():
        previous = equity
        equity = (
            previous * (1.0 + float(row["portfolio"]))
            + float(row["option_pnl"])
        )
        output.append(equity / previous - 1.0)
    return pd.Series(output, index=aligned.index)


def passes_gate(results: dict, trade_count: int) -> tuple[bool, list[str]]:
    reasons = []
    if trade_count < 36:
        reasons.append(f"only {trade_count} completed spreads; require 36")
    for window in ("design_2024", "heldout_2025_plus"):
        rows = {row["portfolio"]: row for row in results[window]}
        incumbent, candidate = rows["no hedge"], rows["conditional"]
        if candidate["sharpe"] <= incumbent["sharpe"]:
            reasons.append(f"{window}: Sharpe did not improve")
        if candidate["max_dd"] <= incumbent["max_dd"]:
            reasons.append(f"{window}: drawdown did not improve")
        if candidate["cagr"] < 0.80 * incumbent["cagr"]:
            reasons.append(f"{window}: retained less than 80% of CAGR")
    return not reasons, reasons


def budget_diagnostics(
    logs: list[dict],
    *,
    account_equity: float = 10_000.0,
) -> dict:
    completed = [
        row for row in logs
        if row.get("enabled") and "maximum_loss_dollars" in row
    ]
    loss_pcts = [
        float(row["maximum_loss_dollars"]) / account_equity
        for row in completed
    ]
    return {
        "completed_spreads": len(completed),
        "median_maximum_loss_pct": (
            round(float(np.median(loss_pcts)), 4) if loss_pcts else None
        ),
        "maximum_maximum_loss_pct": (
            round(max(loss_pcts), 4) if loss_pcts else None
        ),
        "trades_within_budget": {
            f"{budget:.0%}": sum(value <= budget for value in loss_pcts)
            for budget in (0.01, 0.02, 0.03)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    spy_frame = load_parquet(["SPY"], Path("state/history_deep"))["SPY"]
    spy = norm_index(spy_frame["close"])
    if args.refresh:
        plans, bars = refresh_cache(spy)
        tail_plans, tail_bars = refresh_cache(
            spy,
            long_moneyness=0.90,
            short_moneyness=0.85,
            plan_path=TAIL_PLAN_PATH,
            bars_path=TAIL_BARS_PATH,
        )
    else:
        plans, bars = load_cache()
        tail_plans, tail_bars = load_cache(
            plan_path=TAIL_PLAN_PATH,
            bars_path=TAIL_BARS_PATH,
        )
    fixed_2x = recent_capacity_profiles()["2x"].loc[START:LAST_COMPLETE_ROLL]

    variants = {"no hedge": fixed_2x}
    logs = {}
    for mode in ("always", "conditional", "trend_only", "vol_only"):
        pnl, mode_logs = spread_pnl(
            plans, bars, spy,
            mode=mode, friction_per_leg=0.10,
        )
        variants[mode] = add_dollar_pnl(fixed_2x, pnl)
        logs[mode] = mode_logs
    for mode in ("always", "conditional"):
        pnl, mode_logs = spread_pnl(
            tail_plans,
            tail_bars,
            spy,
            mode=mode,
            friction_per_leg=0.10,
        )
        label = f"{mode} 90/85 tail"
        variants[label] = add_dollar_pnl(fixed_2x, pnl)
        logs[label] = mode_logs
    windows = {
        "full": slice(None),
        "design_2024": slice(None, "2024-12-31"),
        "heldout_2025_plus": slice("2025-01-01", None),
    }
    results = {
        window: [
            returns_summary(returns.loc[selector], name)
            for name, returns in variants.items()
        ]
        for window, selector in windows.items()
    }

    friction_sensitivity = {}
    for friction in (0.05, 0.10, 0.20):
        pnl, _ = spread_pnl(
            plans, bars, spy,
            mode="conditional", friction_per_leg=friction,
        )
        friction_sensitivity[f"{friction:.2f}_per_leg"] = returns_summary(
            add_dollar_pnl(fixed_2x, pnl),
            f"conditional ${friction:.2f}/leg",
        )
    completed = [
        row for row in logs["conditional"]
        if row.get("enabled") and "pnl_dollars" in row
    ]
    passed, reasons = passes_gate(results, len(completed))
    payload = {
        "conclusion": (
            "Add an off-by-default paper shadow for the conditional spread."
            if passed else
            "Do not implement the paper hedge; exact-contract evidence failed "
            "or remains insufficient."
        ),
        "promotion_rule_passed": passed,
        "failure_reasons": reasons,
        "method": {
            "account_equity": 10_000,
            "contracts_per_trade": 1,
            "target_dte": 45,
            "expiration_range_days": [35, 55],
            "long_put_moneyness": 0.95,
            "short_put_moneyness": 0.90,
            "conditional_rule": "SPY below 200DMA or 20d realized vol > 20%",
            "base_friction_per_leg": 0.10,
            "minimum_completed_spreads": 36,
        },
        "contract_plans": len(plans),
        "tail_contract_plans": len(tail_plans),
        "conditional_completed_spreads": len(completed),
        "results": results,
        "friction_sensitivity": friction_sensitivity,
        "account_budget_diagnostics": {
            "conditional_95_90": budget_diagnostics(logs["conditional"]),
            "conditional_90_85": budget_diagnostics(
                logs["conditional 90/85 tail"]
            ),
        },
        "conditional_trades": logs["conditional"],
        "tail_conditional_trades": logs["conditional 90/85 tail"],
        "limitations": [
            "Alpaca option history begins in February 2024.",
            "Daily trade bars are not executable bid/ask quotes.",
            "One contract creates lumpy exposure in a $10k account.",
            "American early assignment is not reconstructed.",
            "The sample contains too few independent crash regimes.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2))

    for window, rows in results.items():
        print(f"\n{window}")
        print(pd.DataFrame(rows)[
            ["portfolio", "cagr", "sharpe", "max_dd"]
        ].to_string(index=False))
    print(
        f"\nconditional completed spreads: {len(completed)}/{len(plans)}"
    )
    print(f"PROMOTION RULE: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"\nWrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
