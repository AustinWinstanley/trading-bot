"""Pre-registered whole-share-aware MOM_LS breadth study.

The $10,000 paper accounts spread the MOM_LS short allocation over twenty
names.  That makes a base-profile short slot roughly $75, so many selected
stocks round to zero shares.  This study changes *both* sides of the sleeve to
5, 10, 15, or 20 equal-weight names and evaluates the resulting deployable
portfolio through the same drift-band, no-averaging, whole-share simulator as
production-fidelity studies.

Objective class: return_enhancer.  The primary candidate is ten names per
side, chosen before results because it doubles slot size while retaining ten
independent positions on each side.  Five and fifteen names are sensitivity
checks and cannot be promoted from this run.  The primary candidate must have
higher Sharpe, no lower CAGR, and no worse maximum drawdown in both profiles
and both screening windows.  Data from 2026-08-04 onward remains frozen final
validation and is not used here.
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
PRIMARY_BREADTH = 10
BREADTHS = (20, PRIMARY_BREADTH, 15, 5)


def solve(close, volume, common, *, profile: str, breadth: int):
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result = None
    portfolio = pd.Series(dtype=float)
    diagnostics = None
    for _ in range(3):
        result, diagnostics = build_deployable_stream(
            close,
            volume,
            account_equity=equity,
            account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
            long_n=breadth,
            short_n=breadth,
        )
        portfolio = profile_returns(common, result, profile=profile)
        equity = (
            STARTING_EQUITY * (1 + portfolio).cumprod()
        ).reindex(close.index).ffill().fillna(STARTING_EQUITY)
    return portfolio, result, diagnostics


def exposure_summary(profile: str, breadth: int, result, diagnostics) -> dict:
    weights = result.weights
    multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
    live = result.rebalances
    selected = int(live["selected_shorts"].sum()) if len(live) else 0
    zeros = int(live["zero_share_targets"].sum()) if len(live) else 0
    return {
        "profile": profile,
        "breadth_per_side": breadth,
        "average_account_long_gross": round(
            float(weights.clip(lower=0).sum(axis=1).mean()) * multiplier, 4
        ),
        "average_account_short_gross": round(
            float(-weights.clip(upper=0).sum(axis=1).mean()) * multiplier, 4
        ),
        "zero_share_target_pct": round(100 * zeros / selected, 2) if selected else 0.0,
        "trades": diagnostics.trades,
        "annualized_turnover": round(
            diagnostics.traded_notional / STARTING_EQUITY
            / max((weights.index[-1] - weights.index[0]).days / 365.25, 1e-9),
            3,
        ),
    }


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]
    streams = build_streams()
    common = 0.40 * streams["spy"] + 0.25 * streams["tsmom"] + 0.20 * streams["trend"]

    solved = {}
    exposure = []
    for profile in ("base", "2x"):
        for breadth in BREADTHS:
            print(f"Running {profile}: {breadth} names per side...", flush=True)
            portfolio, result, diagnostics = solve(
                close, volume, common, profile=profile, breadth=breadth
            )
            solved[(profile, breadth)] = portfolio
            exposure.append(
                exposure_summary(profile, breadth, result, diagnostics)
            )

    performance = {}
    sensitivity_gates = {}
    for window, slicer in WINDOWS.items():
        rows = []
        for (profile, breadth), returns in solved.items():
            row = returns_summary(
                returns.loc[slicer], f"{profile} — {breadth} names per side"
            )
            row.update(profile=profile, breadth_per_side=breadth)
            rows.append(row)
        performance[window] = rows

    for breadth in (PRIMARY_BREADTH, 15, 5):
        cells = []
        for window, rows in performance.items():
            for profile in ("base", "2x"):
                control = next(
                    r for r in rows
                    if r["profile"] == profile and r["breadth_per_side"] == 20
                )
                candidate = next(
                    r for r in rows
                    if r["profile"] == profile
                    and r["breadth_per_side"] == breadth
                )
                cells.append((window, profile, control, candidate))
        gate = passes_gate_all_cells(cells, "return_enhancer")
        sensitivity_gates[str(breadth)] = gate
    primary_gate = sensitivity_gates[str(PRIMARY_BREADTH)]
    payload = {
        "decision": "promote_to_shadow" if primary_gate["passed"] else "reject",
        "pre_registration": {
            "objective_class": "return_enhancer",
            "control_breadth_per_side": 20,
            "primary_candidate_breadth_per_side": PRIMARY_BREADTH,
            "sensitivity_only": [15, 5],
            "promotion_rule": "Higher Sharpe, no lower CAGR, and no worse maximum drawdown in both profiles and both screening windows.",
            "scope": "2026-08-04 onward remains frozen final validation.",
        },
        "primary_promotion_gate": primary_gate,
        "sensitivity_gates_not_eligible_for_promotion": {
            key: value for key, value in sensitivity_gates.items()
            if key != str(PRIMARY_BREADTH)
        },
        "performance": performance,
        "exposure": exposure,
        "limitations": [
            "Historical easy-to-borrow availability is unavailable.",
            "Current-listing stock data remains survivorship-biased.",
            "The simulator evaluates once per close rather than twice intraday.",
            "Concentrating breadth increases single-name and sector-event risk.",
        ],
    }
    out = Path("reports/momentum_breadth_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision: {payload['decision']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
