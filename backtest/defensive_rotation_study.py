"""Test defensive uses for the trend sleeve's otherwise idle capital.

The deployed 20% trend sleeve owns SPY above its 200-day moving average and
cash otherwise.  This study keeps that risk-on rule fixed and compares several
point-in-time risk-off holdings.

The pre-selected candidate is 12-month dual momentum: while SPY is below its
moving average, own the strongest of GLD, TLT, and IEF only when its trailing
12-month return is positive; otherwise hold cash.  Fixed fallbacks and a
gold/Treasury blend are diagnostics, not additional candidates to select from.

Promotion rule, specified before results: the pre-selected candidate must
improve Sharpe and maximum drawdown versus cash in both 2007-2016 and 2017+
long-history windows, and must not reduce Sharpe in the 2023+ exact-production
window.  All variants pay 8 bp per unit of sleeve turnover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest.long_history_stress_study import (
    STRESS_WINDOWS,
    build_long_history_profiles,
    long_history_common,
    stress_window,
)
from backtest.production_portfolio import (
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from engine.tiingo import load_parquet

ASSET_HISTORY = Path("state/history_assets")
DEEP_HISTORY = Path("state/history_deep")
VARIANTS = (
    "cash",
    "IEF",
    "TLT",
    "GLD",
    "50/50 GLD-TLT",
    "12m dual momentum",
)
PRESELECTED = "12m dual momentum"


def defensive_trend_stream(
    close: pd.DataFrame,
    variant: str,
    *,
    cost_bps: float = 8.0,
) -> pd.Series:
    """Return a no-lookahead SPY trend stream with a risk-off fallback."""
    required = {"SPY", "IEF", "TLT", "GLD"}
    missing = sorted(required - set(close))
    if missing:
        raise ValueError(f"missing defensive rotation prices: {missing}")
    if variant not in VARIANTS:
        raise ValueError(f"unknown defensive variant {variant!r}")

    prices = close[list(sorted(required))].copy()
    returns = prices.pct_change(fill_method=None)
    prior = prices.shift(1)
    risk_on = prior["SPY"] > prior["SPY"].rolling(200).mean()
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    weights.loc[risk_on, "SPY"] = 1.0
    risk_off = ~risk_on

    if variant in {"IEF", "TLT", "GLD"}:
        weights.loc[risk_off, variant] = 1.0
    elif variant == "50/50 GLD-TLT":
        weights.loc[risk_off, ["GLD", "TLT"]] = 0.5
    elif variant == PRESELECTED:
        momentum = prior[["GLD", "TLT", "IEF"]] / prior[
            ["GLD", "TLT", "IEF"]
        ].shift(252) - 1.0
        # Pre-history rows have no eligible winner and must remain cash.
        winner = momentum.fillna(float("-inf")).idxmax(axis=1)
        positive = momentum.max(axis=1).gt(0) & risk_off
        for symbol in ("GLD", "TLT", "IEF"):
            weights.loc[positive & winner.eq(symbol), symbol] = 1.0

    gross = (weights * returns).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    ready = prior["SPY"].rolling(200).count().eq(200)
    if variant == PRESELECTED:
        ready &= prior[["GLD", "TLT", "IEF"]].shift(252).notna().all(axis=1)
    return (gross - turnover * cost_bps / 10_000).where(ready).dropna()


def load_defensive_prices() -> pd.DataFrame:
    frames = load_parquet(["IEF", "TLT", "GLD"], ASSET_HISTORY)
    missing = sorted({"IEF", "TLT", "GLD"} - set(frames))
    if missing:
        raise FileNotFoundError(f"missing defensive histories: {missing}")
    spy = load_parquet(["SPY"], DEEP_HISTORY)["SPY"]
    series = {
        symbol: norm_index(frame["close"])
        for symbol, frame in {**frames, "SPY": spy}.items()
    }
    return pd.DataFrame(series)


def portfolio_variants(
    trend_variants: dict[str, pd.Series],
    *,
    long_history: bool,
) -> dict[str, pd.Series]:
    if long_history:
        common = long_history_common()
        base_profiles, _, _ = build_long_history_profiles()
        incumbent_trend = common["trend"]
        output = {}
        for name, trend in trend_variants.items():
            adjustment = 0.20 * (
                trend.reindex(base_profiles["base"].index)
                - incumbent_trend.reindex(base_profiles["base"].index)
            )
            output[name] = (base_profiles["base"] + adjustment).dropna()
        return output

    streams = build_streams()
    output = {}
    for name, trend in trend_variants.items():
        aligned = pd.DataFrame({
            "spy": streams["spy"],
            "tsmom": streams["tsmom"],
            "mom_ls": streams["mom_ls"],
            "trend": trend,
        }).dropna()
        output[name] = (
            0.40 * aligned["spy"]
            + 0.25 * aligned["tsmom"]
            + 0.30 * aligned["mom_ls"]
            + 0.20 * aligned["trend"]
            - 0.15 * SHORT_BORROW / TD
        )
    return output


def summarize_windows(
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


def passes_promotion_rule(results: dict) -> tuple[bool, list[str]]:
    reasons = []
    for section, window in (
        ("long_history", "design_2007_2016"),
        ("long_history", "heldout_2017_plus"),
    ):
        rows = {row["portfolio"]: row for row in results[section][window]}
        candidate, incumbent = rows[PRESELECTED], rows["cash"]
        if candidate["sharpe"] <= incumbent["sharpe"]:
            reasons.append(f"{window}: Sharpe did not improve")
        if candidate["max_dd"] <= incumbent["max_dd"]:
            reasons.append(f"{window}: maximum drawdown did not improve")
    recent = {
        row["portfolio"]: row
        for row in results["recent"]["heldout_2023_plus"]
    }
    if recent[PRESELECTED]["sharpe"] < recent["cash"]["sharpe"]:
        reasons.append("heldout_2023_plus: Sharpe regressed")
    return not reasons, reasons


def main() -> None:
    prices = load_defensive_prices()
    streams = {
        variant: defensive_trend_stream(prices, variant)
        for variant in VARIANTS
    }
    recent = portfolio_variants(streams, long_history=False)
    long_history = portfolio_variants(streams, long_history=True)
    results = {
        "recent": summarize_windows(
            recent,
            {
                "full": slice(None),
                "early_2020_2022": slice(None, "2022-12-31"),
                "heldout_2023_plus": slice("2023-01-01", None),
            },
        ),
        "long_history": summarize_windows(
            long_history,
            {
                "full": slice(None),
                "design_2007_2016": slice(None, "2016-12-31"),
                "heldout_2017_plus": slice("2017-01-01", None),
            },
        ),
    }
    passed, reasons = passes_promotion_rule(results)
    candidate_long = long_history[PRESELECTED]
    incumbent_long = long_history["cash"]
    payload = {
        "conclusion": (
            "Promote the pre-selected defensive rotation to a paper shadow."
            if passed else
            "Keep the trend sleeve's risk-off allocation in cash."
        ),
        "preselected_candidate": PRESELECTED,
        "promotion_rule_passed": passed,
        "failure_reasons": reasons,
        "cost_bps_per_unit_turnover": 8.0,
        "results": results,
        "long_history_stress": {
            name: [
                stress_window(series, start, end, label)
                for label, (start, end) in STRESS_WINDOWS.items()
            ]
            for name, series in (
                ("cash", incumbent_long),
                (PRESELECTED, candidate_long),
            )
        },
        "limitations": [
            "ETF total-return histories begin at different dates.",
            "A relative-momentum winner can change abruptly and incur slippage.",
            "Treasury/equity correlation can turn positive during inflation shocks.",
            "The long-history stock momentum sleeve remains an academic proxy.",
        ],
    }
    out = Path("reports/defensive_rotation_study.json")
    out.write_text(json.dumps(payload, indent=2))

    for section in ("recent", "long_history"):
        for window, rows in results[section].items():
            print(f"\n{section.upper()} — {window}")
            print(
                pd.DataFrame(rows)[
                    ["portfolio", "cagr", "sharpe", "max_dd"]
                ].to_string(index=False)
            )
    print(f"\nPROMOTION RULE: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
