"""FOMC x trend-off robustness study: try to BREAK the +58bps cell.

CONDITIONING DISCLOSURE -- READ THIS FIRST
------------------------------------------
The hypothesis under test (scheduled FOMC announcement days are unusually
good for SPY *when the trend signal is off*) was NOT formed independently.
It is the one cell of `backtest/pre_fomc_drift_study.py`'s non-gating
diagnostics that looked good (+58.45 bps, Welch t=2.29, n=57, 1994-2026)
after the primary pre-FOMC hypothesis had already failed its screen
(post-2015 t=0.21; decision `defer_long_history_supports_screen_failed`).
The cell was FOUND by exploring the same historical data this study now
runs on. Re-measuring +58bps here would confirm nothing: conditioning on
a data-mined cell means in-sample "confirmation" is close to worthless.

This study therefore does not try to confirm the effect. It tries to
break it, with subperiod stability checks, placebo tests (trend-ON FOMC
days; random-matched non-FOMC trend-off day-sets), a pre-registered
alternative entry window, and an economic-materiality calculation. Even
if every test below is survived, the only real confirmation is live
forward performance in the frozen 2026-08-13+ validation window. All
data here ends 2026-07-22; nothing touches or tunes on frozen data.

Reuses (does not re-derive):
- `FOMC_ANNOUNCEMENTS` scheduled-date list + `validate_dates` from
  `backtest.pre_fomc_drift_study`;
- `state/history_deep/SPY.parquet` loading via
  `backtest.turn_of_month_study.load_spy_deep`;
- the trend definition via `backtest.turn_of_month_study.trend_off_mask`,
  which mirrors `engine/portfolio.py::trend_targets` exactly (yesterday's
  close vs yesterday's 200-day SMA, no peeking). No variants, no tuning.

Candidate mechanic (fixed): on a scheduled FOMC announcement day T with
trend off (known at the T-1 close), hold the 2x lab's BIL trend reserve
in SPY from the T-1 close to the T close, at 2 bps per one-way leg.

Everything in PRE_REGISTRATION was written before any results were
computed. Research only -- no config or engine changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.pre_fomc_drift_study import (
    FOMC_ANNOUNCEMENTS,
    FOMC_EXCLUDED_UNSCHEDULED,
    validate_dates,
)
from backtest.turn_of_month_study import (
    COST_BPS_PER_LEG,
    STRESS_COST_BPS_PER_LEG,
    decade_label,
    load_spy_deep,
    trend_off_mask,
    welch_t,
)

# 2x lab reserve size: sleeves.trend (0.20) * gross_leverage (2.0), both
# from config_2x.yaml. When trend is off the lab targets 40% of equity in
# BIL -- that is the capital an overlay could redeploy. Base has no
# reserve symbol (idle cash) and is not the deployment context.
TREND_SLEEVE_WEIGHT = 0.20
GROSS_LEVERAGE_2X = 2.0
RESERVE_WEIGHT_2X = TREND_SLEEVE_WEIGHT * GROSS_LEVERAGE_2X

N_PERMUTATION_DRAWS = 10_000
PERMUTATION_SEED = 20260812

PRE_REGISTRATION = {
    "registered_before_results": True,
    "conditioning_disclosure": (
        "The FOMC x trend-off cell was discovered as a non-gating "
        "diagnostic of backtest/pre_fomc_drift_study.py, i.e. by exploring "
        "the same 1994-2026 SPY history this study runs on, after the "
        "primary pre-FOMC hypothesis had already failed post-2015. "
        "In-sample re-confirmation is therefore nearly worthless. This "
        "battery can only REJECT the effect or leave it standing for the "
        "one test that counts: live forward performance post-2026-08-13."
    ),
    "hypothesis_under_attack": (
        "SPY close(T-1)->close(T) return on scheduled FOMC announcement "
        "days with the engine's trend signal OFF (yesterday's close <= "
        "yesterday's 200DMA, per engine/portfolio.py trend_targets, "
        "computed via backtest.turn_of_month_study.trend_off_mask) is "
        "positive and FOMC-specific, not merely a high-volatility "
        "trend-off artifact. Prior-study values to reproduce exactly "
        "before any new computation: n=57, mean=+58.45 bps, Welch "
        "t=2.29 vs all other days (1994-2026, data to 2026-07-22)."
    ),
    "trend_definition": (
        "trend_off_mask(spy_close): prior close <= 200-day rolling mean "
        "of prior closes -- the exact engine construction. No alternative "
        "MA lengths, no bands, no variants anywhere in this study."
    ),
    "battery": {
        "1_subperiod_stability": (
            "The FOMC x trend-off cell per decade (1990s=1994-99, 2000s, "
            "2010s, 2020s) and pre/post-2015. Per cell: exact n, mean/"
            "median bps, hit rate, one-sample t, Welch t vs non-FOMC "
            "trend-off days in the same period (the specificity "
            "comparison), and Welch t vs all other days (continuity with "
            "the prior study). n will be small; reported as-is, no "
            "smoothing, no cell dropped."
        ),
        "2a_placebo_trend_on": (
            "FOMC days with trend ON, full sample and pre/post-2015. If "
            "the effect is not specific to trend-off, this cell should "
            "look similar; per the prior study it should be ~flat "
            "post-2015."
        ),
        "2b_placebo_random_matched": (
            "Primary test of the battery. Draw 57 days uniformly WITHOUT "
            "replacement from non-FOMC trend-off days (1994+), compute "
            "the mean return; repeat 10,000 times, seed 20260812. "
            "One-sided p = (1 + #{draw mean >= observed mean}) / (10000 "
            "+ 1). Secondary, stricter variant: year-matched draws -- "
            "each draw takes, per calendar year, exactly as many "
            "non-FOMC trend-off days as the real cell has FOMC x "
            "trend-off days in that year (controls for regime/vol "
            "clustering, since trend-off days concentrate in bear "
            "markets). Both p-values computed once, before any decision "
            "logic runs; no re-draws, no seed changes."
        ),
        "2c_placebo_cpi": (
            "SKIPPED, with disclosure: no authoritative embedded list of "
            "BLS CPI release dates (2000+) exists in this repo, and "
            "hand-transcribing ~320 exact release dates from memory has "
            "an error risk that would make the placebo itself "
            "untrustworthy (the FOMC list was embedded from the Fed's "
            "published calendars and calendar-validated; no equivalent "
            "verified source is embedded here). The random-matched "
            "permutation (2b) addresses the same alternative hypothesis "
            "-- 'any trend-off day looks like this' -- more directly."
        ),
        "3_window_sensitivity_not_tuning": (
            "Primary window: close(T-1)->close(T) (day-T return), trend "
            "conditioned at the T-1-close decision point (trend_off_mask "
            "at T). ONE pre-registered alternative: close(T-2)->close(T) "
            "(two-day compounded return over T-1 and T), trend "
            "conditioned at the T-2-close decision point (trend_off_mask "
            "at T-1). Divergence between the two is evidence of "
            "FRAGILITY and is reported as such; under no circumstances "
            "is the better-looking window selected or emphasized."
        ),
        "4_economic_materiality": (
            "Expected annual contribution at the 2x lab reserve size "
            "(0.20 trend sleeve x 2.0 gross leverage = 40% of equity in "
            "BIL when trend is off): reserve_weight x realized events/"
            "year x net-per-event bps, computed with (a) the full-sample "
            "mean and (b) the post-2015 mean, each at 2 bps/leg (4 bps "
            "round trip) and at the 5 bps/leg stress, plus an explicit "
            "~2 bps/event reserve-yield-forgone adjustment (BIL at a 5% "
            "short rate; the repo's rf=0 convention hides this). Events/"
            "year from realized joint frequency, full sample and last 5 "
            "years. Stated plainly in bps/yr and dollars on the $10,000 "
            "account."
        ),
    },
    "cost": "2 bps per one-way leg (4 bps per event); 5 bps/leg stress reported.",
    "data": (
        "state/history_deep/SPY.parquet daily adjusted close via "
        "load_spy_deep; returns restricted to 1994-01-01+ (Fed began "
        "post-meeting announcements Feb 1994); trend mask computed on the "
        "full 1993+ series so the 200DMA has no warm-up gap."
    ),
    "decision_rule": {
        "written_before_results": True,
        "survival_requires_all": [
            "perm_p_unstratified < 0.05",
            "perm_p_year_matched < 0.05",
            "FOMC x trend-ON full-sample mean < FOMC x trend-OFF full-sample mean (specificity)",
            "FOMC x trend-OFF mean > 0 in at least 3 of 4 decades",
            "alternative T-2->T window mean > 0 (sign consistency, not magnitude)",
        ],
        "mapping": {
            "all_pass": "battery_survived_forward_validation_required",
            "any_fail": "fails_robustness_battery_reject",
        },
        "meaning": (
            "battery_survived_forward_validation_required does NOT "
            "authorize any config change, experiment registration, or "
            "paper deployment. Because the hypothesis is data-mined, "
            "surviving an in-sample battery only earns the right to be "
            "judged on frozen forward data (2026-08-13+) it has never "
            "seen -- e.g. a registered forward observation of FOMC x "
            "trend-off days as they occur. fails_robustness_battery_"
            "reject closes the follow-up flagged by the prior study."
        ),
    },
}


def cell_stats(inside: pd.Series, specificity_pool: pd.Series,
               all_other: pd.Series) -> dict:
    """Exact n / mean / t stats for one cell. No minimum-n suppression:
    tiny cells are reported with their tiny n, per pre-registration."""
    n = int(len(inside))
    row = {
        "n": n,
        "mean_bps": round(float(inside.mean()) * 1e4, 2) if n else None,
        "median_bps": round(float(inside.median()) * 1e4, 2) if n else None,
        "std_bps": round(float(inside.std(ddof=1)) * 1e4, 1) if n > 1 else None,
        "hit_rate": round(float((inside > 0).mean()), 3) if n else None,
    }
    if n > 1 and inside.std(ddof=1) > 0:
        row["one_sample_t"] = round(
            float(inside.mean() / (inside.std(ddof=1) / np.sqrt(n))), 2
        )
    else:
        row["one_sample_t"] = None
    row["welch_t_vs_nonfomc_trend_off"] = (
        round(welch_t(inside, specificity_pool), 2) if n > 1 else None
    )
    row["n_nonfomc_trend_off"] = int(len(specificity_pool))
    row["mean_nonfomc_trend_off_bps"] = (
        round(float(specificity_pool.mean()) * 1e4, 2)
        if len(specificity_pool) else None
    )
    row["welch_t_vs_all_other_days"] = (
        round(welch_t(inside, all_other), 2) if n > 1 else None
    )
    return row


def subperiod_table(ret: pd.Series, fomc_off: pd.Series, off: pd.Series,
                    fomc: pd.Series) -> list[dict]:
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
        cell = r[fomc_off.loc[slicer]]
        pool = r[(off & ~fomc).loc[slicer]]
        other = r[~fomc_off.loc[slicer]]
        rows.append({
            "period": label,
            "from": r.index[0].date().isoformat(),
            "to": r.index[-1].date().isoformat(),
            **cell_stats(cell, pool, other),
        })
    return rows


def permutation_test(ret: pd.Series, cell_idx: pd.DatetimeIndex,
                     pool_idx: pd.DatetimeIndex) -> dict:
    """Where does the real cell's mean fall among random same-size sets of
    non-FOMC trend-off days? Unstratified (primary) and year-matched
    (secondary) draws, both fixed-seed, computed once."""
    observed = float(ret.loc[cell_idx].mean())
    pool = ret.loc[pool_idx].to_numpy()
    k = len(cell_idx)

    rng = np.random.default_rng(PERMUTATION_SEED)
    means_unstrat = np.empty(N_PERMUTATION_DRAWS)
    for i in range(N_PERMUTATION_DRAWS):
        means_unstrat[i] = pool[
            rng.choice(len(pool), size=k, replace=False)
        ].mean()

    # Year-matched: per draw, take from each calendar year exactly as many
    # pool days as the real cell has in that year.
    cell_years = pd.Series(cell_idx.year).value_counts().sort_index()
    pool_by_year = {
        y: ret.loc[pool_idx[pool_idx.year == y]].to_numpy()
        for y in cell_years.index
    }
    insufficient = {
        int(y): {"needed": int(c), "available": len(pool_by_year[y])}
        for y, c in cell_years.items() if len(pool_by_year[y]) < c
    }
    if insufficient:
        raise RuntimeError(
            f"year-matched pool insufficient (would need replacement "
            f"sampling, not pre-registered): {insufficient}"
        )
    rng2 = np.random.default_rng(PERMUTATION_SEED + 1)
    means_matched = np.empty(N_PERMUTATION_DRAWS)
    for i in range(N_PERMUTATION_DRAWS):
        total = 0.0
        for y, c in cell_years.items():
            arr = pool_by_year[y]
            total += arr[rng2.choice(len(arr), size=c, replace=False)].sum()
        means_matched[i] = total / k

    def summarize(means: np.ndarray) -> dict:
        pcts = [1, 5, 25, 50, 75, 95, 99]
        return {
            "n_draws": N_PERMUTATION_DRAWS,
            "mean_of_draw_means_bps": round(float(means.mean()) * 1e4, 2),
            "std_of_draw_means_bps": round(float(means.std(ddof=1)) * 1e4, 2),
            "percentiles_bps": {
                f"p{p}": round(float(np.percentile(means, p)) * 1e4, 2)
                for p in pcts
            },
            "min_bps": round(float(means.min()) * 1e4, 2),
            "max_bps": round(float(means.max()) * 1e4, 2),
            "one_sided_p_ge_observed": round(
                float((1 + (means >= observed).sum())
                      / (N_PERMUTATION_DRAWS + 1)), 4
            ),
        }

    return {
        "observed_cell_mean_bps": round(observed * 1e4, 2),
        "cell_size": k,
        "pool_size": len(pool_idx),
        "pool_definition": "non-FOMC trend-off days, 1994-01-01+",
        "seed": PERMUTATION_SEED,
        "unstratified_primary": summarize(means_unstrat),
        "year_matched_secondary": {
            "note": (
                "Each draw matches the real cell's per-year day counts, "
                "so regime clustering (bear-market vol) is controlled, "
                "not just trend-off membership."
            ),
            **summarize(means_matched),
        },
    }


def window_sensitivity(ret: pd.Series, off: pd.Series,
                       events: pd.DatetimeIndex) -> dict:
    """Primary T-1->T vs pre-registered alternative T-2->T. Divergence is
    fragility evidence; neither window may be 'picked'."""
    idx = ret.index
    pos = idx.get_indexer(events)
    pos = pos[pos >= 1]  # need T-1 in the return index for the alt window

    # Primary: day-T return, trend conditioned at T (info through T-1 close)
    prim_mask = off.iloc[pos].to_numpy()
    prim = ret.iloc[pos[prim_mask]]

    # Alternative: (1+r_{T-1})(1+r_T)-1, trend conditioned at T-1 (info
    # through T-2 close -- the entry decision at the T-2 close)
    alt_mask = off.iloc[pos - 1].to_numpy()
    p_alt = pos[alt_mask]
    alt = pd.Series(
        (1 + ret.iloc[p_alt - 1].to_numpy()) * (1 + ret.iloc[p_alt].to_numpy()) - 1,
        index=idx[p_alt],
    )

    def one_t(s: pd.Series) -> float | None:
        if len(s) < 2 or s.std(ddof=1) == 0:
            return None
        return round(float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))), 2)

    round_trip_bps = 2 * COST_BPS_PER_LEG
    return {
        "note": (
            "Both windows cost one round trip (2 legs). The alternative "
            "conditions trend at its own entry decision point (T-2 "
            "close), so its event set can differ slightly from the "
            "primary's -- that is the honest construction, not a bug."
        ),
        "primary_T1_close_to_T_close": {
            "n": int(len(prim)),
            "mean_bps": round(float(prim.mean()) * 1e4, 2),
            "median_bps": round(float(prim.median()) * 1e4, 2),
            "one_sample_t": one_t(prim),
            "mean_net_2bps_leg_bps": round(
                float(prim.mean()) * 1e4 - round_trip_bps, 2
            ),
        },
        "alternative_T2_close_to_T_close": {
            "n": int(len(alt)),
            "mean_bps": round(float(alt.mean()) * 1e4, 2),
            "median_bps": round(float(alt.median()) * 1e4, 2),
            "one_sample_t": one_t(alt),
            "mean_net_2bps_leg_bps": round(
                float(alt.mean()) * 1e4 - round_trip_bps, 2
            ),
        },
        "n_events_in_both_conditionings": int((prim_mask & alt_mask).sum()),
        "n_events_in_exactly_one": int((prim_mask ^ alt_mask).sum()),
    }


def materiality(ret: pd.Series, off: pd.Series, fomc_off: pd.Series,
                mean_full_bps: float, mean_post2015_bps: float) -> dict:
    years_full = (ret.index[-1] - ret.index[0]).days / 365.25
    last5_start = ret.index[-1] - pd.DateOffset(years=5)
    last5 = fomc_off.loc[last5_start:]
    post2015 = ret.loc["2015-01-01":]

    events_yr_full = float(fomc_off.sum()) / years_full
    events_yr_last5 = float(last5.sum()) / 5.0
    reserve_yield_forgone_bps = 2.0  # BIL ~5% / 252 per in-market day

    def contribution(mean_bps: float, cost_leg: float,
                     events_yr: float, yield_adj: bool) -> dict:
        net = mean_bps - 2 * cost_leg - (reserve_yield_forgone_bps if yield_adj else 0.0)
        annual_bps = RESERVE_WEIGHT_2X * events_yr * net
        return {
            "net_per_event_bps": round(net, 2),
            "expected_annual_contribution_bps_of_equity": round(annual_bps, 2),
            "expected_annual_dollars_on_10k": round(annual_bps / 1e4 * 10_000, 2),
        }

    return {
        "reserve_weight_2x": {
            "trend_sleeve_weight": TREND_SLEEVE_WEIGHT,
            "gross_leverage": GROSS_LEVERAGE_2X,
            "reserve_pct_of_equity_when_trend_off": RESERVE_WEIGHT_2X,
            "source": "config_2x.yaml paper_portfolio.sleeves.trend x gross_leverage",
        },
        "event_frequency": {
            "trend_off_fraction_full": round(float(off.mean()), 4),
            "trend_off_fraction_post_2015": round(
                float(off.loc["2015-01-01":].mean()), 4
            ),
            "fomc_x_trend_off_events_total": int(fomc_off.sum()),
            "events_per_year_full_sample": round(events_yr_full, 2),
            "events_per_year_last_5y": round(events_yr_last5, 2),
            "events_last_5y": int(last5.sum()),
            "post_2015_span_note": (
                f"post-2015 span {post2015.index[0].date()} to "
                f"{post2015.index[-1].date()}"
            ),
        },
        "scenarios": {
            "full_sample_mean_2bps_leg_last5y_freq": contribution(
                mean_full_bps, COST_BPS_PER_LEG, events_yr_last5, False
            ),
            "full_sample_mean_2bps_leg_last5y_freq_bil_yield_adjusted": contribution(
                mean_full_bps, COST_BPS_PER_LEG, events_yr_last5, True
            ),
            "post_2015_mean_2bps_leg_last5y_freq": contribution(
                mean_post2015_bps, COST_BPS_PER_LEG, events_yr_last5, False
            ),
            "post_2015_mean_2bps_leg_last5y_freq_bil_yield_adjusted": contribution(
                mean_post2015_bps, COST_BPS_PER_LEG, events_yr_last5, True
            ),
            "full_sample_mean_5bps_leg_stress_last5y_freq": contribution(
                mean_full_bps, STRESS_COST_BPS_PER_LEG, events_yr_last5, False
            ),
            "full_sample_mean_full_history_freq_2bps_leg": contribution(
                mean_full_bps, COST_BPS_PER_LEG, events_yr_full, False
            ),
        },
        "plain_statement": None,  # filled in main() with the computed numbers
    }


def main() -> None:
    spy_close = load_spy_deep()
    spy_ret = spy_close.pct_change(fill_method=None).dropna()
    # Trend mask on the FULL 1993+ series (no 200DMA warm-up gap inside
    # the sample), then restrict returns to the announcement era.
    off_full = trend_off_mask(spy_close)
    spy_ret = spy_ret.loc["1994-01-01":]
    off = off_full.reindex(spy_ret.index).fillna(False)

    events, validation = validate_dates(spy_ret.index)
    fomc = pd.Series(spy_ret.index.isin(events), index=spy_ret.index)
    fomc_off = fomc & off

    # ---- control reproduction (AGENTS.md): the prior study's exact cell ----
    cell = spy_ret[fomc_off]
    prior = {"n": 57, "mean_bps": 58.45, "welch_t_vs_all_other_days": 2.29}
    repro = {
        "n": int(fomc_off.sum()),
        "mean_bps": round(float(cell.mean()) * 1e4, 2),
        "welch_t_vs_all_other_days": round(
            welch_t(cell, spy_ret[~fomc_off]), 2
        ),
    }
    repro_ok = repro == prior
    print(f"Control reproduction vs pre_fomc_drift_study diagnostic: "
          f"{repro} ok={repro_ok}")
    if not repro_ok:
        raise RuntimeError(
            f"failed to reproduce the prior study's cell exactly: "
            f"got {repro}, expected {prior} -- fix before trusting anything else"
        )

    # ---- 1. subperiod stability ----
    print("Subperiod stability...", flush=True)
    subperiods = subperiod_table(spy_ret, fomc_off, off, fomc)

    # ---- 2a. placebo: FOMC x trend ON ----
    fomc_on = fomc & ~off
    placebo_on = {
        label: cell_stats(
            spy_ret.loc[slicer][fomc_on.loc[slicer]],
            spy_ret.loc[slicer][(~fomc & ~off).loc[slicer]],
            spy_ret.loc[slicer][~fomc_on.loc[slicer]],
        )
        for label, slicer in [
            ("full", slice(None)),
            ("pre_2015", slice(None, "2014-12-31")),
            ("post_2015", slice("2015-01-01", None)),
        ]
    }

    # ---- 2b. placebo: random-matched non-FOMC trend-off day-sets ----
    print(f"Permutation test ({N_PERMUTATION_DRAWS} draws x 2 variants)...",
          flush=True)
    cell_idx = spy_ret.index[fomc_off]
    pool_idx = spy_ret.index[off & ~fomc]
    permutation = permutation_test(spy_ret, cell_idx, pool_idx)

    # ---- 3. window sensitivity ----
    print("Window sensitivity...", flush=True)
    windows = window_sensitivity(spy_ret, off, events)

    # ---- 4. economic materiality ----
    full_row = next(r for r in subperiods if r["period"] == "full")
    post_row = next(r for r in subperiods if r["period"] == "post_2015")
    econ = materiality(
        spy_ret, off, fomc_off, full_row["mean_bps"], post_row["mean_bps"]
    )
    scen = econ["scenarios"]
    econ["plain_statement"] = (
        f"At the 2x lab's actual reserve ({RESERVE_WEIGHT_2X:.0%} of equity "
        f"when trend is off) and the recent event rate "
        f"({econ['event_frequency']['events_per_year_last_5y']:.1f} "
        f"events/yr over the last 5 years), even taking the in-sample "
        f"full-history mean at face value the overlay is worth about "
        f"{scen['full_sample_mean_2bps_leg_last5y_freq']['expected_annual_contribution_bps_of_equity']:.0f} "
        f"bps/yr = "
        f"${scen['full_sample_mean_2bps_leg_last5y_freq']['expected_annual_dollars_on_10k']:.0f}/yr "
        f"on the $10,000 account (before BIL yield forgone; "
        f"{scen['full_sample_mean_2bps_leg_last5y_freq_bil_yield_adjusted']['expected_annual_dollars_on_10k']:.0f} "
        f"after). Using the post-2015 mean instead gives "
        f"${scen['post_2015_mean_2bps_leg_last5y_freq']['expected_annual_dollars_on_10k']:.0f}/yr. "
        f"'Minor gains aggregated' is the mandate; this quantifies MINOR."
    )

    # ---- pre-registered decision ----
    decade_rows = [r for r in subperiods if r["period"].endswith("s")]
    checks = {
        "perm_p_unstratified_lt_0_05": (
            permutation["unstratified_primary"]["one_sided_p_ge_observed"] < 0.05
        ),
        "perm_p_year_matched_lt_0_05": (
            permutation["year_matched_secondary"]["one_sided_p_ge_observed"] < 0.05
        ),
        "trend_on_mean_lt_trend_off_mean_full": (
            placebo_on["full"]["mean_bps"] < full_row["mean_bps"]
        ),
        "trend_off_mean_positive_3_of_4_decades": (
            sum(r["mean_bps"] is not None and r["mean_bps"] > 0
                for r in decade_rows) >= 3
        ),
        "alt_window_mean_positive": (
            windows["alternative_T2_close_to_T_close"]["mean_bps"] > 0
        ),
    }
    mapping = PRE_REGISTRATION["decision_rule"]["mapping"]
    decision = mapping["all_pass"] if all(checks.values()) else mapping["any_fail"]

    payload = {
        # Conditioning disclosure deliberately FIRST, before the decision.
        "conditioning_disclosure": PRE_REGISTRATION["conditioning_disclosure"],
        "decision": decision,
        "decision_checks": checks,
        "decision_meaning": PRE_REGISTRATION["decision_rule"]["meaning"],
        "pre_registration": PRE_REGISTRATION,
        "date_validation": validation,
        "excluded_unscheduled_dates": FOMC_EXCLUDED_UNSCHEDULED,
        "control_reproduction": {
            "note": (
                "The prior study's diagnostic cell rebuilt from the same "
                "imports and diffed exactly (AGENTS.md control-reproduction "
                "rule); a mismatch aborts the study."
            ),
            "expected": prior,
            "reproduced": repro,
            "exact_match": repro_ok,
        },
        "subperiod_stability": {
            "note": (
                "Small-n cells reported with their small n, per "
                "pre-registration. welch_t_vs_nonfomc_trend_off is the "
                "specificity comparison (same-period trend-off days "
                "without an FOMC announcement)."
            ),
            "table": subperiods,
        },
        "placebo_trend_on": placebo_on,
        "placebo_random_matched_permutation": permutation,
        "placebo_cpi": {
            "status": "skipped",
            "reason": PRE_REGISTRATION["battery"]["2c_placebo_cpi"],
        },
        "window_sensitivity": windows,
        "economic_materiality": econ,
        "limitations": [
            "THE central limitation: the hypothesis was generated from the same 1994-2026 data every test above runs on. The permutation test measures how unusual the cell is relative to random trend-off day-sets, but it cannot undo the fact that this specific cell was selected for looking good -- among the many diagnostic cells across this repo's studies, some will clear p<0.05 by selection alone. No in-sample number here is confirmation.",
            "The prior study's non-gating diagnostics contained at least 3 conditional cells (pre/post-2015 split, trend-off conditional, cost/allocation variants) and this repo runs many studies; the effective number of implicit comparisons behind 'the best-looking cell' is unknown and uncorrectable after the fact.",
            "Close-to-close on day T is the deployable proxy, not the literature's close-to-2pm construction; daily bars cannot resolve the announcement reaction itself.",
            "Live execution trades ~09:51/10:05 ET, so a real entry would not match the modeled T-1-close entry (same gap as the prior study, disclosed not modeled).",
            "Trend-off days cluster in 2001-02, 2008-09, 2015-16, 2018, 2020, 2022 bear phases; the year-matched permutation controls calendar-year clustering but not finer within-year regime structure.",
            "Risk-free rate is zero everywhere in this repo (AGENTS.md); the BIL-yield-forgone adjustment in the materiality section is a stated ~5%-rate assumption, not a modeled series.",
            "CPI placebo skipped -- see placebo_cpi.reason.",
            "FOMC dates embedded from the Fed's published calendars, validated only as trading days; an off-by-one-trading-day transcription would not be caught mechanically (inherited from the prior study).",
        ],
        "screening_tier_caveat": (
            "All data ends 2026-07-22. early_2020_2022 / heldout_2023_plus "
            "are screening windows and the long history is in-sample by "
            "construction; nothing here touches or tunes on the frozen "
            "2026-08-13+ final-validation window. If the effect survived "
            "this battery, the ONLY real confirmation is live forward "
            "performance in that window -- e.g. a registered, "
            "observation-only forward log of FOMC x trend-off days as "
            "they occur (roughly 2/yr; a Wald-SPRT-style registration as "
            "in the IWM compression-breakout precedent would suit the "
            "low event rate better than a fixed-N gate)."
        ),
    }

    out = Path("reports/fomc_trend_off_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print(f"\nDecision: {decision}")
    print("\nSUBPERIOD STABILITY (FOMC x trend-off)")
    print(pd.DataFrame(subperiods).to_string(index=False))
    print("\nPLACEBO: FOMC x trend ON")
    print(pd.DataFrame(placebo_on).T.to_string())
    print("\nPERMUTATION (random-matched non-FOMC trend-off day-sets)")
    print(json.dumps(permutation, indent=2))
    print("\nWINDOW SENSITIVITY")
    print(json.dumps(windows, indent=2))
    print("\nECONOMIC MATERIALITY")
    print(json.dumps(econ["scenarios"], indent=2))
    print(econ["plain_statement"])
    print("\nDecision checks:", json.dumps(checks, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
