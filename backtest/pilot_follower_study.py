"""Can a small pilot account improve a larger follower's MOM_LS entries?

The $5k pilot takes the deployed weekly MOM_LS targets.  The $10k 2x follower
either enters normally, waits one session unconditionally, or enters only when
the pilot position has a positive directional close-to-close markout after a
fixed delay.  The follower always exits when the pilot signal exits.

Pre-selected candidate: one-session positive confirmation.
Promotion rule, fixed before results:

* higher Sharpe and smaller max drawdown than fixed 2x in both 2020-2022 and
  2023+;
* higher Sharpe than an unconditional one-session delay in both windows, so
  any benefit is confirmation rather than merely trading later; and
* no more than a 20% relative CAGR reduction versus fixed 2x in either window.

This first study tests the architecture on the existing MOM_LS sleeve.  It
does not assume that a paper fill contains information unavailable in prices.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.return_uncertainty_study import severe_delisting_drags
from backtest.short_capacity_study import (
    STARTING_EQUITY,
    build_capacity_stream,
    profile_returns,
    solve_dynamic_equity,
)
from backtest.xsec_data import load

PILOT_EQUITY = 5_000.0
FOLLOWER_EQUITY = STARTING_EQUITY
PRESELECTED = "positive after 1 session"


def confirmation_mask(
    close: pd.DataFrame,
    scout_weights: pd.DataFrame,
    *,
    delay_sessions: int,
    require_positive: bool,
) -> pd.DataFrame:
    """Mark target episodes eligible after a no-lookahead confirmation."""
    if delay_sessions < 1:
        raise ValueError("delay_sessions must be positive")
    prices = close.reindex_like(scout_weights)
    mask = pd.DataFrame(
        False, index=scout_weights.index, columns=scout_weights.columns
    )
    active_columns = scout_weights.columns[
        scout_weights.ne(0).any(axis=0)
    ]
    for symbol in active_columns:
        target = scout_weights[symbol].fillna(0.0).to_numpy()
        sign = np.sign(target)
        previous = np.r_[0.0, sign[:-1]]
        starts = np.flatnonzero((sign != 0) & (sign != previous))
        if not len(starts):
            continue
        values = prices[symbol].to_numpy()
        for start in starts:
            following_changes = np.flatnonzero(
                sign[start + 1:] != sign[start]
            )
            end = (
                start + 1 + int(following_changes[0])
                if len(following_changes)
                else len(sign)
            )
            confirm = start + delay_sessions
            if confirm >= end:
                continue
            entry_price, confirm_price = values[start], values[confirm]
            if not np.isfinite(entry_price) or not np.isfinite(confirm_price):
                continue
            directional_markout = sign[start] * (
                confirm_price / entry_price - 1.0
            )
            if not require_positive or directional_markout > 0:
                mask.iloc[confirm:end, mask.columns.get_loc(symbol)] = True
    return mask


def followed_weights(
    follower_targets: pd.DataFrame,
    scout_targets: pd.DataFrame,
    eligible: pd.DataFrame,
) -> pd.DataFrame:
    same_direction = (
        np.sign(follower_targets) == np.sign(scout_targets)
    )
    return follower_targets.where(eligible & same_direction, 0.0)


def weight_returns(
    close: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float = 15.0,
) -> tuple[pd.Series, pd.Series]:
    daily = close.pct_change(fill_method=None)
    gross = (weights.shift(1) * daily).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    returns = gross - turnover * cost_bps / 10_000
    short_gross = -weights.clip(upper=0).sum(axis=1).shift(1).fillna(0.0)
    return returns, short_gross


def follower_profile(
    common: pd.Series,
    momentum: pd.Series,
    short_gross: pd.Series,
) -> pd.Series:
    aligned = pd.concat({
        "common": 2.0 * common,
        "momentum": 0.60 * momentum,
        "short_gross": 0.60 * short_gross,
    }, axis=1, sort=False).dropna()
    return (
        aligned["common"]
        + aligned["momentum"]
        - aligned["short_gross"] * SHORT_BORROW / TD
        - MARGIN_RATE / TD
    )


def result_windows(variants: dict[str, pd.Series]) -> dict[str, list[dict]]:
    return {
        window: [
            returns_summary(returns.loc[selector], name)
            for name, returns in variants.items()
        ]
        for window, selector in {
            "full": slice(None),
            "early_2020_2022": slice(None, "2022-12-31"),
            "heldout_2023_plus": slice("2023-01-01", None),
        }.items()
    }


def promotion_decision(results: dict[str, list[dict]]) -> tuple[
    bool, list[str]
]:
    reasons = []
    for window in ("early_2020_2022", "heldout_2023_plus"):
        rows = {row["portfolio"]: row for row in results[window]}
        fixed = rows["fixed 2x follower"]
        delayed = rows["unconditional 1-session delay"]
        candidate = rows[PRESELECTED]
        if candidate["sharpe"] <= fixed["sharpe"]:
            reasons.append(f"{window}: Sharpe did not beat fixed 2x")
        if candidate["max_dd"] <= fixed["max_dd"]:
            reasons.append(f"{window}: drawdown did not improve")
        if candidate["sharpe"] <= delayed["sharpe"]:
            reasons.append(f"{window}: confirmation did not beat delay control")
        minimum_cagr = fixed["cagr"] * 0.80
        if candidate["cagr"] < minimum_cagr:
            reasons.append(f"{window}: CAGR penalty exceeded 20%")
    return not reasons, reasons


def main() -> None:
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

    pilot = build_capacity_stream(
        close,
        volume,
        account_equity=PILOT_EQUITY,
        account_multiplier=0.30,
        selection="ranked",
        short_n=20,
    )
    fixed_follower_returns, fixed_follower = solve_dynamic_equity(
        close,
        volume,
        common,
        profile="2x",
        selection="ranked",
        short_n=20,
    )
    follower_targets = fixed_follower.weights
    scout_targets = pilot.weights

    masks = {
        "unconditional 1-session delay": confirmation_mask(
            close, scout_targets,
            delay_sessions=1, require_positive=False,
        ),
        PRESELECTED: confirmation_mask(
            close, scout_targets,
            delay_sessions=1, require_positive=True,
        ),
        "positive after 3 sessions": confirmation_mask(
            close, scout_targets,
            delay_sessions=3, require_positive=True,
        ),
        "positive after 5 sessions": confirmation_mask(
            close, scout_targets,
            delay_sessions=5, require_positive=True,
        ),
    }
    variants = {"fixed 2x follower": fixed_follower_returns}
    diagnostics = {}
    fixed_average_gross = float(follower_targets.abs().sum(axis=1).mean())
    for name, mask in masks.items():
        weights = followed_weights(follower_targets, scout_targets, mask)
        momentum, short_gross = weight_returns(close, weights)
        variants[name] = follower_profile(common, momentum, short_gross)
        entries = (
            weights.ne(0)
            & weights.shift(1).fillna(0).eq(0)
        ).sum().sum()
        diagnostics[name] = {
            "entries": int(entries),
            "average_normalized_mom_gross": round(
                float(weights.abs().sum(axis=1).mean()), 4
            ),
            "gross_retention_vs_fixed": round(
                float(weights.abs().sum(axis=1).mean())
                / fixed_average_gross,
                4,
            ) if fixed_average_gross else 0.0,
        }

    results = result_windows(variants)
    passed, reasons = promotion_decision(results)
    delisting_drags = severe_delisting_drags(
        Path("reports/survivorship_study.json")
    )
    severe_variants = {}
    for name, returns in variants.items():
        retention = (
            1.0
            if name == "fixed 2x follower"
            else diagnostics[name]["gross_retention_vs_fixed"]
        )
        severe_variants[name] = (
            returns - delisting_drags["2x"] * retention / TD
        )

    pilot_profile = profile_returns(common, pilot, profile="base")
    combined = {
        name: (
            pd.concat(
                {"pilot": pilot_profile, "follower": follower},
                axis=1,
            ).dropna()
            .mul([PILOT_EQUITY, FOLLOWER_EQUITY])
            .sum(axis=1)
            / (PILOT_EQUITY + FOLLOWER_EQUITY)
        )
        for name, follower in variants.items()
    }
    payload = {
        "conclusion": (
            "Enable the pilot/follower feature for paper testing."
            if passed else
            "Do not implement the pilot/follower feature; confirmation failed."
        ),
        "promotion_rule_passed": passed,
        "failure_reasons": reasons,
        "preselected_candidate": PRESELECTED,
        "account_model": {
            "pilot_equity": PILOT_EQUITY,
            "follower_equity": FOLLOWER_EQUITY,
            "follower_profile": "2x",
        },
        "method": {
            "signal": "weekly 12-1 MOM_LS target episodes",
            "confirmation": "positive directional close-to-close markout",
            "follower_cost_bps": 15.0,
            "short_borrow_rate": SHORT_BORROW,
            "margin_rate": MARGIN_RATE,
        },
        "diagnostics": diagnostics,
        "follower_results": results,
        "severe_delisting_follower_results": result_windows(severe_variants),
        "combined_capital_results": result_windows(combined),
        "limitations": [
            "Currently listed universe remains survivorship biased.",
            (
                "Severe delisting drag is scaled by average retained MOM "
                "gross, not reconstructed position by position."
            ),
            "Paper fills do not model market impact or queue position.",
            "The pilot tests existing MOM_LS positions, not novel signals.",
            "Close-to-close confirmation cannot represent intraday latency.",
            "Historical easy-to-borrow status is unavailable.",
        ],
    }
    out = Path("reports/pilot_follower_study.json")
    out.write_text(json.dumps(payload, indent=2))

    for window, rows in results.items():
        print(f"\nFOLLOWER — {window}")
        print(pd.DataFrame(rows)[
            ["portfolio", "cagr", "sharpe", "max_dd"]
        ].to_string(index=False))
    print("\nDIAGNOSTICS")
    print(pd.DataFrame(diagnostics).T.to_string())
    print(f"\nPROMOTION RULE: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
