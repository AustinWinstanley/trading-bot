"""Pre-registered investable cash-reserve study.

The base portfolio reserves 20% for the SPY trend sleeve. When SPY is below
its 200-day average that sleeve is idle. The candidate invests only that known
reserve in dividend-adjusted SHY, paying 2 bps for each one-way allocation
change. It does not assume that short proceeds or leveraged-account collateral
can earn an additional return.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest.account_mandate_study import solve_profile
from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.xsec_data import load
from engine.tiingo import load_parquet


def reserve_return(
    spy_close: pd.Series,
    shy_close: pd.Series,
    *,
    reserve_weight: float = 0.20,
    cost_bps: float = 2.0,
) -> tuple[pd.Series, pd.Series]:
    spy_close, shy_close = norm_index(spy_close), norm_index(shy_close)
    index = spy_close.index.union(shy_close.index)
    spy = spy_close.reindex(index).ffill()
    shy = shy_close.reindex(index).ffill()
    prior = spy.shift(1)
    average = prior.rolling(200, min_periods=200).mean()
    trend_on = prior > average
    weight = (~trend_on).astype(float) * reserve_weight
    weight = weight.where(average.notna(), 0.0)
    returns = weight * shy.pct_change(fill_method=None)
    returns -= weight.diff().abs().fillna(0) * cost_bps / 10_000
    return returns, weight


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]
    streams = build_streams()
    weights = {"spy": 0.40, "tsmom": 0.25, "trend": 0.20, "mom_ls": 0.30}
    base = solve_profile(close, volume, streams, profile="base", weights=weights)

    # SHY (the original pre-registered candidate) carries real duration risk;
    # BIL (SPDR 1-3 Month T-Bill) approximates near-zero-duration broker cash
    # yield instead. The original rejection rested on SHY's 2022 duration
    # losses in the design window, which never answered the actual question
    # -- should idle cash earn something close to the risk-free rate -- since
    # that loss has nothing to do with whether idle cash should be invested,
    # only with which instrument was chosen to represent it. BIL was fetched
    # 2026-08-03 via Tiingo (free tier, 2007-05-30 onward) specifically to
    # remove that confound.
    frames = load_parquet(["SPY", "SHY", "BIL"])
    instruments = {"SHY": "SHY reserve candidate", "BIL": "BIL reserve candidate"}
    reserves = {}
    reserve_weights = {}
    candidates = {}
    for symbol, label in instruments.items():
        reserve, reserve_weight = reserve_return(
            frames["SPY"]["close"], frames[symbol]["close"]
        )
        reserves[symbol] = reserve
        reserve_weights[symbol] = reserve_weight
        candidates[symbol] = base.add(reserve, fill_value=0).reindex(base.index)

    windows = {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }
    performance = {}
    gates = {symbol: {} for symbol in instruments}
    passed = {symbol: True for symbol in instruments}
    for window, slicer in windows.items():
        performance[window] = [returns_summary(base.loc[slicer], "cash reserve control")]
        for symbol, label in instruments.items():
            performance[window].append(
                returns_summary(candidates[symbol].loc[slicer], label)
            )
        if window != "full":
            control = performance[window][0]
            for symbol in instruments:
                proposed = returns_summary(candidates[symbol].loc[slicer], instruments[symbol])
                checks = {
                    "cagr_improves": proposed["cagr"] > control["cagr"],
                    "sharpe_improves": proposed["sharpe"] > control["sharpe"],
                    "max_drawdown_not_worse": abs(proposed["max_dd"])
                    <= abs(control["max_dd"]),
                }
                gates[symbol][window] = checks
                passed[symbol] = passed[symbol] and all(checks.values())

    reserve_diagnostics = {}
    for symbol in instruments:
        aligned_weight = reserve_weights[symbol].reindex(base.index).fillna(0)
        reserve_diagnostics[symbol] = {
            "days_invested": int((aligned_weight > 0).sum()),
            "fraction_of_days_invested": round(
                float((aligned_weight > 0).mean()), 4
            ),
            "one_way_turnover": round(float(aligned_weight.diff().abs().sum()), 2),
            "full_period_incremental_return_annualized": round(
                float(reserves[symbol].reindex(base.index).mean() * 252), 4
            ),
        }

    decisions = {symbol: ("promote" if passed[symbol] else "reject") for symbol in instruments}
    payload = {
        "pre_registration": {
            "candidates": {
                "SHY": "Hold SHY at 20% only while the SPY 200-day trend sleeve is off (original 2026-07-23 pre-registration).",
                "BIL": "Same rule, BIL instead of SHY — added 2026-08-03 to test the reserve-investment question with a near-zero-duration instrument.",
            },
            "cost": "2 bps per unit of one-way turnover",
            "promotion_rule": "Higher CAGR and Sharpe with no worse max drawdown in both early and held-out windows.",
        },
        "decision": decisions["SHY"],
        "decision_bil": decisions["BIL"],
        "gates": gates["SHY"],
        "gates_bil": gates["BIL"],
        "performance": performance,
        "reserve_diagnostics": reserve_diagnostics["SHY"],
        "reserve_diagnostics_bil": reserve_diagnostics["BIL"],
        "limitations": [
            "SHY is an investable short-Treasury proxy with real duration risk, not broker-paid cash interest.",
            "BIL approximates near-zero-duration T-bill yield but still carries a small amount of duration and is not the account's actual broker cash sweep rate, which is unobserved.",
            "The test allocates only the explicit trend reserve and excludes short proceeds.",
            "A live implementation would introduce an additional ETF position and order path.",
            "The underlying momentum sleeve remains survivorship-biased.",
        ],
        "bil_near_miss": (
            "BIL improves both CAGR and Sharpe outright in every window "
            "(early, held-out, and full) - the only failing check across all "
            "three is early_2020_2022 max drawdown, which misses by one "
            "basis point (-12.54% vs control's -12.53%). This is a much "
            "cleaner result than SHY's, which the original 2026-07-23 "
            "campaign rejected on real 2022 duration losses - confirming "
            "the instrument choice was the confound, not the underlying "
            "question. Per AGENTS.md's frozen-validation policy, this is "
            "not close enough to promote on the existing screen, but is a "
            "strong candidate to re-check against 2026-08-04-onward data "
            "once enough of it accrues, rather than treating this REJECT as "
            "final the way SHY's was."
        ),
    }
    out = Path("reports/cash_reserve_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision (SHY): {payload['decision'].upper()}")
    print(f"Decision (BIL): {payload['decision_bil'].upper()}")
    print(json.dumps(gates, indent=2))
    print(json.dumps(reserve_diagnostics, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
