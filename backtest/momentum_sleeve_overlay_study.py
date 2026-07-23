"""Test volatility de-risking only the cross-sectional momentum sleeve.

The pre-selected policy targets 15% annualized volatility on the normalized
MOM_LS return stream, updates weekly from a lagged 63-session estimate, never
increases exposure, and never cuts below 25% of the fixed sleeve.  A 20%
target is reported only as a sensitivity check.

Promotion rule, set before results: the 15% target must improve portfolio
Sharpe and reduce absolute maximum drawdown by at least 10% in both the
2007-2016 design and 2017+ held-out proxy windows, without reducing Sharpe in
either exact 2020-2022 or 2023+ capacity-adjusted window.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.long_history_stress_study import (
    STRESS_WINDOWS,
    build_long_history_profiles,
    load_french_momentum,
    long_history_common,
    stress_window,
)
from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.short_capacity_study import (
    MOM_ACCOUNT_MULTIPLIER,
    solve_dynamic_equity,
)
from backtest.xsec_data import load

PRESELECTED_TARGET = 0.15


def weekly_de_risk_scale(
    momentum_returns: pd.Series,
    target_vol: float,
    *,
    lookback: int = 63,
    min_scale: float = 0.25,
    rebalance_sessions: int = 5,
) -> pd.Series:
    if target_vol <= 0:
        raise ValueError("target_vol must be positive")
    if not 0 < min_scale <= 1:
        raise ValueError("min_scale must be in (0, 1]")
    observed = (
        momentum_returns.rolling(
            lookback, min_periods=max(20, lookback // 2)
        ).std()
        * np.sqrt(TD)
    )
    desired = (target_vol / observed).clip(min_scale, 1.0).shift(1)
    weekly = pd.Series(np.nan, index=momentum_returns.index)
    weekly.iloc[::rebalance_sessions] = desired.iloc[::rebalance_sessions]
    return weekly.ffill().fillna(1.0)


def profile_with_overlay(
    common: pd.Series,
    momentum: pd.Series,
    short_gross: pd.Series,
    *,
    profile: str,
    target_vol: float | None,
    cost_bps: float = 5.0,
) -> tuple[pd.Series, pd.Series]:
    multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
    aligned = pd.concat({
        "common": common * (2.0 if profile == "2x" else 1.0),
        "momentum": momentum,
        "short_gross": short_gross,
    }, axis=1, sort=False).dropna()
    scale = (
        weekly_de_risk_scale(aligned["momentum"], target_vol)
        if target_vol is not None
        else pd.Series(1.0, index=aligned.index)
    )
    financing = MARGIN_RATE / TD if profile == "2x" else 0.0
    overlay_cost = (
        scale.diff().abs().fillna(0.0)
        * multiplier
        * cost_bps
        / 10_000
    )
    returns = (
        aligned["common"]
        + multiplier * scale * aligned["momentum"]
        - multiplier * scale * aligned["short_gross"] * SHORT_BORROW / TD
        - financing
        - overlay_cost
    )
    return returns, scale


def recent_components() -> dict[str, tuple[pd.Series, pd.Series, pd.Series]]:
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
    output = {}
    for profile in ("base", "2x"):
        _, capacity = solve_dynamic_equity(
            close,
            volume,
            common,
            profile=profile,
            selection="ranked",
            short_n=20,
        )
        output[profile] = (common, capacity.returns, capacity.short_gross)
    return output


def long_history_components() -> dict[
    str, tuple[pd.Series, pd.Series, pd.Series]
]:
    _, calibration, history = build_long_history_profiles()
    french = load_french_momentum()
    output = {}
    for profile in ("base", "2x"):
        multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
        details = calibration[profile]
        normalized_momentum = (
            french * float(details["volatility_scale"]) / multiplier
        )
        normalized_short = pd.Series(
            float(details["average_short_gross"]) / multiplier,
            index=normalized_momentum.index,
        )
        output[profile] = (
            history["common"],
            normalized_momentum,
            normalized_short,
        )
    return output


def build_variants(
    components: dict[str, tuple[pd.Series, pd.Series, pd.Series]],
) -> dict[str, dict[str, tuple[pd.Series, pd.Series]]]:
    output = {}
    for profile, (common, momentum, short_gross) in components.items():
        output[profile] = {
            "fixed sleeve": profile_with_overlay(
                common, momentum, short_gross,
                profile=profile, target_vol=None,
            ),
            "15% sleeve vol target": profile_with_overlay(
                common, momentum, short_gross,
                profile=profile, target_vol=0.15,
            ),
            "20% sleeve vol target": profile_with_overlay(
                common, momentum, short_gross,
                profile=profile, target_vol=0.20,
            ),
        }
    return output


def summarize(
    variants: dict[str, dict[str, tuple[pd.Series, pd.Series]]],
    windows: dict[str, slice],
) -> dict:
    result = {}
    for profile, profile_variants in variants.items():
        result[profile] = {}
        for window, selector in windows.items():
            rows = []
            for name, (returns, scale) in profile_variants.items():
                row = returns_summary(returns.loc[selector], name)
                selected_scale = scale.reindex(returns.loc[selector].index)
                row["average_sleeve_scale"] = round(
                    float(selected_scale.mean()), 3
                )
                row["pct_sessions_de_risked"] = round(
                    float((selected_scale < 0.999).mean()), 4
                )
                rows.append(row)
            result[profile][window] = rows
    return result


def row_map(results: dict, profile: str, window: str) -> dict:
    return {
        row["portfolio"]: row for row in results[profile][window]
    }


def promotion_decision(long_results: dict, recent_results: dict) -> tuple[
    bool, list[str]
]:
    reasons = []
    candidate = "15% sleeve vol target"
    fixed = "fixed sleeve"
    for window in ("design_2007_2016", "heldout_2017_plus"):
        rows = row_map(long_results, "base", window)
        if rows[candidate]["sharpe"] <= rows[fixed]["sharpe"]:
            reasons.append(f"{window}: Sharpe did not improve")
        if abs(rows[candidate]["max_dd"]) > 0.90 * abs(rows[fixed]["max_dd"]):
            reasons.append(f"{window}: drawdown reduction was below 10%")
    for window in ("early_2020_2022", "heldout_2023_plus"):
        rows = row_map(recent_results, "base", window)
        if rows[candidate]["sharpe"] < rows[fixed]["sharpe"]:
            reasons.append(f"{window}: exact Sharpe regressed")
    return not reasons, reasons


def main() -> None:
    recent_variants = build_variants(recent_components())
    long_variants = build_variants(long_history_components())
    recent = summarize(
        recent_variants,
        {
            "full": slice(None),
            "early_2020_2022": slice(None, "2022-12-31"),
            "heldout_2023_plus": slice("2023-01-01", None),
        },
    )
    long_history = summarize(
        long_variants,
        {
            "full": slice(None),
            "design_2007_2016": slice(None, "2016-12-31"),
            "heldout_2017_plus": slice("2017-01-01", None),
        },
    )
    passed, reasons = promotion_decision(long_history, recent)
    stress = {}
    for profile, variants in long_variants.items():
        stress[profile] = {
            name: [
                stress_window(returns, start, end, label)
                for label, (start, end) in STRESS_WINDOWS.items()
            ]
            for name, (returns, _) in variants.items()
            if name in {"fixed sleeve", "15% sleeve vol target"}
        }
    payload = {
        "conclusion": (
            "Promote the 15% MOM_LS volatility target to paper shadow."
            if passed else
            "Keep the MOM_LS sleeve fixed; its volatility overlay failed."
        ),
        "promotion_rule_passed": passed,
        "failure_reasons": reasons,
        "preselected_target": PRESELECTED_TARGET,
        "method": {
            "lookback_sessions": 63,
            "rebalance_sessions": 5,
            "minimum_sleeve_scale": 0.25,
            "maximum_sleeve_scale": 1.0,
            "overlay_cost_bps": 5.0,
        },
        "recent_exact": recent,
        "long_history_proxy": long_history,
        "stress_windows": stress,
        "limitations": [
            "The pre-2020 stock momentum return remains an academic proxy.",
            "Historical borrow availability and realized slippage are unknown.",
            "Volatility scaling reacts after shocks and cannot prevent gaps.",
            "A live sleeve-level scale is not implemented.",
        ],
    }
    out = Path("reports/momentum_sleeve_overlay_study.json")
    out.write_text(json.dumps(payload, indent=2))

    for title, results in (("RECENT", recent), ("LONG", long_history)):
        for profile, windows in results.items():
            for window, rows in windows.items():
                print(f"\n{title} {profile} — {window}")
                print(pd.DataFrame(rows)[[
                    "portfolio", "cagr", "sharpe", "max_dd",
                    "average_sleeve_scale",
                ]].to_string(index=False))
    print(f"\nPROMOTION RULE: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
