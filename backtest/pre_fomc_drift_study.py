"""Pre-FOMC announcement drift overlay study on SPY, 1994-2026.

Candidate: hold SPY from the close of T-1 (the day before a scheduled FOMC
announcement) to the close of announcement day T -- i.e. earn the
announcement day's close-to-close return -- and cash otherwise, at 2 bps
per one-way leg (2 legs per meeting, ~16 legs/year). Deployment context is
the same as the turn-of-month overlay: temporarily redeploying the 2x
lab's BIL trend reserve during a historically favorable window.

The documented effect (Lucca & Moench 2015) concentrates in the final ~24h
up to the 2pm ET announcement, i.e. close of T-1 to 2pm of T. This repo
trades at ~09:51/10:05 ET with daily bars, so close-to-close over
announcement day T is what is actually DEPLOYABLE, and that is what this
study measures and gates on. The close-to-2pm literature construction
cannot be computed from daily bars at all; the gap is disclosed in
limitations rather than approximated.

FOMC announcement dates are a fixed public list (Federal Reserve
historical calendars), embedded below: SCHEDULED meetings only, 1994
onward (the Fed began post-meeting announcements in Feb 1994), using the
second day of two-day meetings as the announcement date. Unscheduled /
emergency actions are deliberately excluded -- they are reactions to news,
not pre-scheduled windows a calendar overlay could hold into (exclusions
listed in FOMC_EXCLUDED_UNSCHEDULED). The scheduled March 17-18 2020
meeting was cancelled (replaced by the unscheduled March 15 action), so
2020 contributes 7 scheduled announcements, not 8.

Everything in PRE_REGISTRATION was written before results were computed.
Screening-tier evidence only; data ends 2026-07-22, before the 2026-08-13
frozen final-validation window.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import TD, returns_summary
from backtest.promotion import passes_gate_all_cells
from backtest.tsmom_fxe_drop_study import (
    WINDOWS,
    full_portfolio,
    validate_control_reproduction,
)
from backtest.turn_of_month_study import (
    ALLOCATION,
    ALLOCATION_SENSITIVITY,
    COST_BPS_PER_LEG,
    STRESS_COST_BPS_PER_LEG,
    decade_label,
    load_spy_deep,
    overlay_stream,
    per_decade_summaries,
    trend_off_mask,
    turn_of_month_mask,
    welch_t,
)

# Scheduled FOMC announcement dates (second day of two-day meetings).
# Source: Federal Reserve Board historical meeting calendars
# (federalreserve.gov/monetarypolicy/fomccalendars.htm and the historical
# year pages). Embedded as data per study design; validated at runtime
# against the SPY trading calendar (see date_validation in the report).
FOMC_ANNOUNCEMENTS = [
    # 1994
    "1994-02-04", "1994-03-22", "1994-05-17", "1994-07-06",
    "1994-08-16", "1994-09-27", "1994-11-15", "1994-12-20",
    # 1995
    "1995-02-01", "1995-03-28", "1995-05-23", "1995-07-06",
    "1995-08-22", "1995-09-26", "1995-11-15", "1995-12-19",
    # 1996
    "1996-01-31", "1996-03-26", "1996-05-21", "1996-07-03",
    "1996-08-20", "1996-09-24", "1996-11-13", "1996-12-17",
    # 1997
    "1997-02-05", "1997-03-25", "1997-05-20", "1997-07-02",
    "1997-08-19", "1997-09-30", "1997-11-12", "1997-12-16",
    # 1998
    "1998-02-04", "1998-03-31", "1998-05-19", "1998-07-01",
    "1998-08-18", "1998-09-29", "1998-11-17", "1998-12-22",
    # 1999
    "1999-02-03", "1999-03-30", "1999-05-18", "1999-06-30",
    "1999-08-24", "1999-10-05", "1999-11-16", "1999-12-21",
    # 2000
    "2000-02-02", "2000-03-21", "2000-05-16", "2000-06-28",
    "2000-08-22", "2000-10-03", "2000-11-15", "2000-12-19",
    # 2001
    "2001-01-31", "2001-03-20", "2001-05-15", "2001-06-27",
    "2001-08-21", "2001-10-02", "2001-11-06", "2001-12-11",
    # 2002
    "2002-01-30", "2002-03-19", "2002-05-07", "2002-06-26",
    "2002-08-13", "2002-09-24", "2002-11-06", "2002-12-10",
    # 2003
    "2003-01-29", "2003-03-18", "2003-05-06", "2003-06-25",
    "2003-08-12", "2003-09-16", "2003-10-28", "2003-12-09",
    # 2004
    "2004-01-28", "2004-03-16", "2004-05-04", "2004-06-30",
    "2004-08-10", "2004-09-21", "2004-11-10", "2004-12-14",
    # 2005
    "2005-02-02", "2005-03-22", "2005-05-03", "2005-06-30",
    "2005-08-09", "2005-09-20", "2005-11-01", "2005-12-13",
    # 2006
    "2006-01-31", "2006-03-28", "2006-05-10", "2006-06-29",
    "2006-08-08", "2006-09-20", "2006-10-25", "2006-12-12",
    # 2007
    "2007-01-31", "2007-03-21", "2007-05-09", "2007-06-28",
    "2007-08-07", "2007-09-18", "2007-10-31", "2007-12-11",
    # 2008
    "2008-01-30", "2008-03-18", "2008-04-30", "2008-06-25",
    "2008-08-05", "2008-09-16", "2008-10-29", "2008-12-16",
    # 2009
    "2009-01-28", "2009-03-18", "2009-04-29", "2009-06-24",
    "2009-08-12", "2009-09-23", "2009-11-04", "2009-12-16",
    # 2010
    "2010-01-27", "2010-03-16", "2010-04-28", "2010-06-23",
    "2010-08-10", "2010-09-21", "2010-11-03", "2010-12-14",
    # 2011
    "2011-01-26", "2011-03-15", "2011-04-27", "2011-06-22",
    "2011-08-09", "2011-09-21", "2011-11-02", "2011-12-13",
    # 2012
    "2012-01-25", "2012-03-13", "2012-04-25", "2012-06-20",
    "2012-08-01", "2012-09-13", "2012-10-24", "2012-12-12",
    # 2013
    "2013-01-30", "2013-03-20", "2013-05-01", "2013-06-19",
    "2013-07-31", "2013-09-18", "2013-10-30", "2013-12-18",
    # 2014
    "2014-01-29", "2014-03-19", "2014-04-30", "2014-06-18",
    "2014-07-30", "2014-09-17", "2014-10-29", "2014-12-17",
    # 2015
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17",
    "2015-07-29", "2015-09-17", "2015-10-28", "2015-12-16",
    # 2016
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15",
    "2016-07-27", "2016-09-21", "2016-11-02", "2016-12-14",
    # 2017
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14",
    "2017-07-26", "2017-09-20", "2017-11-01", "2017-12-13",
    # 2018
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13",
    "2018-08-01", "2018-09-26", "2018-11-08", "2018-12-19",
    # 2019
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19",
    "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    # 2020 (scheduled March 17-18 meeting cancelled; 7 scheduled)
    "2020-01-29", "2020-04-29", "2020-06-10", "2020-07-29",
    "2020-09-16", "2020-11-05", "2020-12-16",
    # 2021
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026 (published calendar; sample data ends 2026-07-22)
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

FOMC_EXCLUDED_UNSCHEDULED = [
    "1994-04-18", "1998-10-15", "2001-01-03", "2001-04-18", "2001-09-17",
    "2007-08-17", "2008-01-22", "2008-10-08", "2010-05-09",
    "2020-03-03", "2020-03-15",
]

PRE_REGISTRATION = {
    "registered_before_results": True,
    "candidate": (
        "Hold SPY from the close of T-1 (day before a scheduled FOMC "
        "announcement) to the close of announcement day T, cash otherwise. "
        "One in-market day per scheduled meeting."
    ),
    "event_list": (
        "FOMC_ANNOUNCEMENTS embedded in this file: scheduled meetings only, "
        "1994 onward, second day of two-day meetings; unscheduled/emergency "
        "actions excluded (FOMC_EXCLUDED_UNSCHEDULED)."
    ),
    "cost": "2 bps per one-way leg; 2 legs/meeting = ~16 legs/year. 5 bps/leg stress reported.",
    "data": "state/history_deep/SPY.parquet daily adjusted close; sample restricted to 1994-01-01 onward.",
    "measures": [
        "announcement-day close-to-close return vs all non-announcement days, per decade (1990s=1994-99) and full-sample, Welch t-stats",
        "standalone net-of-cost overlay CAGR/Sharpe/max-DD, full and per decade",
        "marginal addition to the production portfolio at 5% base / 10% 2x (primary), 10%/20% (sensitivity)",
        "overlap with the turn-of-month overlay (shared reserve capital)",
    ],
    "deployability_note": (
        "Close-to-close on day T is what the repo's ~09:51/10:05 ET daily "
        "trade times can actually implement (enter at/after the prior "
        "close via a next-morning order is NOT equivalent; see "
        "limitations). The literature's close-of-T-1-to-2pm-of-T window "
        "is not computable from daily bars and is not gated on."
    ),
    "marginal_construction": (
        "candidate[profile] = production control[profile] + allocation * "
        "overlay_stream; no incremental 2x margin financing (reserve "
        "redeployment framing, same as the turn-of-month study)."
    ),
    "objective_class": "return_enhancer",
    "screening_gate": (
        "backtest.promotion.passes_gate_all_cells, return_enhancer, "
        "primary allocation, early_2020_2022 x heldout_2023_plus x base x "
        "2x."
    ),
    "decision_rule": {
        "long_history_support_all_required": [
            "full-sample (1994+) Welch t-stat (announcement day vs other days) >= 2.0",
            "announcement-day mean > other-day mean in at least 3 of 4 decades",
            "2020s announcement-day mean >= 2020s other-day mean (not fully decayed)",
            "standalone net-of-cost CAGR > 0 over the full sample at 2 bps/leg",
        ],
        "mapping": {
            "long_history_support and gate passed": "adopt_experiment_tier_candidate",
            "long_history_support and gate failed": "defer_long_history_supports_screen_failed",
            "no long_history_support": "reject",
        },
    },
    "non_gating_diagnostics": [
        "pre-2015 vs post-2015 split (documented decay boundary in the literature)",
        "conditional variant (announcement day AND trend off)",
        "5 bps/leg cost stress; 10%/20% allocation sensitivity",
    ],
}


def validate_dates(index: pd.DatetimeIndex) -> tuple[pd.DatetimeIndex, dict]:
    """Every embedded date must be a real trading day in the SPY calendar.

    Dates beyond the sample end are expected (forward-published calendar)
    and dropped; a date INSIDE the sample span that is not a trading day
    would mean a transcription error and is loudly reported.
    """
    dates = pd.DatetimeIndex([pd.Timestamp(d) for d in FOMC_ANNOUNCEMENTS])
    if dates.duplicated().any():
        raise ValueError("duplicate FOMC dates embedded")
    in_span = dates[(dates >= index[0]) & (dates <= index[-1])]
    beyond = dates[dates > index[-1]]
    matched = in_span[in_span.isin(index)]
    unmatched = in_span[~in_span.isin(index)]
    per_year = pd.Series(matched.year).value_counts().sort_index()
    dow = pd.Series(matched.dayofweek).value_counts().sort_index()
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    validation = {
        "n_embedded": len(dates),
        "n_within_sample_span": len(in_span),
        "n_matched_trading_days": len(matched),
        "n_beyond_sample_end": len(beyond),
        "unmatched_within_span": [d.date().isoformat() for d in unmatched],
        "per_year_counts": {int(y): int(c) for y, c in per_year.items()},
        "years_not_eq_8": {
            int(y): int(c) for y, c in per_year.items() if c != 8
        },
        "day_of_week_distribution": {
            dow_names[int(d)]: int(c) for d, c in dow.items()
        },
    }
    if len(unmatched):
        print(f"WARNING: embedded FOMC dates not on the SPY trading calendar: "
              f"{validation['unmatched_within_span']}")
    return matched, validation


def event_mask(index: pd.DatetimeIndex, events: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(index.isin(events), index=index)


def inside_outside_by_period(ret: pd.Series, mask: pd.Series) -> list[dict]:
    ret = ret.dropna()
    mask = mask.reindex(ret.index)
    periods = [("full", slice(None))] + [
        (decade_label(d), slice(f"{max(d, 1994)}-01-01", f"{d + 9}-12-31"))
        for d in sorted({y // 10 * 10 for y in ret.index.year})
    ] + [
        ("pre_2015", slice(None, "2014-12-31")),
        ("post_2015", slice("2015-01-01", None)),
    ]
    rows = []
    for label, slicer in periods:
        r = ret.loc[slicer]
        m = mask.loc[slicer]
        inside, outside = r[m], r[~m]
        if len(inside) < 4:
            continue
        one_sample_t = float(
            inside.mean() / (inside.std(ddof=1) / np.sqrt(len(inside)))
        ) if inside.std(ddof=1) > 0 else float("nan")
        rows.append({
            "period": label,
            "from": r.index[0].date().isoformat(),
            "to": r.index[-1].date().isoformat(),
            "n_announcement_days": int(len(inside)),
            "n_other_days": int(len(outside)),
            "mean_announcement_bps": round(float(inside.mean()) * 1e4, 2),
            "mean_other_bps": round(float(outside.mean()) * 1e4, 2),
            "median_announcement_bps": round(float(inside.median()) * 1e4, 2),
            "median_other_bps": round(float(outside.median()) * 1e4, 2),
            "std_announcement_bps": round(float(inside.std()) * 1e4, 1),
            "hit_rate": round(float((inside > 0).mean()), 3),
            "welch_t_vs_other_days": round(welch_t(inside, outside), 2),
            "one_sample_t": round(one_sample_t, 2),
            "cum_return_announcement_days_only": round(
                float((1 + inside).prod() - 1), 4
            ),
        })
    return rows


def overlap_analysis(
    ret: pd.Series,
    fomc_mask: pd.Series,
    net_fomc: pd.Series,
) -> dict:
    """Turn-of-month windows containing FOMC announcement days -- a
    combined deployment must not double-count the same reserve capital."""
    tom_mask = turn_of_month_mask(ret.index)
    net_tom = overlay_stream(ret, tom_mask, COST_BPS_PER_LEG)
    both = fomc_mask & tom_mask
    n_fomc = int(fomc_mask.sum())
    corr = float(net_tom.corr(net_fomc))
    union_mask = fomc_mask | tom_mask
    net_union = overlay_stream(ret, union_mask, COST_BPS_PER_LEG)
    return {
        "note": (
            "Both overlays draw on the same trend-reserve capital. On "
            "overlap days a combined deployment must hold ONE SPY position "
            "(union), not the sum of two allocations."
        ),
        "n_fomc_announcement_days": n_fomc,
        "n_fomc_days_inside_turn_of_month_window": int(both.sum()),
        "pct_fomc_days_inside_tom_window": round(float(both.sum() / n_fomc), 4),
        "expected_pct_if_independent": round(float(tom_mask.mean()), 4),
        "daily_overlay_stream_correlation": round(corr, 4),
        "union_overlay_standalone_net_2bps": returns_summary(
            net_union, "union (ToM | FOMC) overlay net 2bps"
        ),
        "union_days_per_year": round(
            float(union_mask.sum() / (len(ret) / TD)), 1
        ),
    }


def main() -> None:
    spy_close = load_spy_deep()
    spy_ret = spy_close.pct_change(fill_method=None).dropna()
    spy_ret = spy_ret.loc["1994-01-01":]

    events, validation = validate_dates(spy_ret.index)
    mask = event_mask(spy_ret.index, events)

    table = inside_outside_by_period(spy_ret, mask)
    full_row = table[0]
    decade_rows = [r for r in table if r["period"].endswith("s")]

    net = overlay_stream(spy_ret, mask, COST_BPS_PER_LEG)
    net_stress = overlay_stream(spy_ret, mask, STRESS_COST_BPS_PER_LEG)
    standalone = per_decade_summaries(net, "pre-FOMC sleeve net 2bps")
    standalone_full = standalone[0]

    off = trend_off_mask(spy_close).reindex(spy_ret.index).fillna(False)
    cond_mask = mask & off
    cond_inside = spy_ret[cond_mask]
    conditional = {
        "n_announcement_days_trend_off": int(cond_mask.sum()),
        "fraction_of_announcement_days_trend_off": round(
            float(cond_mask.sum() / mask.sum()), 4
        ),
        "mean_bps_announcement_and_trend_off": round(
            float(cond_inside.mean()) * 1e4, 2
        ) if len(cond_inside) else None,
        "welch_t_vs_all_other_days": round(
            welch_t(cond_inside, spy_ret[~cond_mask]), 2
        ),
    }

    overlap = overlap_analysis(spy_ret, mask, net)

    # ---- production-portfolio marginal comparison ----
    print("Building production streams (control)...", flush=True)
    from backtest.production_portfolio import build_streams
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
                f"{profile} + {ALLOCATION[profile]:.0%} pre-FOMC overlay",
            )
            sens_summary = returns_summary(
                candidate_sens.loc[slicer],
                f"{profile} + {ALLOCATION_SENSITIVITY[profile]:.0%} pre-FOMC overlay",
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

    row_2020s = next((r for r in table if r["period"] == "2020s"), None)
    lh_checks = {
        "welch_t_full_ge_2": full_row["welch_t_vs_other_days"] >= 2.0,
        "announcement_beats_other_3_of_4_decades": sum(
            r["mean_announcement_bps"] > r["mean_other_bps"]
            for r in decade_rows
        ) >= 3,
        "2020s_not_fully_decayed": (
            row_2020s is not None
            and row_2020s["mean_announcement_bps"] >= row_2020s["mean_other_bps"]
        ),
        "standalone_net_cagr_positive": standalone_full["cagr"] > 0,
    }
    mapping = PRE_REGISTRATION["decision_rule"]["mapping"]
    if all(lh_checks.values()):
        decision = (
            mapping["long_history_support and gate passed"]
            if gate["passed"]
            else mapping["long_history_support and gate failed"]
        )
    else:
        decision = mapping["no long_history_support"]

    payload = {
        "decision": decision,
        "decision_meaning": (
            "Screening-tier evidence for the config_2x.yaml experiment tier "
            "(AGENTS.md lighter bar), NOT a hard-gate core promotion. All "
            "data ends 2026-07-22; the 2026-08-13+ frozen final-validation "
            "window is untouched."
        ),
        "pre_registration": PRE_REGISTRATION,
        "date_validation": validation,
        "excluded_unscheduled_dates": FOMC_EXCLUDED_UNSCHEDULED,
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
                "1994-2026 daily SPY, per-decade plus the pre/post-2015 "
                "split where the literature locates the decay. The "
                "screening windows (2020+) below are exactly where this "
                "anomaly is documented to be weakest."
            ),
            "announcement_vs_other_days": table,
            "long_history_support_checks": lh_checks,
        },
        "standalone_overlay": {
            "net_2bps_per_leg": standalone,
            "net_5bps_per_leg_stress_full": returns_summary(
                net_stress, "pre-FOMC sleeve net 5bps stress (full)"
            ),
        },
        "conditional_trend_off_diagnostic": conditional,
        "overlap_with_turn_of_month": overlap,
        "screening_windows_performance": performance,
        "gate_primary_allocation": gate,
        "gate_sensitivity_allocation": gate_sensitivity,
        "limitations": [
            "The documented drift concentrates in the final ~24h up to the 2pm ET announcement (close T-1 to 2pm T). Daily bars cannot measure that; this study measures close-to-close on day T, which includes the typically noisy 2pm-4pm post-announcement reaction. This is deliberately the deployable construction, but it is a coarser proxy and will understate (and add variance to) the literature effect.",
            "Even close-to-close is idealized vs live execution: the repo trades ~09:51/10:05 ET, so a live entry on T-1 morning would also hold T-1's intraday session, and an entry at T's open would miss T-1's overnight move. Neither leg matches the modeled close-of-T-1 entry exactly.",
            "FOMC dates were embedded from the Fed's published historical calendars and validated only as trading days (date_validation); a transcribed date that is off by one valid trading day would not be caught mechanically.",
            "2026 dates come from the Fed's forward-published calendar.",
            "Unconditional overlay vs trend-reserve deployment: same caveat as the turn-of-month study -- reserve capital only exists when trend is OFF (see conditional diagnostic).",
            "No incremental 2x margin financing charged (~8 in-market days/year at 10% -- well under 1bp/yr).",
            "Risk-free rate is zero everywhere in this repo (AGENTS.md); the sleeve is in cash ~97% of days.",
        ],
        "screening_tier_caveat": (
            "early_2020_2022 and heldout_2023_plus are screening windows "
            "(heldout is no longer clean out-of-sample per AGENTS.md), and "
            "the long history is in-sample by construction. Nothing here "
            "touches or tunes on 2026-08-13+ data."
        ),
    }
    out = Path("reports/pre_fomc_drift_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print(f"\nDecision: {decision}")
    print("\nANNOUNCEMENT VS OTHER DAYS")
    print(pd.DataFrame(table).to_string(index=False))
    print("\nSTANDALONE OVERLAY (net 2bps)")
    print(pd.DataFrame(standalone).to_string(index=False))
    print("\nOVERLAP WITH TURN-OF-MONTH")
    print(json.dumps({k: v for k, v in overlap.items() if k != "union_overlay_standalone_net_2bps"}, indent=2))
    print(f"\nGate (primary): passed={gate['passed']}")
    print(f"Gate (sensitivity): passed={gate_sensitivity['passed']}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
