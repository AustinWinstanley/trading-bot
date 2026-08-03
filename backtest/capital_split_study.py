"""Combined risk across the separate $10k base and 2x paper accounts."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

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
from backtest.return_uncertainty_study import (
    TD,
    bootstrap_outcomes,
    severe_delisting_drags,
)
from backtest.short_capacity_study import solve_dynamic_equity
from backtest.xsec_data import load


def blend_profiles(
    base: pd.Series,
    two_x: pd.Series,
    *,
    two_x_weight: float,
) -> pd.Series:
    if not 0 <= two_x_weight <= 1:
        raise ValueError("two_x_weight must be in [0, 1]")
    aligned = pd.concat(
        {"base": base, "2x": two_x}, axis=1, sort=False
    ).dropna()
    return (
        (1.0 - two_x_weight) * aligned["base"]
        + two_x_weight * aligned["2x"]
    )


def recent_capacity_profiles() -> dict[str, pd.Series]:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]
    production = build_streams()
    common = (
        0.40 * production["spy"]
        + 0.25 * production["tsmom"]
        + 0.20 * production["trend"]
    )
    profiles = {}
    for profile in ("base", "2x"):
        profiles[profile], _ = solve_dynamic_equity(
            close,
            volume,
            common,
            profile=profile,
            selection="ranked",
            short_n=20,
        )
    return profiles


def split_variants(profiles: dict[str, pd.Series]) -> dict[str, pd.Series]:
    variants = {}
    for two_x_weight in (0.0, 0.25, 0.50, 0.75, 1.0):
        label = f"{two_x_weight:.0%} 2x / {1-two_x_weight:.0%} base"
        variants[label] = blend_profiles(
            profiles["base"],
            profiles["2x"],
            two_x_weight=two_x_weight,
        )
    return variants


def window_results(
    variants: dict[str, pd.Series],
    windows: dict[str, slice],
) -> dict[str, list[dict]]:
    output = {}
    for window, selector in windows.items():
        rows = []
        for label, returns in variants.items():
            row = returns_summary(returns.loc[selector], label)
            two_x_weight = float(label.split("%", 1)[0]) / 100.0
            row["nominal_combined_leverage"] = round(
                1.0 + two_x_weight, 2
            )
            rows.append(row)
        output[window] = rows
    return output


def main() -> None:
    recent_profiles = recent_capacity_profiles()
    long_profiles, _, _ = build_long_history_profiles()
    recent = split_variants(recent_profiles)
    long_history = split_variants(long_profiles)

    recent_results = window_results(
        recent,
        {
            "full": slice(None),
            "early_2020_2022": slice(None, "2022-12-31"),
            "heldout_2023_plus": slice("2023-01-01", None),
        },
    )
    long_results = window_results(
        long_history,
        {
            "full": slice(None),
            "design_2007_2016": slice(None, "2016-12-31"),
            "heldout_2017_plus": slice("2017-01-01", None),
        },
    )

    equal_recent = recent["50% 2x / 50% base"]
    drags = severe_delisting_drags(Path("reports/survivorship_study.json"))
    equal_severe = blend_profiles(
        recent_profiles["base"] - drags["base"] / TD,
        recent_profiles["2x"] - drags["2x"] / TD,
        two_x_weight=0.50,
    )
    uncertainty = {
        f"{years}y": bootstrap_outcomes(equal_severe, years=years)
        for years in (1, 3)
    }
    equal_long = long_history["50% 2x / 50% base"]
    stress = [
        stress_window(equal_long, start, end, label)
        for label, (start, end) in STRESS_WINDOWS.items()
    ]

    payload = {
        "conclusion": (
            "The current equal-dollar accounts behave like an approximately "
            "1.5x combined portfolio, not two diversified strategies. Judge "
            "risk and capital decisions on the combined results."
        ),
        "current_capital_split": {
            "base_dollars": 10_000,
            "2x_dollars": 10_000,
            "two_x_weight": 0.50,
            "nominal_combined_leverage": 1.50,
        },
        "limitations": [
            "Both accounts trade the same sleeves, so operational correlation is near one.",
            "Recent results retain stock-universe and borrow-history limitations.",
            "Long-history results use the academic momentum proxy.",
            "The severe bootstrap remains conditional on 2020-2026 regimes.",
        ],
        "recent_capacity_adjusted": recent_results,
        "long_history_proxy": long_results,
        "equal_split_severe_bootstrap": uncertainty,
        "equal_split_long_history_stress": stress,
    }
    out = Path("reports/capital_split_study.json")
    out.write_text(json.dumps(payload, indent=2))

    for title, results in (
        ("RECENT CAPACITY-ADJUSTED", recent_results),
        ("LONG-HISTORY PROXY", long_results),
    ):
        for window, rows in results.items():
            print(f"\n{title} — {window}")
            print(
                pd.DataFrame(rows)[
                    [
                        "portfolio", "cagr", "sharpe", "max_dd",
                        "nominal_combined_leverage",
                    ]
                ].to_string(index=False)
            )
    print("\nEQUAL-SPLIT SEVERE BOOTSTRAP")
    for horizon, result in uncertainty.items():
        print(
            f"  {horizon}: CAGR p05/50/95 "
            f"{result['cagr']['p05']:.1%}/{result['cagr']['p50']:.1%}/"
            f"{result['cagr']['p95']:.1%}; "
            f"P(loss) {result['probability_negative_cagr']:.1%}"
        )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
