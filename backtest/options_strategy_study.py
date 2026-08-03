"""Broader options benchmark screen against the deployed portfolio.

The earlier VRP study tested only cash-secured put writing and a fully covered
call benchmark. This study evaluates structurally different strategies:

* PPUT: monthly 5% OTM protective put;
* CLL: 95-110 collar;
* CLLZ: zero-cost put-spread collar;
* CNDR/BFLY: defined-risk short-volatility spreads;
* BXMH/BXMD: partial or lower-delta covered calls;
* PUTY/WPUT: OTM and weekly put writing.

The pre-selected candidate is CLLZ because it explicitly finances downside
protection by capping some upside rather than relying on persistent premium
selling. Promotion rule, fixed before results: replacing half of the 40% SPY
core with CLLZ must improve Sharpe and maximum drawdown in 2007-2016,
2017+, 2020-2022, and 2023+, while retaining at least 85% of incumbent CAGR in
each window. Other strategies are exploratory and cannot be selected here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest.capital_split_study import recent_capacity_profiles
from backtest.long_history_stress_study import (
    STRESS_WINDOWS,
    build_long_history_profiles,
    stress_window,
)
from backtest.production_portfolio import (
    build_streams,
    norm_index,
    returns_summary,
)
from engine.cboe import series

OPTION_BENCHMARKS = {
    "PPUT": "5% OTM protective put",
    "CLL": "95-110 collar",
    "CLLZ": "zero-cost put-spread collar",
    "CNDR": "iron condor",
    "BFLY": "iron butterfly",
    "BXMH": "half covered call",
    "BXMD": "30-delta covered call",
    "PUTY": "2% OTM put-write",
    "WPUT": "weekly put-write",
}
PRESELECTED = "CLLZ"
REPLACEMENT_WEIGHT = 0.20


def replace_spy_core(
    incumbent: pd.Series,
    spy_returns: pd.Series,
    option_returns: pd.Series,
    *,
    weight: float = REPLACEMENT_WEIGHT,
) -> pd.Series:
    if not 0 <= weight <= 0.40:
        raise ValueError("replacement weight must be in [0, 0.40]")
    aligned = pd.concat({
        "incumbent": incumbent,
        "spy": spy_returns,
        "option": option_returns,
    }, axis=1, sort=False).dropna()
    return (
        aligned["incumbent"]
        + weight * (aligned["option"] - aligned["spy"])
    )


def load_option_returns() -> dict[str, pd.Series]:
    return {
        symbol: norm_index(series(symbol)).pct_change(fill_method=None)
        for symbol in OPTION_BENCHMARKS
    }


def portfolio_variants(
    incumbent: pd.Series,
    spy_returns: pd.Series,
    options: dict[str, pd.Series],
) -> dict[str, pd.Series]:
    variants = {"incumbent": incumbent}
    for symbol, returns in options.items():
        variants[f"20% {symbol}"] = replace_spy_core(
            incumbent, spy_returns, returns
        )
    return variants


def result_windows(
    variants: dict[str, pd.Series],
    windows: dict[str, slice],
) -> dict[str, list[dict]]:
    return {
        window: [
            returns_summary(returns.loc[selector], name)
            for name, returns in variants.items()
        ]
        for window, selector in windows.items()
    }


def promotion_decision(long_results: dict, recent_results: dict) -> tuple[
    bool, list[str]
]:
    reasons = []
    candidate = f"20% {PRESELECTED}"
    for results, windows in (
        (long_results, ("design_2007_2016", "heldout_2017_plus")),
        (recent_results, ("early_2020_2022", "heldout_2023_plus")),
    ):
        for window in windows:
            rows = {row["portfolio"]: row for row in results[window]}
            incumbent, proposed = rows["incumbent"], rows[candidate]
            if proposed["sharpe"] <= incumbent["sharpe"]:
                reasons.append(f"{window}: Sharpe did not improve")
            if proposed["max_dd"] <= incumbent["max_dd"]:
                reasons.append(f"{window}: maximum drawdown did not improve")
            if proposed["cagr"] < 0.85 * incumbent["cagr"]:
                reasons.append(f"{window}: retained less than 85% of CAGR")
    return not reasons, reasons


def main() -> None:
    option_returns = load_option_returns()

    long_profiles, _, long_components = build_long_history_profiles()
    long_variants = portfolio_variants(
        long_profiles["base"],
        long_components["spy"],
        option_returns,
    )
    long_results = result_windows(
        long_variants,
        {
            "full": slice(None),
            "design_2007_2016": slice(None, "2016-12-31"),
            "heldout_2017_plus": slice("2017-01-01", None),
        },
    )

    recent_profiles = recent_capacity_profiles()
    recent_streams = build_streams()
    recent_variants = portfolio_variants(
        recent_profiles["base"],
        recent_streams["spy"],
        option_returns,
    )
    recent_results = result_windows(
        recent_variants,
        {
            "full": slice(None),
            "early_2020_2022": slice(None, "2022-12-31"),
            "heldout_2023_plus": slice("2023-01-01", None),
        },
    )

    standalone = [
        returns_summary(
            returns.loc["2007":],
            f"{symbol} — {OPTION_BENCHMARKS[symbol]}",
        )
        for symbol, returns in option_returns.items()
    ]
    passed, reasons = promotion_decision(long_results, recent_results)
    stress = {
        name: [
            stress_window(returns, start, end, label)
            for label, (start, end) in STRESS_WINDOWS.items()
        ]
        for name, returns in long_variants.items()
        if name in {"incumbent", f"20% {PRESELECTED}"}
    }
    payload = {
        "conclusion": (
            "Promote a 20% CLLZ replacement to contract-level validation."
            if passed else
            "Do not promote CLLZ; continue contract-level research only for "
            "strategies with a distinct, pre-specified hypothesis."
        ),
        "preselected_candidate": PRESELECTED,
        "promotion_rule_passed": passed,
        "failure_reasons": reasons,
        "replacement_weight": REPLACEMENT_WEIGHT,
        "benchmarks": OPTION_BENCHMARKS,
        "standalone_since_2007": standalone,
        "long_history_portfolio": long_results,
        "recent_exact_portfolio": recent_results,
        "stress_windows": stress,
        "limitations": [
            "Cboe indices are hypothetical benchmarks, not Alpaca fills.",
            "SPX index options differ from tradeable SPY option contracts.",
            "Index results omit our exact contract spreads and execution latency.",
            "The long-history MOM_LS component remains an academic proxy.",
            "Taxes, assignment handling, and broker margin can differ materially.",
        ],
    }
    out = Path("reports/options_strategy_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print("STANDALONE")
    print(pd.DataFrame(standalone)[
        ["portfolio", "cagr", "sharpe", "max_dd"]
    ].to_string(index=False))
    for title, results in (
        ("LONG-HISTORY PORTFOLIO", long_results),
        ("RECENT EXACT PORTFOLIO", recent_results),
    ):
        for window, rows in results.items():
            print(f"\n{title} — {window}")
            print(pd.DataFrame(rows)[
                ["portfolio", "cagr", "sharpe", "max_dd"]
            ].to_string(index=False))
    print(f"\nPROMOTION RULE: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
