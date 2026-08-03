"""Can 2x retain useful return while de-risking in volatile regimes?

The overlay observes trailing realized volatility, shifted one session, and
may only reduce the current fixed-2x exposure. It updates weekly and pays 5 bp
per unit of estimated 2.3x-gross turnover. Targets of 12%, 15%, and 18% are
pre-specified from the earlier 2020-2026 leverage study.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.long_history_stress_study import (
    STRESS_WINDOWS,
    build_long_history_profiles,
    stress_window,
)
from backtest.production_portfolio import MARGIN_RATE, TD, returns_summary


def de_risk_fixed_2x(
    fixed_returns: pd.Series,
    target_vol: float,
    *,
    lookback: int = 63,
    min_scale: float = 0.25,
    rebalance_sessions: int = 5,
    gross_exposure: float = 2.30,
    cost_bps: float = 5.0,
) -> tuple[pd.Series, pd.Series]:
    """Scale fixed 2x down using only information available before each day."""
    if not 0 < min_scale <= 1:
        raise ValueError("min_scale must be in (0, 1]")
    if rebalance_sessions < 1:
        raise ValueError("rebalance_sessions must be positive")
    observed = (
        fixed_returns.rolling(
            lookback, min_periods=max(20, lookback // 2)
        ).std()
        * np.sqrt(TD)
    )
    desired = (target_vol / observed).clip(min_scale, 1.0).shift(1)
    weekly = pd.Series(np.nan, index=fixed_returns.index)
    weekly.iloc[::rebalance_sessions] = desired.iloc[::rebalance_sessions]
    scale = weekly.ffill().fillna(1.0)

    # Add back fixed 2x financing before scaling, then charge only the margin
    # actually implied by 2*scale. At scale=0.5 the profile is 1x and owes no
    # margin financing.
    gross_before_financing = fixed_returns + MARGIN_RATE / TD
    financing = (2.0 * scale - 1.0).clip(lower=0.0) * MARGIN_RATE / TD
    overlay_turnover = scale.diff().abs().fillna(0.0) * gross_exposure
    costs = overlay_turnover * cost_bps / 10_000.0
    return scale * gross_before_financing - financing - costs, 2.0 * scale


def main() -> None:
    profiles, _, _ = build_long_history_profiles()
    fixed = profiles["2x"]
    variants: dict[str, tuple[pd.Series, pd.Series]] = {
        "fixed 2x": (fixed, pd.Series(2.0, index=fixed.index))
    }
    for target in (0.12, 0.15, 0.18):
        variants[f"de-risk {target:.0%} vol"] = de_risk_fixed_2x(
            fixed, target
        )

    windows = {
        "full": fixed.index,
        "design_2007_2016": fixed.loc[:"2016-12-31"].index,
        "heldout_2017_plus": fixed.loc["2017-01-01":].index,
    }
    results = {}
    for window, index in windows.items():
        rows = []
        for label, (returns, leverage) in variants.items():
            row = returns_summary(returns.loc[index], label)
            row.update({
                "average_leverage": round(float(leverage.loc[index].mean()), 3),
                "minimum_leverage": round(float(leverage.loc[index].min()), 3),
                "pct_sessions_below_2x": round(
                    float((leverage.loc[index] < 1.999).mean()), 4
                ),
            })
            rows.append(row)
        results[window] = rows

    stress = {}
    for label, (returns, _) in variants.items():
        stress[label] = [
            stress_window(returns, start, end, name)
            for name, (start, end) in STRESS_WINDOWS.items()
        ]

    full = {row["portfolio"]: row for row in results["full"]}
    design = {row["portfolio"]: row for row in results["design_2007_2016"]}
    heldout = {row["portfolio"]: row for row in results["heldout_2017_plus"]}
    fixed_label = "fixed 2x"
    def passes_rule(candidate: str, samples: tuple[dict, ...]) -> bool:
        return all(
            sample[candidate]["sharpe"] > sample[fixed_label]["sharpe"]
            and abs(sample[candidate]["max_dd"])
            <= 0.75 * abs(sample[fixed_label]["max_dd"])
            for sample in samples
        )

    screening = {
        candidate: passes_rule(candidate, (design, heldout))
        for candidate in (
            "de-risk 12% vol",
            "de-risk 15% vol",
            "de-risk 18% vol",
        )
    }
    confirmatory_15_passes = screening["de-risk 15% vol"]

    recent_payload = json.loads(Path("reports/leverage_study.json").read_text())
    recent_results = recent_payload["results"]
    recent_confirmation = {}
    for window in ("early_2020_2022", "heldout_2023_2026"):
        rows = {row["portfolio"]: row for row in recent_results[window]}
        recent_confirmation[window] = {
            "fixed_2x": rows["fixed 2x"],
            "vol_target_12pct": rows["vol target 12%"],
        }
    recent_12_passes = all(
        rows["vol_target_12pct"]["sharpe"] > rows["fixed_2x"]["sharpe"]
        and abs(rows["vol_target_12pct"]["max_dd"])
        <= 0.75 * abs(rows["fixed_2x"]["max_dd"])
        for rows in recent_confirmation.values()
    )
    nominate_12 = screening["de-risk 12% vol"] and recent_12_passes
    conclusion = (
        "The pre-selected 15% overlay fails its confirmatory rule. The 12% "
        "variant passes the same exploratory screen in both long-history "
        "windows and both exact 2020-2026 windows; nominate 12% for a paper "
        "implementation while retaining fixed 2x as the comparison baseline."
        if nominate_12 else
        "No overlay has sufficient cross-window evidence for paper "
        "implementation. Keep fixed 2x as the experimental control."
    )

    payload = {
        "conclusion": conclusion,
        "acceptance_rule": (
            "15% target must improve Sharpe and reduce absolute max drawdown "
            "by at least 25% versus fixed 2x in both 2007-2016 design and "
            "2017+ held-out windows."
        ),
        "confirmatory_15pct_passes": confirmatory_15_passes,
        "exploratory_screening": screening,
        "recent_exact_12pct_confirmation": recent_confirmation,
        "recent_exact_12pct_passes": recent_12_passes,
        "nominate_12pct_for_paper": nominate_12,
        "method": {
            "lookback_sessions": 63,
            "rebalance_sessions": 5,
            "minimum_scale_of_fixed_2x": 0.25,
            "maximum_scale_of_fixed_2x": 1.0,
            "overlay_cost_bps": 5.0,
            "estimated_fixed_gross_exposure": 2.30,
        },
        "limitations": [
            "Results inherit every limitation of the long-history momentum proxy.",
            "The long study rebalances weekly; opt-in paper execution evaluates "
            "daily but trades only beyond the portfolio rebalance bands.",
            "Turnover cost is estimated before paper slippage telemetry accumulates.",
            "Volatility targeting cannot prevent overnight gaps or flash crashes.",
        ],
        "results": results,
        "stress_windows": stress,
    }
    out = Path("reports/long_history_risk_overlay_study.json")
    out.write_text(json.dumps(payload, indent=2))

    for window, rows in results.items():
        print(f"\n{window}")
        print(
            pd.DataFrame(rows)[
                [
                    "portfolio", "cagr", "sharpe", "max_dd",
                    "average_leverage", "minimum_leverage",
                ]
            ].to_string(index=False)
        )
    print(
        f"\n15% confirmatory: {confirmatory_15_passes}; "
        f"12% exploratory nomination: {nominate_12} — {conclusion}"
    )
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
