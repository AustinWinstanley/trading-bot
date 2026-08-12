"""Pre-registered study of the live no-averaging gate on MOM_LS.

Candidate: permit a losing incumbent to be restored only to its unchanged,
systematically computed target. It may never exceed that target. All other
selection, capacity, drift-band, cost, borrow and portfolio assumptions remain
the same. Objective class: return_enhancer.
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


def solve(close, volume, common, *, profile: str, restore: bool):
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result = diag = None
    portfolio = pd.Series(dtype=float)
    for _ in range(3):
        result, diag = build_deployable_stream(
            close,
            volume,
            account_equity=equity,
            account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
            allow_target_restoration_at_loss=restore,
        )
        portfolio = profile_returns(common, result, profile=profile)
        curve = STARTING_EQUITY * (1 + portfolio).cumprod()
        equity = curve.reindex(close.index).ffill().fillna(STARTING_EQUITY)
    return portfolio, result, diag


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in classified["stocks"] if s in close_all]
    close, volume = close_all[stocks], volume_all[stocks]
    streams = build_streams()
    common = 0.40 * streams["spy"] + 0.25 * streams["tsmom"] + 0.20 * streams["trend"]

    variants = {
        "live no-averaging control": False,
        "systematic target restoration": True,
    }
    solved = {}
    for profile in ("base", "2x"):
        for variant, restore in variants.items():
            print(f"Running {profile}: {variant}...", flush=True)
            solved[(profile, variant)] = solve(
                close, volume, common, profile=profile, restore=restore
            )

    performance = {}
    cells = []
    diagnostics = []
    for window, slicer in WINDOWS.items():
        rows = []
        for (profile, variant), (returns, result, diag) in solved.items():
            row = returns_summary(returns.loc[slicer], f"{profile} — {variant}")
            row.update(profile=profile, variant=variant)
            rows.append(row)
        performance[window] = rows
        for profile in ("base", "2x"):
            by_variant = {r["variant"]: r for r in rows if r["profile"] == profile}
            cells.append((
                window,
                profile,
                by_variant["live no-averaging control"],
                by_variant["systematic target restoration"],
            ))

    for (profile, variant), (_, result, diag) in solved.items():
        diagnostics.append({
            "profile": profile,
            "variant": variant,
            "rejected_restorations": diag.rejected_restorations,
            "rejected_restoration_notional": round(diag.rejected_restoration_notional, 2),
            "trades": diag.trades,
            "traded_notional": round(diag.traded_notional, 2),
            "average_account_short_gross": round(
                float(result.short_gross.mean()) * MOM_ACCOUNT_MULTIPLIER[profile], 4
            ),
        })

    gate = passes_gate_all_cells(cells, "return_enhancer")
    payload = {
        "decision": "promote_to_shadow" if gate["passed"] else "reject",
        "pre_registration": {
            "objective_class": "return_enhancer",
            "control": "Live no-averaging gate with weekly ranks, daily 20%/$25 drift band, fractional longs and whole-share shorts.",
            "candidate": "Permit increases in a losing incumbent only up to its unchanged systematic target.",
            "promotion_rule": "Higher Sharpe, no lower CAGR and no worse max drawdown in both profiles and both screening windows.",
        },
        "promotion_gate": gate,
        "performance": performance,
        "diagnostics": diagnostics,
        "limitations": [
            "Historical easy-to-borrow availability is unavailable.",
            "Current-listing stock data remains survivorship-biased.",
            "The simulator trades once per close; production evaluates twice intraday.",
            "2026-08-04 onward remains reserved for final shadow validation and is not used to tune this candidate.",
        ],
    }
    out = Path("reports/target_restoration_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision: {payload['decision']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
