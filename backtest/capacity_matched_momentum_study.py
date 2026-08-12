"""Pre-registered capacity-matched MOM_LS risk-reduction study.

The paper accounts cannot short fractionally.  When an equal-weight short
slot rounds to zero or below target, production still funds the full long
side and the nominally market-neutral sleeve becomes unintentionally net
long.  This candidate sizes the long basket to the whole-share short dollars
the account can target that day.  It does not alter ranks or concentrate a
short beyond its original equal-weight target.

Objective class: risk_reducer.  Before results are inspected, the candidate
must reduce max drawdown by at least 5% while costing no more than one CAGR
percentage point in every profile and screening window.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest.deployable_momentum import build_deployable_stream
from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.promotion import passes_gate_all_cells
from backtest.short_capacity_study import (
    MOM_ACCOUNT_MULTIPLIER,
    STARTING_EQUITY,
    profile_returns,
)
from backtest.xsec_data import load

WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}


def solve(close, volume, common, *, profile: str, capacity_match: bool):
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result = None
    portfolio = pd.Series(dtype=float)
    for _ in range(3):
        result, _ = build_deployable_stream(
            close,
            volume,
            account_equity=equity,
            account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
            match_long_to_short_capacity=capacity_match,
        )
        portfolio = profile_returns(common, result, profile=profile)
        equity = (
            STARTING_EQUITY * (1 + portfolio).cumprod()
        ).reindex(close.index).ffill().fillna(STARTING_EQUITY)
    return portfolio, result


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]
    streams = build_streams()
    common = 0.40 * streams["spy"] + 0.25 * streams["tsmom"] + 0.20 * streams["trend"]

    variants = {
        "unmatched whole-share control": False,
        "capacity-matched longs": True,
    }
    solved = {}
    for profile in ("base", "2x"):
        for label, enabled in variants.items():
            print(f"Running {profile}: {label}...", flush=True)
            solved[(profile, label)] = solve(
                close, volume, common, profile=profile, capacity_match=enabled
            )

    performance = {}
    cells = []
    for window, slicer in WINDOWS.items():
        rows = []
        for (profile, label), (returns, _) in solved.items():
            row = returns_summary(returns.loc[slicer], f"{profile} — {label}")
            row.update(profile=profile, variant=label)
            rows.append(row)
        performance[window] = rows
        for profile in ("base", "2x"):
            by_variant = {r["variant"]: r for r in rows if r["profile"] == profile}
            cells.append((
                window,
                profile,
                by_variant["unmatched whole-share control"],
                by_variant["capacity-matched longs"],
            ))

    gate = passes_gate_all_cells(
        cells,
        "risk_reducer",
        max_cagr_cost_pp=1.0,
        min_dd_improvement_pct=0.05,
    )
    exposure = []
    for (profile, label), (_, result) in solved.items():
        multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
        weights = result.weights
        exposure.append({
            "profile": profile,
            "variant": label,
            "average_account_long_gross": round(
                float(weights.clip(lower=0).sum(axis=1).mean()) * multiplier, 4
            ),
            "average_account_short_gross": round(
                float(-weights.clip(upper=0).sum(axis=1).mean()) * multiplier, 4
            ),
        })

    payload = {
        "decision": "promote_to_shadow" if gate["passed"] else "reject",
        "pre_registration": {
            "objective_class": "risk_reducer",
            "candidate": "Size MOM_LS longs to attainable whole-share short target dollars without changing ranks or concentrating shorts.",
            "max_cagr_cost_pp": 1.0,
            "min_dd_improvement_pct": 0.05,
            "scope": "Both profiles and both screening windows must pass; 2026-08-04 onward is frozen final validation.",
        },
        "promotion_gate": gate,
        "performance": performance,
        "exposure": exposure,
        "limitations": [
            "Historical easy-to-borrow availability is unavailable.",
            "Current-listing stock data remains survivorship-biased.",
            "The simulator evaluates once per close rather than twice intraday.",
        ],
    }
    out = Path("reports/capacity_matched_momentum_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision: {payload['decision']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
