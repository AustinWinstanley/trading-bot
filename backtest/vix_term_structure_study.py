"""Does the VIX/VIX3M term structure add de-risking value BEYOND realized vol?

The 2x lab already shadows a realized-vol overlay (12% target, 63d lookback —
`engine/leverage_overlay.py`, nominated by
`reports/long_history_risk_overlay_study.json`). Backwardation in the VIX term
structure (VIX/VIX3M > 1) is a forward-looking stress signal that historically
precedes most large S&P 500 drawdowns, while contango dominates ~85% of days.
This study asks three pre-registered questions:

  1. Standalone: does gating the production portfolio's gross on the term
     structure alone clear the `risk_reducer` bar vs the raw portfolio?
  2. THE KEY CELL — incremental: applied ON TOP of the realized-vol overlay
     (scale = min(vol_scale, ts_scale)), does the gate improve anything vs
     the vol overlay alone? If not, that is the finding.
  3. Signal quality independent of the portfolio: per backwardation episode,
     what did SPY do over the next 5/10/20 sessions; false-positive rate;
     days per year the signal is active.

Vol-overlay implementation: the 2x-profile scaling REUSES (is verified
bit-identical to) `backtest.long_history_risk_overlay_study.de_risk_fixed_2x`,
the accepted study behind the shadow overlay, whose scale rule
`clip(target_vol / realized_63d_vol, 0.25, 1.0)` mirrors
`engine/leverage_overlay.recommend_leverage`. The base profile uses the same
generalized formula minus the margin-financing term (base carries none).

Data: Cboe daily index closes via `engine.cboe` (`state/cboe/VIX.csv`,
`state/cboe/VIX3M.csv`). NOTE: although VIX3M/VXV nominally starts late 2007,
Cboe's published CSV begins 1009 sessions later, 2009-09-18, and the module's
`daily_only` convention trims to the first full year — the usable ratio series
starts 2010-01-04. There is NO 2008 coverage; the episode table covers the
2010 flash crash, 2011, 2015-16, 2018 (both Feb and Q4), 2020, 2022, 2024-08
and 2025 stress regimes.

Research-only. No config or engine changes. Screening-tier evidence
(pre-2026-08-13); frozen-window validation would still be required before any
promotion.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.long_history_risk_overlay_study import de_risk_fixed_2x
from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.promotion import passes_gate_all_cells
from engine import cboe
from engine.tiingo import load_parquet

BASE_GROSS = 1.15
LEV_GROSS = 2.30
OVERLAY_COST_BPS = 5.0  # AGENTS.md cost schedule: portfolio vol overlays, per
                        # unit of one-way gross turnover (same as the accepted
                        # long-history overlay study)

WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}

# ---------------------------------------------------------------------------
# PRE-REGISTRATION — declared before any results were computed. Nothing below
# this block was tuned after seeing a portfolio number.
# ---------------------------------------------------------------------------
PRE_REGISTRATION = {
    "objective_class": "risk_reducer",
    "max_cagr_cost_pp": 1.0,
    "min_dd_improvement_pct": 0.10,
    "signal": (
        "ratio = VIX close / VIX3M close (Cboe daily), reindexed onto the "
        "portfolio calendar, lagged one session before acting"
    ),
    "primary_rule": {
        "ratio > 1.0 (backwardation)": "scale portfolio gross to 0.5x",
        "ratio <= 1.0 (contango)": "1.0x",
        "note": "de-risking conditioner only — never scales above 1.0x",
        "response": "daily, one-session lag; scale changes pay 5 bps per unit "
                    "of one-way gross turnover",
    },
    "sensitivity_variant": {
        "rule": "two-tier: ratio > 0.95 -> 0.75x, ratio > 1.0 -> 0.5x",
        "status": "sensitivity only — NOT promotable regardless of outcome",
    },
    "vol_overlay_reference": {
        "target_vol": 0.12,
        "lookback_sessions": 63,
        "min_scale": 0.25,
        "rebalance_sessions": 5,
        "source": (
            "backtest.long_history_risk_overlay_study.de_risk_fixed_2x "
            "(reused, verified bit-identical for the 2x profile); base "
            "profile uses the same formula without the margin-financing term"
        ),
    },
    "combination_rule": "combined_scale = min(vol_scale, ts_scale)",
    "episode_definition": (
        "maximal run of sessions with ratio > 1.0; runs separated by fewer "
        "than 5 contango sessions are merged into one episode"
    ),
    "false_positive_definition": (
        "an episode is a false positive if SPY's worst close-to-close return "
        "relative to the episode-start close over the following 60 sessions "
        "is shallower than -5%"
    ),
    "forward_horizons_sessions": [5, 10, 20],
    "windows": list(WINDOWS),
    "profiles": ["base", "2x"],
    "promotable_question": (
        "cell 2 only (incremental over the realized-vol overlay); cell 1 "
        "standalone is reported for context, the sensitivity variant is not "
        "promotable"
    ),
}

SCRATCH_TOLERANCE = 5e-4  # control-reproduction tolerance on rounded metrics


# ---------------------------------------------------------------------------
# Portfolio profiles from the accepted production backtest
# ---------------------------------------------------------------------------

def fixed_profiles(streams: pd.DataFrame) -> dict[str, pd.Series]:
    raw = (
        0.40 * streams["spy"]
        + 0.25 * streams["tsmom"]
        + 0.20 * streams["trend"]
        + 0.30 * streams["mom_ls"]
    )
    return {
        "base": raw - (0.15 * SHORT_BORROW / TD),
        "2x": 2 * raw - (MARGIN_RATE / TD) - (0.30 * SHORT_BORROW / TD),
    }


def vol_scale(
    fixed_returns: pd.Series,
    *,
    target_vol: float = 0.12,
    lookback: int = 63,
    min_scale: float = 0.25,
    rebalance_sessions: int = 5,
) -> pd.Series:
    """Replicates de_risk_fixed_2x's scale computation exactly."""
    observed = (
        fixed_returns.rolling(lookback, min_periods=max(20, lookback // 2)).std()
        * np.sqrt(TD)
    )
    desired = (target_vol / observed).clip(min_scale, 1.0).shift(1)
    weekly = pd.Series(np.nan, index=fixed_returns.index)
    weekly.iloc[::rebalance_sessions] = desired.iloc[::rebalance_sessions]
    return weekly.ffill().fillna(1.0)


def apply_scale(fixed: pd.Series, scale: pd.Series, profile: str) -> pd.Series:
    """Scale a fixed profile's gross, matching de_risk_fixed_2x's accounting.

    2x: add back fixed financing, charge only the margin implied by 2*scale.
    Base: no financing either way. Both pay 5 bps on scale-change turnover.
    """
    turnover_gross = LEV_GROSS if profile == "2x" else BASE_GROSS
    costs = scale.diff().abs().fillna(0.0) * turnover_gross * OVERLAY_COST_BPS / 10_000.0
    if profile == "2x":
        gross_before_financing = fixed + MARGIN_RATE / TD
        financing = (2.0 * scale - 1.0).clip(lower=0.0) * MARGIN_RATE / TD
        return scale * gross_before_financing - financing - costs
    return scale * fixed - costs


# ---------------------------------------------------------------------------
# Term-structure signal
# ---------------------------------------------------------------------------

def load_ratio() -> pd.Series:
    vix = norm_index(cboe.series("VIX"))
    vix3m = norm_index(cboe.series("VIX3M"))
    return (vix / vix3m).dropna().rename("vix_vix3m")


def ts_scale_from_ratio(
    ratio: pd.Series,
    index: pd.DatetimeIndex,
    tiers: list[tuple[float, float]],
) -> pd.Series:
    """Map the lagged ratio to a gross scale on the portfolio calendar."""
    lagged = ratio.reindex(index).ffill().shift(1)
    scale = pd.Series(1.0, index=index)
    for threshold, tier_scale in sorted(tiers):
        scale[lagged > threshold] = tier_scale
    return scale


PRIMARY_TIERS = [(1.0, 0.5)]
SENSITIVITY_TIERS = [(0.95, 0.75), (1.0, 0.5)]


# ---------------------------------------------------------------------------
# Episode diagnostics (portfolio-independent)
# ---------------------------------------------------------------------------

def episode_diagnostics(
    ratio: pd.Series,
    spy: pd.Series,
    *,
    merge_gap: int = 5,
    horizons: tuple[int, ...] = (5, 10, 20),
    dd_horizon: int = 60,
    dd_threshold: float = -0.05,
) -> tuple[list[dict], dict]:
    ratio = ratio.dropna()
    positions = np.flatnonzero((ratio > 1.0).to_numpy())
    if len(positions) == 0:
        return [], {"n_episodes": 0}

    runs: list[tuple[int, int]] = []
    start = prev = positions[0]
    for p in positions[1:]:
        if p - prev <= merge_gap:  # fewer than merge_gap contango sessions
            prev = p
        else:
            runs.append((start, prev))
            start = prev = p
    runs.append((start, prev))

    spy = spy.dropna()
    table = []
    for s, e in runs:
        start_date, end_date = ratio.index[s], ratio.index[e]
        seg = ratio.iloc[s:e + 1]
        row = {
            "start": start_date.date().isoformat(),
            "end": end_date.date().isoformat(),
            "span_sessions": int(e - s + 1),
            "backwardation_sessions": int((seg > 1.0).sum()),
            "max_ratio": round(float(seg.max()), 4),
        }
        spos = int(spy.index.searchsorted(start_date))
        if spos >= len(spy):
            row["spy_note"] = "no SPY data at episode start"
            table.append(row)
            continue
        base_price = float(spy.iloc[spos])
        for h in horizons:
            key = f"spy_fwd_{h}d"
            row[key] = (
                round(float(spy.iloc[spos + h] / base_price - 1), 4)
                if spos + h < len(spy) else None
            )
        fwd_window = spy.iloc[spos + 1: spos + 1 + dd_horizon]
        if len(fwd_window):
            worst = float((fwd_window / base_price - 1).min())
            row["spy_worst_60d"] = round(worst, 4)
            row["false_positive"] = bool(worst > dd_threshold)
        else:
            row["spy_worst_60d"] = None
            row["false_positive"] = None
        table.append(row)

    judged = [r for r in table if r.get("false_positive") is not None]
    fp = sum(r["false_positive"] for r in judged)
    active = ratio > 1.0
    by_year = active.groupby(active.index.year)
    spy_ret = spy.pct_change()

    def _mean(key):
        vals = [r[key] for r in table if r.get(key) is not None]
        return round(float(np.mean(vals)), 4) if vals else None

    def _median(key):
        vals = [r[key] for r in table if r.get(key) is not None]
        return round(float(np.median(vals)), 4) if vals else None

    summary = {
        "ratio_from": ratio.index[0].date().isoformat(),
        "ratio_to": ratio.index[-1].date().isoformat(),
        "n_sessions": int(len(ratio)),
        "pct_sessions_backwardated": round(float(active.mean()), 4),
        "backwardation_sessions_per_year": {
            str(year): int(count) for year, count in by_year.sum().items()
        },
        "n_episodes": len(runs),
        "episodes_judged_for_false_positive": len(judged),
        "false_positives": int(fp),
        "false_positive_rate": round(fp / len(judged), 4) if judged else None,
        "hit_rate_5pct_drawdown_within_60d": (
            round(1 - fp / len(judged), 4) if judged else None
        ),
        "mean_spy_fwd": {f"{h}d": _mean(f"spy_fwd_{h}d") for h in horizons},
        "median_spy_fwd": {f"{h}d": _median(f"spy_fwd_{h}d") for h in horizons},
        "unconditional_spy_mean_fwd": {
            f"{h}d": round(float((1 + spy_ret).rolling(h).apply(np.prod).mean() - 1), 4)
            for h in horizons
        },
    }
    return table, summary


# ---------------------------------------------------------------------------
# Study
# ---------------------------------------------------------------------------

def window_cells(
    control: dict[str, pd.Series],
    candidate: dict[str, pd.Series],
    label: str,
) -> tuple[list, dict]:
    """Build (window, profile, control, candidate) cells plus readable rows."""
    cells, rows = [], {}
    for window, sl in WINDOWS.items():
        rows[window] = {}
        for profile in ("base", "2x"):
            c = returns_summary(control[profile].loc[sl], f"control {profile}")
            v = returns_summary(candidate[profile].loc[sl], f"{label} {profile}")
            cells.append((window, profile, c, v))
            rows[window][profile] = {"control": c, "candidate": v}
    return cells, rows


def main() -> None:
    print("Loading term-structure signal ...")
    ratio = load_ratio()
    spy_full = norm_index(load_parquet(["SPY"])["SPY"]["close"])

    print("Building production streams (minutes) ...")
    streams = build_streams()
    profiles = fixed_profiles(streams)
    index = profiles["base"].index

    # --- Control reproduction (AGENTS.md: validate against the accepted study)
    stored = json.loads(Path("reports/production_portfolio.json").read_text())
    stored_rows = {r["portfolio"]: r for r in stored["results"]}
    control_repro = {}
    for profile, stored_label in (
        ("base", "production SPY-core base (1.15x gross)"),
        ("2x", "production SPY-core 2x (2.30x gross)"),
    ):
        here = returns_summary(profiles[profile], stored_label)
        ref = stored_rows[stored_label]
        diffs = {
            k: round(abs(here[k] - ref[k]), 6)
            for k in ("cagr", "sharpe", "max_dd")
        }
        control_repro[profile] = {
            "reproduced": all(d <= SCRATCH_TOLERANCE for d in diffs.values()),
            "this_study": here,
            "accepted_reports_production_portfolio_json": {
                k: ref[k] for k in ("from", "to", "cagr", "sharpe", "max_dd")
            },
            "abs_diffs": diffs,
        }
    repro_ok = all(v["reproduced"] for v in control_repro.values())
    print(f"Control reproduction: {'OK' if repro_ok else 'MISMATCH'}")
    if not repro_ok:
        print(json.dumps(control_repro, indent=2))
        raise SystemExit(
            "Control does not reproduce the accepted production portfolio; "
            "refusing to evaluate variants on top of a drifted baseline."
        )

    # --- Vol overlay (control for the key cell) + faithfulness check
    vscales = {p: vol_scale(profiles[p]) for p in profiles}
    vol_overlaid = {p: apply_scale(profiles[p], vscales[p], p) for p in profiles}
    reused_2x, _ = de_risk_fixed_2x(profiles["2x"], 0.12)
    faith_diff = float((vol_overlaid["2x"] - reused_2x).abs().max())
    faithfulness = {
        "reused_function": "backtest.long_history_risk_overlay_study.de_risk_fixed_2x",
        "max_abs_daily_return_diff_2x": faith_diff,
        "bit_identical": faith_diff < 1e-15,
    }
    print(f"Vol-overlay faithfulness vs de_risk_fixed_2x: max diff {faith_diff:.2e}")

    # --- Term-structure scales
    ts_primary = ts_scale_from_ratio(ratio, index, PRIMARY_TIERS)
    ts_sens = ts_scale_from_ratio(ratio, index, SENSITIVITY_TIERS)

    gate_kwargs = dict(
        objective_class=PRE_REGISTRATION["objective_class"],
        max_cagr_cost_pp=PRE_REGISTRATION["max_cagr_cost_pp"],
        min_dd_improvement_pct=PRE_REGISTRATION["min_dd_improvement_pct"],
    )

    # Cell 1: standalone term-structure gate vs raw production portfolio.
    standalone = {p: apply_scale(profiles[p], ts_primary, p) for p in profiles}
    cells1, rows1 = window_cells(profiles, standalone, "ts-gate standalone")
    gate1 = passes_gate_all_cells(cells1, **gate_kwargs)

    # Cell 2 (KEY): incremental on top of the realized-vol overlay.
    combined = {
        p: apply_scale(profiles[p], np.minimum(vscales[p], ts_primary), p)
        for p in profiles
    }
    cells2, rows2 = window_cells(vol_overlaid, combined, "vol overlay + ts gate")
    gate2 = passes_gate_all_cells(cells2, **gate_kwargs)

    # How often does the term-structure gate actually bind beyond the vol
    # overlay? (Days where min(vol, ts) < vol, i.e. ts is the tighter cap.)
    binding = {}
    for p in profiles:
        both = pd.DataFrame({"vol": vscales[p], "ts": ts_primary})
        for window, sl in WINDOWS.items():
            seg = both.loc[sl]
            binding.setdefault(window, {})[p] = {
                "sessions": int(len(seg)),
                "ts_active_sessions": int((seg["ts"] < 1.0).sum()),
                "ts_binding_beyond_vol_sessions": int(
                    (seg["ts"] < seg["vol"]).sum()
                ),
                "vol_scale_mean": round(float(seg["vol"].mean()), 4),
            }

    # Sensitivity: two-tier variant, standalone and incremental. Not promotable.
    sens_standalone = {p: apply_scale(profiles[p], ts_sens, p) for p in profiles}
    cells3a, rows3a = window_cells(profiles, sens_standalone, "two-tier standalone")
    gate3a = passes_gate_all_cells(cells3a, **gate_kwargs)
    sens_combined = {
        p: apply_scale(profiles[p], np.minimum(vscales[p], ts_sens), p)
        for p in profiles
    }
    cells3b, rows3b = window_cells(vol_overlaid, sens_combined, "two-tier incremental")
    gate3b = passes_gate_all_cells(cells3b, **gate_kwargs)

    # Cell 3: portfolio-independent signal quality.
    episode_table, episode_summary = episode_diagnostics(ratio, spy_full)

    # --- Decision
    if gate2["all_no_effect"]:
        decision = "no_effect_beyond_realized_vol"
    elif gate2["passed"]:
        decision = (
            "screen_pass_candidate_for_shadow"  # still needs frozen-window
        )
    else:
        decision = "reject_no_incremental_value_beyond_realized_vol"

    payload = {
        "decision": decision,
        "decision_note": (
            "The promotable question is pre-registered as cell 2 only: does "
            "the VIX/VIX3M gate improve the portfolio beyond the existing "
            "12% realized-vol overlay under the declared risk_reducer "
            "budgets. Standalone and sensitivity results are context."
        ),
        "screening_tier_caveat": (
            "All evidence here is screening-tier (data through 2026-07-22, "
            "entirely pre-2026-08-13). Per AGENTS.md, clearing this screen "
            "is a precondition for shadow observation, not sufficient "
            "evidence for promotion; the 2026-08-13+ frozen window and live "
            "paper journal remain the final validation."
        ),
        "pre_registration": PRE_REGISTRATION,
        "control_reproduction": control_repro,
        "vol_overlay_faithfulness": faithfulness,
        "cell_1_standalone_vs_raw": {
            "gate": gate1, "summaries": rows1,
        },
        "cell_2_incremental_over_vol_overlay": {
            "gate": gate2, "summaries": rows2,
            "ts_binding_beyond_vol": binding,
        },
        "sensitivity_two_tier_not_promotable": {
            "standalone": {"gate": gate3a, "summaries": rows3a},
            "incremental": {"gate": gate3b, "summaries": rows3b},
        },
        "cell_3_signal_quality": {
            "summary": episode_summary,
            "episodes": episode_table,
        },
        "limitations": [
            "Cboe's published VIX3M CSV begins 2009-09-18 (trimmed to "
            "2010-01-04 by the daily-coverage convention), not the index's "
            "nominal 2007 start — there is NO 2008 coverage anywhere in this "
            "study, so the single most important historical backwardation "
            "regime is unobserved.",
            "Portfolio cells inherit every production_portfolio.py "
            "limitation: survivorship-biased cross-sectional universe, panel "
            "start 2020-07-28 (no COVID-crash observation — the one recent "
            "episode where the term structure inverted hardest is absent "
            "from the portfolio windows, though it is in the episode table "
            "via SPY), zero risk-free rate in Sharpe.",
            "The realized-vol overlay modeled here is the backtest formula "
            "from the accepted long-history study; the production shadow "
            "overlay measures account-equity vol with min_observations=32 "
            "and daily evaluation, so live scaling would differ in detail.",
            "VIX cache ends 2026-07-22 (deliberately not refreshed to avoid "
            "mutating a cache shared with other studies mid-campaign); the "
            "episode table and activity stats end there.",
            "The 0.5x scale depth and 1.0 threshold are conventions, not "
            "optimized values; only the pre-registered two-tier variant was "
            "examined as sensitivity, and it is not promotable.",
            "5 bps/leg overlay cost applies to scale-change turnover only; "
            "it does not model gap risk between the signal close and "
            "next-day execution.",
        ],
        "outputs": {
            "vix3m_cache": "state/cboe/VIX3M.csv",
        },
    }
    out = Path("reports/vix_term_structure_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print(f"\nDecision: {decision}")
    print(f"Cell 1 standalone gate: passed={gate1['passed']} "
          f"all_no_effect={gate1['all_no_effect']}")
    print(f"Cell 2 incremental gate: passed={gate2['passed']} "
          f"all_no_effect={gate2['all_no_effect']}")
    print(f"Episodes: {episode_summary.get('n_episodes')} "
          f"false-positive rate: {episode_summary.get('false_positive_rate')}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
