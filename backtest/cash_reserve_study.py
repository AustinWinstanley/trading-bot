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

    frames = load_parquet(["SPY", "SHY"])
    reserve, reserve_weight = reserve_return(
        frames["SPY"]["close"], frames["SHY"]["close"]
    )
    candidate = base.add(reserve, fill_value=0).reindex(base.index)
    windows = {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }
    performance = {}
    gates = {}
    passed = True
    for window, slicer in windows.items():
        performance[window] = [
            returns_summary(base.loc[slicer], "cash reserve control"),
            returns_summary(candidate.loc[slicer], "SHY reserve candidate"),
        ]
        if window != "full":
            control, proposed = performance[window]
            checks = {
                "cagr_improves": proposed["cagr"] > control["cagr"],
                "sharpe_improves": proposed["sharpe"] > control["sharpe"],
                "max_drawdown_not_worse": abs(proposed["max_dd"])
                <= abs(control["max_dd"]),
            }
            gates[window] = checks
            passed = passed and all(checks.values())

    aligned_weight = reserve_weight.reindex(base.index).fillna(0)
    payload = {
        "pre_registration": {
            "candidate": "Hold SHY at 20% only while the SPY 200-day trend sleeve is off.",
            "cost": "2 bps per unit of one-way SHY turnover",
            "promotion_rule": "Higher CAGR and Sharpe with no worse max drawdown in both early and held-out windows.",
        },
        "decision": "promote" if passed else "reject",
        "gates": gates,
        "performance": performance,
        "reserve_diagnostics": {
            "days_invested": int((aligned_weight > 0).sum()),
            "fraction_of_days_invested": round(
                float((aligned_weight > 0).mean()), 4
            ),
            "one_way_turnover": round(float(aligned_weight.diff().abs().sum()), 2),
            "full_period_incremental_return_annualized": round(
                float(reserve.reindex(base.index).mean() * 252), 4
            ),
        },
        "limitations": [
            "SHY is an investable short-Treasury proxy, not broker-paid cash interest.",
            "The test allocates only the explicit trend reserve and excludes short proceeds.",
            "A live implementation would introduce an additional ETF position and order path.",
            "The underlying momentum sleeve remains survivorship-biased.",
        ],
    }
    out = Path("reports/cash_reserve_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision: {payload['decision'].upper()}")
    print(json.dumps(gates, indent=2))
    print(json.dumps(payload["reserve_diagnostics"], indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
