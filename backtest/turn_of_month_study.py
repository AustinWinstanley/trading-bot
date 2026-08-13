"""Turn-of-month calendar overlay study on 33 years of SPY history.

Candidate: a sleeve that holds SPY only during the turn-of-month window --
the last trading day of each month (T-1) through the first three trading
days of the next month (T+3), 4 trading days per month -- and cash
otherwise, at 2 bps per one-way leg (~24 round-trip legs/year). Deployment
context: the 2x lab's `trend` sleeve parks in BIL when SPY is under its
200DMA (`trend_reserve_symbol`); this overlay would temporarily redeploy a
small slice of capital during the historically favorable window.

This is one of the rare studies that gets REAL long history:
`state/history_deep/SPY.parquet` (dividend-adjusted, 1993-01-29 onward),
per-decade breakdowns so documented anomaly decay is visible instead of
averaged away. The standard screening windows (2020+) are exactly where
calendar anomalies are weakest, so the long-history evidence is reported
separately from -- and alongside -- the standard
`early_2020_2022`/`heldout_2023_plus` screening gate.

Everything in PRE_REGISTRATION below was written before any results were
computed. Screening-tier evidence only (all data ends 2026-07-22, before
the 2026-08-13 frozen final-validation window); the relevant bar is the
config_2x.yaml experiment tier, not a hard-gate core promotion.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.promotion import passes_gate_all_cells
from backtest.tsmom_fxe_drop_study import (
    WINDOWS,
    full_portfolio,
    validate_control_reproduction,
)
from engine.tiingo import load_parquet

DEEP_HISTORY = Path("state/history_deep")
COST_BPS_PER_LEG = 2.0
STRESS_COST_BPS_PER_LEG = 5.0
# Primary marginal allocation of the overlay stream on top of the production
# portfolio: 5% of equity (base), leverage-scaled to 10% at 2x -- the same
# proportional doubling every other sleeve gets in config_2x.yaml.
ALLOCATION = {"base": 0.05, "2x": 0.10}
ALLOCATION_SENSITIVITY = {"base": 0.10, "2x": 0.20}

PRE_REGISTRATION = {
    "registered_before_results": True,
    "candidate": (
        "Hold SPY from the close before the last trading day of each month "
        "through the close of the third trading day of the next month "
        "(in-window days: last trading day T-1 plus first three trading "
        "days T+1..T+3; 4 trading days per month boundary), cash otherwise."
    ),
    "window_definition": (
        "A trading day is in-window iff it is the last trading day of its "
        "calendar month OR one of the first three trading days of its "
        "calendar month. Deterministic calendar rule, no lookahead: the "
        "position for day t is established at the close of t-1."
    ),
    "cost": "2 bps per one-way leg; 5 bps/leg reported as stress. ~2 legs/month = ~24 legs/year.",
    "data": "state/history_deep/SPY.parquet daily adjusted close, 1993-01-29 onward.",
    "measures": [
        "mean/median daily SPY return inside vs outside the window, per decade and full-sample, with Welch t-stats",
        "standalone net-of-cost overlay stream CAGR/Sharpe/max-DD, full-sample and per decade",
        "marginal addition to the production portfolio at 5% base / 10% 2x (primary) and 10%/20% (sensitivity)",
        "honest comparison: in-window days vs simply holding SPY all month (share of days vs share of cumulative log return)",
    ],
    "marginal_construction": (
        "candidate[profile] = production control[profile] + allocation * "
        "overlay_stream. No incremental margin financing is charged at 2x: "
        "the overlay is framed as redeploying the trend sleeve's existing "
        "reserve capital, not new borrowing (see limitations for the "
        "trend-on caveat and the negligible ~1bp/yr financing bound)."
    ),
    "objective_class": "return_enhancer",
    "screening_gate": (
        "backtest.promotion.passes_gate_all_cells, return_enhancer, at the "
        "primary allocation, over early_2020_2022 x heldout_2023_plus x "
        "base x 2x (4 cells)."
    ),
    "decision_rule": {
        "long_history_support_all_required": [
            "full-sample Welch t-stat (inside vs outside daily mean) >= 2.0",
            "inside mean > outside mean in at least 3 of 4 decades",
            "2020s inside mean >= 2020s outside mean (anomaly not fully decayed)",
            "standalone net-of-cost CAGR > 0 over the full sample at 2 bps/leg",
        ],
        "mapping": {
            "long_history_support and gate passed": "adopt_experiment_tier_candidate",
            "long_history_support and gate failed": "defer_long_history_supports_screen_failed",
            "no long_history_support": "reject",
        },
    },
    "non_gating_diagnostics": [
        "conditional variant (in-window AND trend off, i.e. SPY under its 200DMA) -- the days the BIL reserve actually exists",
        "5 bps/leg cost stress",
        "10%/20% allocation sensitivity",
    ],
}


def load_spy_deep() -> pd.Series:
    frames = load_parquet(["SPY"], DEEP_HISTORY)
    if "SPY" not in frames:
        raise FileNotFoundError(f"SPY parquet missing from {DEEP_HISTORY}")
    return norm_index(frames["SPY"]["close"]).dropna()


def turn_of_month_mask(index: pd.DatetimeIndex) -> pd.Series:
    """True on the last trading day of each month and the first three
    trading days of each month."""
    codes = pd.PeriodIndex(index, freq="M").asi8
    n = len(codes)
    is_new = np.r_[True, codes[1:] != codes[:-1]]
    starts = np.where(is_new, np.arange(n), 0)
    pos = np.arange(n) - np.maximum.accumulate(starts)
    # Final sample day is never marked "last of month": the month is not
    # complete, so we cannot know yet.
    is_last = np.r_[codes[:-1] != codes[1:], False]
    return pd.Series(is_last | (pos < 3), index=index)


def welch_t(a: pd.Series, b: pd.Series) -> float:
    a, b = a.dropna(), b.dropna()
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return float((a.mean() - b.mean()) / se) if se > 0 else float("nan")


def decade_label(year: int) -> str:
    return f"{year // 10 * 10}s"


def inside_outside_table(ret: pd.Series, mask: pd.Series) -> list[dict]:
    """Per-decade and full-sample inside/outside stats with t-stats."""
    ret = ret.dropna()
    mask = mask.reindex(ret.index)
    rows = []
    groups = [("full", ret.index)] + [
        (label, ret.index[ret.index.year // 10 * 10 == decade])
        for decade, label in sorted(
            {y // 10 * 10: decade_label(y) for y in ret.index.year}.items()
        )
    ]
    log_total_all = float(np.log1p(ret).sum())
    for label, idx in groups:
        r = ret.loc[idx]
        m = mask.loc[idx]
        inside, outside = r[m], r[~m]
        log_r = np.log1p(r)
        rows.append({
            "period": label,
            "from": r.index[0].date().isoformat(),
            "to": r.index[-1].date().isoformat(),
            "n_inside": int(len(inside)),
            "n_outside": int(len(outside)),
            "mean_inside_bps": round(float(inside.mean()) * 1e4, 2),
            "mean_outside_bps": round(float(outside.mean()) * 1e4, 2),
            "mean_all_days_bps": round(float(r.mean()) * 1e4, 2),
            "median_inside_bps": round(float(inside.median()) * 1e4, 2),
            "median_outside_bps": round(float(outside.median()) * 1e4, 2),
            "std_inside_bps": round(float(inside.std()) * 1e4, 1),
            "std_outside_bps": round(float(outside.std()) * 1e4, 1),
            "welch_t_inside_vs_outside": round(welch_t(inside, outside), 2),
            "share_of_days": round(float(m.mean()), 4),
            "share_of_cum_log_return": round(
                float(np.log1p(inside).sum() / log_r.sum()), 4
            ) if abs(float(log_r.sum())) > 1e-12 else None,
        })
    # unused, kept for symmetry of computation intent
    _ = log_total_all
    return rows


def overlay_stream(
    ret: pd.Series, mask: pd.Series, cost_bps_per_leg: float
) -> pd.Series:
    """Net-of-cost return stream of the calendar sleeve at full (1.0) weight."""
    ret = ret.dropna()
    w = mask.reindex(ret.index).astype(float)
    turnover = w.diff().abs()
    turnover.iloc[0] = w.iloc[0]
    return w * ret - turnover * cost_bps_per_leg / 10_000


def per_decade_summaries(stream: pd.Series, label: str) -> list[dict]:
    stream = stream.dropna()
    rows = [returns_summary(stream, f"{label} (full)")]
    for decade in sorted({y // 10 * 10 for y in stream.index.year}):
        sub = stream[stream.index.year // 10 * 10 == decade]
        if len(sub) > 60:
            rows.append(returns_summary(sub, f"{label} ({decade_label(decade)})"))
    return rows


def trend_off_mask(spy_close: pd.Series) -> pd.Series:
    """True when the trend sleeve would be OFF (prior close <= prior 200DMA)
    -- the days the 2x lab's reserve capital actually sits in BIL."""
    prior = spy_close.shift(1)
    avg = prior.rolling(200, min_periods=200).mean()
    return (prior <= avg) & avg.notna()


def evaluate_decision(lh_checks: dict, gate: dict) -> str:
    rule = PRE_REGISTRATION["decision_rule"]["mapping"]
    if all(lh_checks.values()):
        return (
            rule["long_history_support and gate passed"]
            if gate["passed"]
            else rule["long_history_support and gate failed"]
        )
    return rule["no long_history_support"]


def main() -> None:
    spy_close = load_spy_deep()
    spy_ret = spy_close.pct_change(fill_method=None).dropna()
    mask = turn_of_month_mask(spy_ret.index)

    table = inside_outside_table(spy_ret, mask)
    full_row = table[0]

    net = overlay_stream(spy_ret, mask, COST_BPS_PER_LEG)
    net_stress = overlay_stream(spy_ret, mask, STRESS_COST_BPS_PER_LEG)
    standalone = per_decade_summaries(net, "turn-of-month sleeve net 2bps")
    standalone_full = standalone[0]

    legs_per_year = float(
        mask.astype(float).diff().abs().sum() / (len(spy_ret) / TD)
    )

    # Honest comparison: those same 4 days vs simply holding SPY all month.
    spy_full = returns_summary(spy_ret, "SPY buy & hold (full)")
    honest = {
        "note": (
            "The anomaly only matters if in-window returns are "
            "disproportionate, not merely positive. share_of_days vs "
            "share_of_cum_log_return is the direct test."
        ),
        "share_of_days_full": full_row["share_of_days"],
        "share_of_cum_log_return_full": full_row["share_of_cum_log_return"],
        "mean_inside_bps": full_row["mean_inside_bps"],
        "mean_all_days_bps": full_row["mean_all_days_bps"],
        "excess_vs_all_days_bps_per_window_day": round(
            full_row["mean_inside_bps"] - full_row["mean_all_days_bps"], 2
        ),
        "spy_buy_and_hold_full": spy_full,
        "standalone_sleeve_full": standalone_full,
    }

    # Conditional diagnostic: window days on which the trend sleeve is off
    # (reserve capital actually parked in BIL) -- what reserve-only
    # deployment would capture.
    off = trend_off_mask(spy_close).reindex(spy_ret.index).fillna(False)
    cond_mask = mask & off
    cond_net = overlay_stream(spy_ret, cond_mask, COST_BPS_PER_LEG)
    cond_inside = spy_ret[cond_mask]
    conditional = {
        "n_window_days_trend_off": int(cond_mask.sum()),
        "fraction_of_window_days_trend_off": round(
            float(cond_mask.sum() / mask.sum()), 4
        ),
        "mean_bps_window_and_trend_off": round(float(cond_inside.mean()) * 1e4, 2)
        if len(cond_inside) else None,
        "welch_t_vs_all_other_days": round(
            welch_t(cond_inside, spy_ret[~cond_mask]), 2
        ),
        "standalone": returns_summary(
            cond_net, "conditional (window & trend off) net 2bps"
        ),
    }

    # ---- production-portfolio marginal comparison (screening windows) ----
    print("Building production streams (control)...", flush=True)
    streams = build_streams()
    reproduction = validate_control_reproduction(streams)
    for profile, check in reproduction.items():
        print(f"  control repro {profile}: max_abs_diff={check['max_abs_diff']}")
    controls = full_portfolio(streams)

    performance = {}
    cells = []
    sensitivity_cells = []
    for window, slicer in WINDOWS.items():
        rows = []
        for profile in ("base", "2x"):
            control = controls[profile].dropna()
            add = net.reindex(control.index).fillna(0.0)
            candidate = control + ALLOCATION[profile] * add
            candidate_sens = control + ALLOCATION_SENSITIVITY[profile] * add
            control_summary = returns_summary(
                control.loc[slicer], f"{profile} control (production)"
            )
            candidate_summary = returns_summary(
                candidate.loc[slicer],
                f"{profile} + {ALLOCATION[profile]:.0%} turn-of-month overlay",
            )
            sens_summary = returns_summary(
                candidate_sens.loc[slicer],
                f"{profile} + {ALLOCATION_SENSITIVITY[profile]:.0%} turn-of-month overlay",
            )
            rows += [
                {"variant": "control", **control_summary},
                {"variant": "candidate_primary", **candidate_summary},
                {"variant": "candidate_sensitivity", **sens_summary},
            ]
            cells.append((window, profile, control_summary, candidate_summary))
            sensitivity_cells.append((window, profile, control_summary, sens_summary))
        performance[window] = rows

    gate = passes_gate_all_cells(cells, "return_enhancer")
    gate_sensitivity = passes_gate_all_cells(sensitivity_cells, "return_enhancer")

    lh_checks = {
        "welch_t_full_ge_2": full_row["welch_t_inside_vs_outside"] >= 2.0,
        "inside_beats_outside_3_of_4_decades": sum(
            row["mean_inside_bps"] > row["mean_outside_bps"]
            for row in table[1:]
        ) >= 3,
        "2020s_not_fully_decayed": next(
            row for row in table if row["period"] == "2020s"
        )["mean_inside_bps"] >= next(
            row for row in table if row["period"] == "2020s"
        )["mean_outside_bps"],
        "standalone_net_cagr_positive": standalone_full["cagr"] > 0,
    }
    decision = evaluate_decision(lh_checks, gate)

    payload = {
        "decision": decision,
        "decision_meaning": (
            "Screening-tier evidence for the config_2x.yaml experiment tier "
            "(AGENTS.md lighter bar), NOT a hard-gate core promotion. All "
            "data ends 2026-07-22; the 2026-08-13+ frozen final-validation "
            "window is untouched."
        ),
        "pre_registration": PRE_REGISTRATION,
        "control_reproduction": {
            "note": (
                "Unmodified production control rebuilt and diffed against "
                "reports/production_portfolio.json per AGENTS.md's "
                "control-reproduction rule."
            ),
            "result": {
                profile: {
                    "max_abs_diff": check["max_abs_diff"],
                    "abs_diffs": check["abs_diffs"],
                }
                for profile, check in reproduction.items()
            },
        },
        "long_history_evidence": {
            "note": (
                "The whole point of this study: 1993-2026 daily SPY, "
                "per-decade so anomaly decay is visible. The screening "
                "windows (2020+) below are exactly where calendar anomalies "
                "are weakest."
            ),
            "inside_vs_outside": table,
            "long_history_support_checks": lh_checks,
            "legs_per_year_observed": round(legs_per_year, 1),
        },
        "honest_comparison_vs_holding_all_month": honest,
        "standalone_overlay": {
            "net_2bps_per_leg": standalone,
            "net_5bps_per_leg_stress_full": returns_summary(
                net_stress, "turn-of-month sleeve net 5bps stress (full)"
            ),
        },
        "conditional_trend_off_diagnostic": conditional,
        "screening_windows_performance": performance,
        "gate_primary_allocation": gate,
        "gate_sensitivity_allocation": gate_sensitivity,
        "limitations": [
            "Close-to-close daily bars; production trades at ~09:51/10:05 ET, so live entries lag the modeled close by ~16 hours of overnight return on entry days.",
            "The candidate is unconditional (every month), but the deployment context (BIL trend reserve) only has free capital when trend is OFF; on trend-ON days a live overlay would be incremental leverage instead. The conditional diagnostic reports the reserve-only capture; the unconditional stream is the exposure upper bound.",
            "No incremental 2x margin financing charged on the overlay (~5% * 10% * ~19% of days = ~1bp/yr if it were all new borrowing).",
            "Risk-free rate is zero everywhere in this repo (AGENTS.md): the sleeve's cash days earn 0%, understating standalone Sharpe in high-rate eras; in deployment the reserve earns BIL instead.",
            "Calendar anomalies are heavily data-mined territory; a 4-day window choice is standard in the literature (Lakonishok-Smidt style) but not the only possible one, and no multiple-testing correction is applied.",
            "SPY adjusted close embeds dividends at the ex-date; fine for return measurement, but the sleeve would sometimes hold over ex-dates and sometimes not.",
        ],
        "screening_tier_caveat": (
            "early_2020_2022 and heldout_2023_plus are screening windows "
            "(heldout is no longer clean out-of-sample per AGENTS.md), and "
            "the long history is in-sample by construction. Nothing here "
            "touches or tunes on 2026-08-13+ data."
        ),
    }
    out = Path("reports/turn_of_month_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print(f"\nDecision: {decision}")
    print("\nLONG-HISTORY INSIDE VS OUTSIDE")
    print(pd.DataFrame(table).to_string(index=False))
    print("\nSTANDALONE OVERLAY (net 2bps)")
    print(pd.DataFrame(standalone).to_string(index=False))
    print(f"\nGate (primary): passed={gate['passed']}")
    print(f"Gate (sensitivity): passed={gate_sensitivity['passed']}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
