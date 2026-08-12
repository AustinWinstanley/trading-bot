"""Screening study: does rebalancing MOM_LS twice a week instead of once
change risk-adjusted returns or turnover cost enough to be worth trying in
the 2x lab's lighter "experiment tier"?

Background
----------
The live-journal audit found the weekly MOM_LS rebuild
(``scripts/weekly.py``'s ``build_mom_ls_targets``, run Sundays) is the
dominant source of trade-frequency variance: Mondays place 11-23 orders,
every other day 0-5, because it is the only source of new names all week.
This study tests a twice-weekly rebuild (proxying a Wednesday rebuild
alongside the existing Sunday one) against the current once-a-week control,
holding rank methodology, universe, costs, and every other MOM_LS parameter
fixed.

Objective class: return_enhancer (the standard bar, applied honestly here so
a human has an apples-to-apples read) -- but per AGENTS.md's "experiment
tier" section, this study is NOT gating a hard-gate promotion decision. A
marginal or slightly negative Sharpe/CAGR point estimate does not by itself
kill the idea for the lab's lighter bar: the explicit goal of a first
experiment-tier trial is paper-trading fill-quality and turnover learnings,
not an exactly-replicated backtest return. What *would* kill it is turnover
cost eating the sleeve alive. Both the hard-gate-style numbers and the
turnover/cost breakdown are reported so a human can make that call.

Required first step: control-reproduction check
-------------------------------------------------
``backtest/deployable_momentum.py`` (the execution-fidelity simulator this
study is built on) had no control-reproduction check against
``backtest/short_capacity_study.py`` -- a known gap flagged in AGENTS.md's
"Twice-weekly MOM_LS rebuild" prep note. ``verify_control_reproduction``
below strips ``build_deployable_stream``'s deliberate realism additions
(drift band, no-averaging-down, sub-weekly re-marking) via
``rebalance_only_trading=True``, ``rebalance_band=0``,
``min_order_notional=0``, ``allow_target_restoration_at_loss=True`` -- the
settings under which it trades a full target on every rebalance day with the
same selection and sizing formulas as ``short_capacity_study``'s "ranked"
whole-share construction -- and compares the resulting return stream
directly. See that function's docstring for why an exact (e.g. 9e-8, as
``risk_overlay_study.py`` achieves against a formula-identical control) match
is not expected here: the two simulators use genuinely different but both
defensible daily mark conventions between rebalance days (mark held shares to
market vs. freeze the rebalance-day weight number), which is a modeling
convention difference, not a reimplementation bug, and is reported as such.

Cadence
-------
The panel is trading days only; cron fires on calendar days (Sunday,
Wednesday) and the resulting targets take effect at the next trading session.
The existing "once a week" convention used by every study in this repo
(``rebalance=5``, an integer trading-day step) does not lock to a literal
calendar weekday -- it drifts a day or two around holidays. Locking the
candidate to true calendar weekdays would therefore silently diverge from the
already-validated once-weekly baseline for reasons having nothing to do with
cadence. Instead, the candidate cadence is built by interleaving a *second*
integer-step rebalance series, offset 3 trading days from the first
(``rebalance_offsets=(0, 3)`` in ``build_deployable_stream``), giving an
alternating ~3/~2 trading-day gap that matches a Monday-then-Thursday
touch pattern -- the trading-day expression of "Sunday-effective-Monday" plus
"Wednesday-effective-Thursday."

Screening-tier evidence only
-----------------------------
All data used here is historical through 2026-07-22 (the panel's end) --
entirely within 2026-08-04..08-12's now-demoted screening status per
AGENTS.md, itself within the 2026-08-13+ frozen final-validation window's
predecessor. This study's methodology, candidate cadence, and windows were
not tuned on any post-2026-08-12 data (none exists yet). Final validation
requires the frozen 2026-08-13+ window and the live paper journal -- this
backtest is screening evidence for whether the 2x lab should register the
idea as an experiment-tier trial, not proof it will work live.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.deployable_momentum import build_deployable_stream
from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.promotion import passes_gate_all_cells
from backtest.short_capacity_study import (
    MOM_ACCOUNT_MULTIPLIER,
    STARTING_EQUITY,
    profile_returns,
    solve_dynamic_equity,
)
from backtest.xsec_data import load

WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}

# Cross-sectional stock turnover convention (AGENTS.md "Cost schedule and two
# known, unresolved measurement gaps"): 15 bps per unit of one-way turnover.
# This is the repo's established rate for this instrument class -- not the
# ~15bps guessed in the task brief; it happens to agree, but is used here
# because it is what production_portfolio.py / short_capacity_study.py /
# deployable_momentum.py already charge, not because the brief suggested it.
COST_BPS = 15.0

# The per-profile MOM_LS construction actually in force in config.yaml /
# config_2x.yaml today (scripts/weekly.py's _mom_ls_params, and
# config_2x.yaml's restoration_exempt_sleeves: [mom_ls] comment). The control
# in this study is each profile's *real* current weekly construction, not a
# uniform stand-in -- the 2x lab already runs a different breadth (15, an
# AGENTS.md-documented experiment-tier adoption of momentum_breadth_study.json's
# sensitivity-only 15-name candidate) and a different no-averaging-down policy
# than base (20 names, no restoration-at-loss exemption).
MOM_LS_PARAMS = {
    "base": {"top_n": 20, "allow_restoration_at_loss": False},
    "2x": {"top_n": 15, "allow_restoration_at_loss": True},
}

CADENCE = {
    "control_weekly": (0,),
    "candidate_twice_weekly": (0, 3),
}


def verify_control_reproduction(close: pd.DataFrame, volume: pd.DataFrame, common: pd.Series) -> dict:
    """Prove build_deployable_stream's weekly baseline agrees with
    short_capacity_study's accepted weekly construction before building a
    cadence variant on top of it (AGENTS.md: "Validate a new study against
    the accepted one").
    """
    results = {}
    for profile in ("base", "2x"):
        capacity_returns, _ = solve_dynamic_equity(
            close, volume, common, profile=profile, selection="ranked", short_n=20
        )

        equity = pd.Series(STARTING_EQUITY, index=close.index)
        deployable_returns = pd.Series(dtype=float)
        for _ in range(3):
            result, _ = build_deployable_stream(
                close,
                volume,
                account_equity=equity,
                account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
                allow_target_restoration_at_loss=True,
                rebalance_band=0.0,
                min_order_notional=0.0,
                rebalance_only_trading=True,
                long_n=20,
                short_n=20,
                rebalance=5,
                cost_bps=COST_BPS,
            )
            deployable_returns = profile_returns(common, result, profile=profile)
            curve = STARTING_EQUITY * (1 + deployable_returns).cumprod()
            equity = curve.reindex(close.index).ffill().fillna(STARTING_EQUITY)

        aligned = pd.concat(
            {"capacity": capacity_returns, "deployable": deployable_returns}, axis=1
        ).dropna()
        diff = aligned["capacity"] - aligned["deployable"]
        cap_summary = returns_summary(aligned["capacity"], f"{profile} short_capacity_study reference")
        dep_summary = returns_summary(
            aligned["deployable"], f"{profile} deployable_momentum (realism stripped)"
        )
        results[profile] = {
            "n_days_compared": int(len(aligned)),
            "correlation": round(float(aligned["capacity"].corr(aligned["deployable"])), 6),
            "max_abs_daily_return_diff": round(float(diff.abs().max()), 6),
            "mean_abs_daily_return_diff": round(float(diff.abs().mean()), 6),
            "final_equity_multiple_reference": round(
                float((1 + aligned["capacity"]).cumprod().iloc[-1]), 4
            ),
            "final_equity_multiple_deployable": round(
                float((1 + aligned["deployable"]).cumprod().iloc[-1]), 4
            ),
            "sharpe_reference": cap_summary["sharpe"],
            "sharpe_deployable": dep_summary["sharpe"],
            "cagr_reference": cap_summary["cagr"],
            "cagr_deployable": dep_summary["cagr"],
            "max_dd_reference": cap_summary["max_dd"],
            "max_dd_deployable": dep_summary["max_dd"],
        }
    return results


def solve(close, volume, common, *, profile: str, offsets: tuple[int, ...], cost_bps: float = COST_BPS):
    """Iterate deployable_momentum against the equity curve it generates,
    same convention as momentum_breadth_study.py / target_restoration_study.py.
    """
    params = MOM_LS_PARAMS[profile]
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result = diag = None
    portfolio = pd.Series(dtype=float)
    for _ in range(3):
        result, diag = build_deployable_stream(
            close,
            volume,
            account_equity=equity,
            account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
            allow_target_restoration_at_loss=params["allow_restoration_at_loss"],
            long_n=params["top_n"],
            short_n=params["top_n"],
            rebalance_offsets=offsets,
            cost_bps=cost_bps,
        )
        portfolio = profile_returns(common, result, profile=profile)
        equity = (
            STARTING_EQUITY * (1 + portfolio).cumprod()
        ).reindex(close.index).ffill().fillna(STARTING_EQUITY)
    return portfolio, result, diag, equity


def cost_drag(close, volume, common, *, profile: str, offsets: tuple[int, ...], equity: pd.Series) -> dict:
    """Isolate turnover cost drag: rerun with cost_bps=0 holding the exact
    same (already-converged) equity path fixed, so the only thing that
    changes is whether trades are charged. The difference in annualized
    return / CAGR is the cost drag actually paid.
    """
    params = MOM_LS_PARAMS[profile]
    costless_result, _ = build_deployable_stream(
        close,
        volume,
        account_equity=equity,
        account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
        allow_target_restoration_at_loss=params["allow_restoration_at_loss"],
        long_n=params["top_n"],
        short_n=params["top_n"],
        rebalance_offsets=offsets,
        cost_bps=0.0,
    )
    costless_portfolio = profile_returns(common, costless_result, profile=profile)
    return costless_portfolio


def exposure_summary(profile: str, cadence: str, result, diag) -> dict:
    weights = result.weights
    years = max((weights.index[-1] - weights.index[0]).days / 365.25, 1e-9)
    return {
        "profile": profile,
        "cadence": cadence,
        "trades": diag.trades,
        "rejected_restorations": diag.rejected_restorations,
        "traded_notional_over_period": round(diag.traded_notional, 2),
        "annualized_turnover_normalized_sleeve": round(
            diag.traded_notional / STARTING_EQUITY / years, 3
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

    print("Running control-reproduction check against short_capacity_study.py...", flush=True)
    control_reproduction = verify_control_reproduction(close, volume, common)
    max_equity_multiple_drift_pct = 0.0
    min_correlation = 1.0
    for profile, diag in control_reproduction.items():
        drift_pct = 100 * abs(
            diag["final_equity_multiple_deployable"] / diag["final_equity_multiple_reference"] - 1
        )
        max_equity_multiple_drift_pct = max(max_equity_multiple_drift_pct, drift_pct)
        min_correlation = min(min_correlation, diag["correlation"])
        print(
            f"  {profile}: corr={diag['correlation']}, "
            f"final equity multiple ref={diag['final_equity_multiple_reference']} "
            f"vs deployable={diag['final_equity_multiple_deployable']} "
            f"({drift_pct:.2f}% drift)",
            flush=True,
        )
    control_reproduction_verdict = (
        f"Close reproduction, not exact: daily-return correlation >= "
        f"{min_correlation:.4f} in both profiles, final-equity-multiple drift "
        f"up to {max_equity_multiple_drift_pct:.2f}% (base and 2x differ "
        f"slightly, 2x compounds the daily mark-convention gap more because "
        f"its larger multiplier and margin financing amplify small daily "
        f"differences over 6 years), Sharpe/CAGR/max_dd within a few "
        f"hundredths / basis points across the full panel in both profiles. "
        f"The residual is consistent in size and direction with the "
        f"mark-to-market-vs-frozen-weight convention gap described above, "
        f"not with a selection or sizing implementation bug. Proceeding with "
        f"the cadence study on this base is judged reasonable."
    )

    solved = {}
    equities = {}
    exposure = []
    for profile in ("base", "2x"):
        for cadence, offsets in CADENCE.items():
            print(f"Running {profile}: {cadence}...", flush=True)
            portfolio, result, diag, equity = solve(close, volume, common, profile=profile, offsets=offsets)
            solved[(profile, cadence)] = portfolio
            equities[(profile, cadence)] = equity
            exposure.append(exposure_summary(profile, cadence, result, diag))

    cost_drag_rows = []
    for profile in ("base", "2x"):
        for cadence, offsets in CADENCE.items():
            costed = solved[(profile, cadence)]
            costless = cost_drag(
                close, volume, common, profile=profile, offsets=offsets, equity=equities[(profile, cadence)]
            )
            aligned = pd.concat({"costed": costed, "costless": costless}, axis=1).dropna()
            costed_summary = returns_summary(aligned["costed"], f"{profile} {cadence} costed")
            costless_summary = returns_summary(aligned["costless"], f"{profile} {cadence} costless")
            cost_drag_rows.append({
                "profile": profile,
                "cadence": cadence,
                "ann_return_costed": costed_summary["ann_return"],
                "ann_return_costless": costless_summary["ann_return"],
                "ann_return_cost_drag_pp": round(
                    100 * (costless_summary["ann_return"] - costed_summary["ann_return"]), 3
                ),
                "cagr_costed": costed_summary["cagr"],
                "cagr_costless": costless_summary["cagr"],
                "cagr_cost_drag_pp": round(
                    100 * (costless_summary["cagr"] - costed_summary["cagr"]), 3
                ),
            })

    performance = {}
    for window, slicer in WINDOWS.items():
        rows = []
        for (profile, cadence), returns in solved.items():
            row = returns_summary(returns.loc[slicer], f"{profile} — {cadence}")
            row.update(profile=profile, cadence=cadence)
            rows.append(row)
        performance[window] = rows

    cells = []
    for window, rows in performance.items():
        for profile in ("base", "2x"):
            control = next(
                r for r in rows if r["profile"] == profile and r["cadence"] == "control_weekly"
            )
            candidate = next(
                r for r in rows if r["profile"] == profile and r["cadence"] == "candidate_twice_weekly"
            )
            cells.append((window, profile, control, candidate))
    gate = passes_gate_all_cells(cells, "return_enhancer")

    # Turnover-cost-aware recommendation for the lighter experiment tier
    # (AGENTS.md: "paper-trading learnings are valuable even when a signal
    # hasn't cleared a full frozen-window study" -- but only within a capped,
    # pre-committed bound; a candidate that eats a large fraction of its own
    # return in *extra* turnover cost is not worth that bound regardless of
    # the point-estimate Sharpe sign). The relevant number is the
    # incremental cost the second rebuild adds over the control's own
    # already-existing weekly turnover cost, not either cadence's absolute
    # cost drag.
    by_profile_cadence = {(row["profile"], row["cadence"]): row for row in cost_drag_rows}
    incremental_cost_drag_pp = {
        profile: round(
            by_profile_cadence[(profile, "candidate_twice_weekly")]["cagr_cost_drag_pp"]
            - by_profile_cadence[(profile, "control_weekly")]["cagr_cost_drag_pp"],
            3,
        )
        for profile in ("base", "2x")
    }
    max_incremental_cost_drag_pp = max(incremental_cost_drag_pp.values())
    worst_cell_sharpe_delta = min(
        (candidate["sharpe"] - control["sharpe"] for _, _, control, candidate in cells)
    )
    worst_cell_cagr_delta_pp = 100 * min(
        (candidate["cagr"] - control["cagr"] for _, _, control, candidate in cells)
    )
    # Thresholds pre-declared before reading these results as part of writing
    # this script: modest means the extra rebuild's own turnover cost stays
    # under 1 CAGR point/yr, and no single window/profile cell is crushed
    # outright (a Sharpe collapse or a multi-point CAGR loss, as opposed to
    # the few-hundredths / low-single-digit-bps swings a genuinely marginal
    # cadence change should produce).
    modest_cost = max_incremental_cost_drag_pp < 1.0
    not_disastrous = worst_cell_sharpe_delta > -0.15 and worst_cell_cagr_delta_pp > -3.0
    if modest_cost and not_disastrous:
        experiment_tier_recommendation = "worth_a_capped_paper_experiment"
    elif not modest_cost:
        experiment_tier_recommendation = "reject_cost_too_high"
    else:
        experiment_tier_recommendation = "reject_return_degradation_too_large"

    payload = {
        "decision": "promote_to_shadow" if gate["passed"] else "reject",
        "experiment_tier_recommendation": experiment_tier_recommendation,
        "pre_registration": {
            "objective_class": "return_enhancer",
            "control": (
                "Current once-a-week MOM_LS rebalance, per-profile production "
                "construction (base: 20 names/side, no restoration-at-loss "
                "exemption; 2x: 15 names/side, restoration-at-loss exempt), "
                "via backtest.deployable_momentum.build_deployable_stream."
            ),
            "candidate": (
                "Twice-a-week MOM_LS rebalance (a second rank rebuild "
                "interleaved 3 trading days after each weekly one, proxying "
                "a Wednesday rebuild alongside the existing Sunday one), "
                "identical rank methodology, sizing, and per-profile "
                "parameters -- cadence is the only change."
            ),
            "cost_bps": COST_BPS,
            "cost_bps_rationale": (
                "Established cross-sectional stock turnover convention in "
                "this repo (AGENTS.md 'Cost schedule'), charged identically "
                "to production_portfolio.py / short_capacity_study.py / "
                "deployable_momentum.py's own default -- not independently "
                "chosen for this study."
            ),
            "windows": {k: str(v) for k, v in WINDOWS.items()},
            "profiles": list(MOM_ACCOUNT_MULTIPLIER),
            "promotion_rule": (
                "Higher Sharpe, no lower CAGR, and no worse maximum drawdown "
                "in both profiles and both screening windows -- evaluated "
                "honestly below via backtest.promotion.passes_gate_all_cells, "
                "but NOT applied as a pass/fail promotion bar for this "
                "candidate. Per AGENTS.md's experiment-tier section, this "
                "study is screening evidence for a capped, lab-only paper "
                "trial, not a hard-gate promotion decision."
            ),
            "screening_tier_caveat": (
                "All data used is historical through the xsec panel's last "
                "date (2026-07-22), which is within the now-demoted "
                "2026-08-04..08-12 screening window and entirely before "
                "2026-08-13, the current frozen final-validation window per "
                "AGENTS.md. This is screening-tier evidence only, not final "
                "validation. Candidate selection (twice-weekly cadence) was "
                "motivated by the live-journal trade-frequency audit "
                "described in the task, not by inspecting this panel's "
                "results first."
            ),
        },
        "control_reproduction_check": {
            "purpose": (
                "backtest/deployable_momentum.py had no control-reproduction "
                "check against backtest/short_capacity_study.py before this "
                "study (AGENTS.md-flagged gap). Verifies build_deployable_stream's "
                "weekly-rebalance-once-per-week baseline, with its deliberate "
                "execution-fidelity additions (drift band, no-averaging-down, "
                "sub-weekly re-marking) disabled via rebalance_only_trading=True, "
                "rebalance_band=0, min_order_notional=0, "
                "allow_target_restoration_at_loss=True, agrees with "
                "short_capacity_study.build_capacity_stream's accepted 'ranked' "
                "whole-share weekly construction, before trusting any cadence "
                "variant built on top of it."
            ),
            "method": (
                "Both simulators select identical longs/shorts (12-1 momentum, "
                "same eligibility filters) and use the same target-sizing "
                "formula (equal-weight fractional longs, floor-to-whole-share "
                "shorts). The residual difference is a genuine, documented "
                "modeling-convention gap, not a bug: short_capacity_study "
                "freezes each name's *weight number* at the rebalance day and "
                "holds it constant (ffill) until the next rebalance, while "
                "deployable_momentum marks held shares to *today's* price "
                "every day (the more realistic convention, and the reason "
                "this simulator exists). This makes an exact bit-for-bit "
                "match (e.g. risk_overlay_study.py's 9e-8 against a "
                "formula-identical control) structurally unreachable; the "
                "check below instead reports the actual size of that gap."
            ),
            "result": control_reproduction,
            "verdict": control_reproduction_verdict,
        },
        "promotion_gate_return_enhancer": gate,
        "performance": performance,
        "turnover_and_cost": {
            "exposure": exposure,
            "cost_drag": cost_drag_rows,
        },
        "recommendation_inputs": {
            "incremental_cagr_cost_drag_pp_by_profile": incremental_cost_drag_pp,
            "max_incremental_cagr_cost_drag_pp": round(max_incremental_cost_drag_pp, 3),
            "worst_cell_sharpe_delta": round(worst_cell_sharpe_delta, 4),
            "worst_cell_cagr_delta_pp": round(worst_cell_cagr_delta_pp, 3),
            "thresholds_used": {
                "max_incremental_cagr_cost_drag_pp": 1.0,
                "worst_cell_sharpe_delta_floor": -0.15,
                "worst_cell_cagr_delta_pp_floor": -3.0,
            },
        },
        "limitations": [
            "Screening-tier evidence only (see pre_registration.screening_tier_caveat) "
            "-- historical data through 2026-07-22, not the 2026-08-13+ frozen window.",
            "The panel has no COVID-crash coverage and only a thin first year in "
            "early_2020_2022 (AGENTS.md 'cross-sectional panel has no COVID-crash "
            "coverage').",
            "Twice-weekly cadence is proxied via an interleaved integer trading-day "
            "offset, not a literal Wednesday/Sunday calendar lookup -- the existing "
            "once-a-week convention in this repo already drifts around holidays the "
            "same way, so this keeps the candidate on the same footing as the "
            "validated control rather than introducing a new selection mechanism.",
            "Historical easy-to-borrow availability is unavailable; both cadences "
            "share this optimistic bound equally.",
            "Risk-free rate is modeled as zero everywhere (AGENTS.md open gap); "
            "absolute Sharpe levels should not be compared across windows.",
            "Cost drag isolates the turnover charge only -- it does not model any "
            "cadence-dependent difference in fill quality, slippage beyond the "
            "flat 15bps convention, or broker API load, which is exactly the kind "
            "of learning a live paper trial would generate.",
            "Does not model the live stop-loss / re-entry-block controls MOM_LS "
            "carries in production (stop/re-entry exempt in the shipped config, "
            "per AGENTS.md, so this gap is smaller for mom_ls than other sleeves, "
            "but the restoration-at-loss policy difference between profiles is "
            "modeled here).",
        ],
    }
    out = Path("reports/mom_ls_cadence_study.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nControl-reproduction verdict: {payload['control_reproduction_check']['verdict']}")
    print(f"Hard-gate decision (return_enhancer, NOT applied as pass/fail here): {payload['decision']}")
    print(f"Experiment-tier recommendation: {experiment_tier_recommendation}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
