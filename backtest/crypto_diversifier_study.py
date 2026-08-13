"""Pre-registered RE-ASK: the crypto trend sleeve judged as a `diversifier`.

What this is
------------
`reports/crypto_trend_study.json` (decision:
`screening_fail_do_not_scope_execution_phase`) rejected a 5% crypto trend
allocation under the `return_enhancer` hard gate: it failed on small
drawdown adds (0.34-0.76pp) despite higher Sharpe AND higher CAGR in both
heldout cells and 0.28 correlation to the production portfolio. The
`diversifier` objective class in `backtest/promotion.py` — a class for
exactly the question "does adding a NEW, lowly-correlated stream improve
the portfolio within drawdown noise?" — did not exist when that study ran.
This is a fresh pre-registered study under that class, NOT an edit to the
old report, whose decision stands as the `return_enhancer` answer.

CONDITIONING DISCLOSURE (read this before the results)
------------------------------------------------------
This re-ask was motivated by having SEEN the prior study's results: the
candidate was selected for re-judging because it looked good under the old
gate (clean heldout-2x pass, 0.28 measured correlation), and the
`diversifier` class it is judged under was added to `backtest/promotion.py`
after — and partly because — candidates of exactly this shape were being
rejected on sub-percentage-point drawdown differences. Both facts condition
this analysis on already-observed data. The pre-registration below is
honest about its parameters (they were written into the report file before
this script computed any gate result), but it cannot undo that
conditioning. Therefore even a clean pass here is screening-tier evidence
at its weakest: the real test remains the experiment tier plus live
forward performance on the frozen 2026-08-13+ validation window, which
this study does not touch and cannot substitute for.

Pre-registration (declared before any gate result was computed)
---------------------------------------------------------------
* Candidate: byte-identical to the prior study's PRIMARY — TSMOM long/flat
  on BTC/USD + ETH/USD, 252-row lookback, inverse-vol weights, one-day
  lag, via functions IMPORTED from backtest/crypto_trend_study.py (not
  reimplemented). 90/180-row variants are NOT re-litigated here.
* Marginal test: 5% allocation funded pro-rata from all sleeves
  (candidate = 0.95*control + 0.05*crypto per profile), crypto returns
  compounded onto the equity trading calendar — same convention as prior.
* Costs: 25 bps/leg primary, 50 bps/leg stress — same as prior; both
  remain UNVERIFIED planning figures.
* objective_class: "diversifier".
* max_correlation: 0.40. The prior study observed 0.28 vs the production
  base portfolio; 0.40 is deliberately declared ABOVE the observed value
  (the declaration is the bar; the fresh per-cell measurement goes into
  the gate as `stream_correlation`).
* max_dd_cost_pp: per cell, the value returned by
  backtest.promotion.paired_drawdown_noise_pp on that cell's aligned
  control/candidate daily returns, with the module defaults
  (block_size=63, simulations=2000, seed=20260812). Every computed band
  is echoed into the report. Because the band differs per cell,
  passes_gate is called per cell and the results aggregated manually in
  the exact {passed, any_no_effect, all_no_effect, cells} shape
  passes_gate_all_cells produces — that helper only supports one uniform
  kwarg set across cells.
* Cells: the standard 4 — (early_2020_2022, heldout_2023_plus) x
  (base, 2x) — with the prior study's honest partial-coverage disclosure:
  crypto data starts 2021-01-01 and the 252-row signal is first defined
  ~2021-09, so the early cell is PARTIAL; exact spans are in the report.
* Control-reproduction first: the crypto trend stream must reproduce the
  committed prior study's standalone numbers exactly (imported code on
  the same capped cache), the equity-TSMOM reproduction must hold to
  <1e-12, and the production portfolio baseline must match
  reports/production_portfolio.json exactly, before anything is judged.
  Any reproduction failure aborts the run.
* Data capped at 2026-08-12 — the frozen 2026-08-13+ window is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest.crypto_trend_study import (
    CRYPTO_ALLOCATION,
    PRIMARY_COST_BPS,
    PRIMARY_LOOKBACK,
    SCREEN_WINDOWS,
    STRESS_COST_BPS,
    TD_CRYPTO,
    load_crypto_closes,
    summarize,
    tsmom_stream_from_closes,
    validate_control_reproduction,
)
from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    resample_returns,
    returns_summary,
)
from backtest.promotion import paired_drawdown_noise_pp, passes_gate

OUT_PATH = Path("reports/crypto_diversifier_study.json")
PRIOR_STUDY_PATH = Path("reports/crypto_trend_study.json")
PRODUCTION_REPORT_PATH = Path("reports/production_portfolio.json")

MAX_CORRELATION = 0.40  # declared above the prior study's observed 0.28
# Explicitly the paired_drawdown_noise_pp defaults, echoed for the report.
NOISE_BAND_PARAMS = {"block_size": 63, "simulations": 2000, "seed": 20260812}

NUMERIC_SUMMARY_KEYS = (
    "years", "cagr", "ann_return", "vol", "sharpe", "max_dd", "x_money",
)


# --------------------------------------------------------------------------
# Control reproduction — nothing is judged until all three checks hold
# --------------------------------------------------------------------------


def _diff_summaries(mine: dict, theirs: dict, keys) -> dict:
    """Exact-equality diff of rounded summary fields, plus date span."""
    keys = list(keys) + ["from", "to"]
    return {
        k: {"mine": mine.get(k), "prior_report": theirs.get(k)}
        for k in keys
        if mine.get(k) != theirs.get(k)
    }


def reproduce_crypto_stream(closes: pd.DataFrame) -> dict:
    """The candidate stream must match the committed prior study exactly.

    The construction functions are IMPORTED from crypto_trend_study (one
    shared code path — the daily-return difference between "my" stream and
    that study's stream is 0.0 by construction), so the check that carries
    information is against the committed report JSON: it proves the cached
    data, the frozen-window cap, and the code path all still produce the
    numbers the prior decision was based on.
    """
    prior = json.loads(PRIOR_STUDY_PATH.read_text())
    rows = {r["portfolio"]: r for r in prior["standalone"]["full_period"]}

    primary, _ = tsmom_stream_from_closes(
        closes, lookback=PRIMARY_LOOKBACK, cost_bps=PRIMARY_COST_BPS
    )
    stress, _ = tsmom_stream_from_closes(
        closes, lookback=PRIMARY_LOOKBACK, cost_bps=STRESS_COST_BPS
    )
    mismatches = {}
    for stream, label in (
        (primary, f"crypto trend {PRIMARY_COST_BPS:.0f}bps (PRIMARY)"),
        (stress, f"crypto trend {STRESS_COST_BPS:.0f}bps (stress)"),
    ):
        mine = summarize(stream, label, TD_CRYPTO)
        diff = _diff_summaries(mine, rows[label], NUMERIC_SUMMARY_KEYS)
        if diff:
            mismatches[label] = diff
    data_span_matches = (
        [closes.index[0].date().isoformat(), closes.index[-1].date().isoformat()]
        == prior["data"]["span"]
        and len(closes) == prior["data"]["n_days"]
    )
    return {
        "method": (
            "Stream construction is IMPORTED from backtest/crypto_trend_study.py "
            "(tsmom_stream_from_closes + load_crypto_closes), so the daily-return "
            "difference vs that study's own stream is 0.0 by shared code path. "
            "The informative check: rebuild the primary and stress streams on the "
            "current cache and require every rounded standalone summary field to "
            "EQUAL the committed reports/crypto_trend_study.json values, and the "
            "data span/row-count to equal that report's data block."
        ),
        "max_abs_daily_return_diff_vs_prior_construction": 0.0,
        "data_span_matches_prior_report": bool(data_span_matches),
        "summary_field_mismatches": mismatches,
        "reproduced": bool(data_span_matches and not mismatches),
    }


def reproduce_production_baseline(controls: dict[str, pd.Series]) -> dict:
    """Full-period control summaries must equal reports/production_portfolio.json."""
    prior = json.loads(PRODUCTION_REPORT_PATH.read_text())
    rows = {r["portfolio"]: r for r in prior["results"]}
    labels = {
        "base": "production SPY-core base (1.15x gross)",
        "2x": "production SPY-core 2x (2.30x gross)",
    }
    per_profile = {}
    for profile, label in labels.items():
        mine = returns_summary(controls[profile], label)
        diff = _diff_summaries(
            mine, rows[label], list(NUMERIC_SUMMARY_KEYS) + ["sortino"]
        )
        per_profile[profile] = {
            "label": label,
            "mismatched_fields": diff,
            "matched": not diff,
            "summary": mine,
        }
    return {
        "method": (
            "Rebuild both profiles' control streams (build_streams + the exact "
            "production sleeve weights and cost/borrow/margin adjustments) and "
            "require every rounded returns_summary field to EQUAL the committed "
            "reports/production_portfolio.json results rows."
        ),
        "per_profile": per_profile,
        "reproduced": all(p["matched"] for p in per_profile.values()),
    }


def check_cell_controls_vs_prior_study(cell_control_summaries: dict) -> dict:
    """Bonus check: per-cell control rows must equal the prior study's."""
    prior = json.loads(PRIOR_STUDY_PATH.read_text())
    prior_rows = prior["marginal_addition"]["performance"]["primary_25bps"]
    mismatches = {}
    for window, rows in prior_rows.items():
        for row in rows:
            if row["variant"] != "control":
                continue
            mine = cell_control_summaries[(window, row["profile"])]
            diff = _diff_summaries(
                mine, row, list(NUMERIC_SUMMARY_KEYS) + ["sortino"]
            )
            if diff:
                mismatches[f"{window}/{row['profile']}"] = diff
    return {"mismatches": mismatches, "reproduced": not mismatches}


# --------------------------------------------------------------------------
# Study
# --------------------------------------------------------------------------


def build_controls(streams: pd.DataFrame) -> dict[str, pd.Series]:
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


def pre_registration_block() -> dict:
    return {
        "re_ask_of": str(PRIOR_STUDY_PATH),
        "objective_class": "diversifier",
        "primary_candidate": (
            "Byte-identical to crypto_trend_study's PRIMARY: TSMOM long/flat "
            "on BTC/USD + ETH/USD, long iff trailing 252-row return > 0 on "
            "completed UTC-daily bars, inverse-vol weighted (63-row vol, "
            "floor 0.04), weights lagged one day, unlit = cash — built by "
            "IMPORTING that study's functions, not reimplementing them. "
            "90/180-row lookbacks are not re-litigated."
        ),
        "marginal_test": (
            "5% allocation funded pro-rata from all sleeves "
            "(candidate = 0.95*control + 0.05*crypto per profile), crypto "
            "returns compounded onto the equity trading calendar; candidate "
            "== control before the crypto stream exists — same convention "
            "as the prior study."
        ),
        "allocation": CRYPTO_ALLOCATION,
        "primary_lookback": PRIMARY_LOOKBACK,
        "costs_bps_one_way": {
            "primary": PRIMARY_COST_BPS,
            "stress": STRESS_COST_BPS,
        },
        "cost_caveat": (
            "Planning figures only — Alpaca crypto spreads are much wider "
            "than equities and neither number has been verified against "
            "real paper fills; that verification is a hard precondition to "
            "any deployment."
        ),
        "max_correlation": MAX_CORRELATION,
        "max_correlation_note": (
            "The prior study observed stream correlation 0.28 vs the "
            "production base portfolio. 0.40 is deliberately declared ABOVE "
            "that already-seen value — the declaration is the bar, and the "
            "freshly measured per-cell correlation enters the gate as "
            "stream_correlation. Declaring a bound above an observed value "
            "is part of the conditioning this report discloses; it is tight "
            "enough to fail if the measured correlation regime shifts "
            "materially, but it was not chosen blind."
        ),
        "max_dd_cost_pp": (
            "Per cell: the value returned by "
            "backtest.promotion.paired_drawdown_noise_pp(control_daily, "
            "candidate_daily) on that cell's aligned daily returns, module "
            "defaults (block_size=63, simulations=2000, seed=20260812). Not "
            "a hand-picked number; every computed band is echoed in the "
            "gate inputs and diagnostics."
        ),
        "noise_band_params": NOISE_BAND_PARAMS,
        "promotion_rule": (
            "Per cell: backtest.promotion.passes_gate(control, candidate, "
            "'diversifier', max_dd_cost_pp=<that cell's band>, "
            "max_correlation=0.40, stream_correlation=<that cell's measured "
            "correlation>). All 4 standard cells (early_2020_2022, "
            "heldout_2023_plus) x (base, 2x) must pass at primary cost; the "
            "stress-cost gate is reported alongside. Aggregated manually in "
            "passes_gate_all_cells's exact output shape because the "
            "per-cell max_dd_cost_pp differs and that helper only supports "
            "one uniform kwarg set across cells. Screening only — never a "
            "direct promotion."
        ),
        "correlation_convention": (
            "Daily correlation of the aligned crypto stream vs that cell's "
            "control, on the equity trading calendar, restricted to days on "
            "or after the 252-row signal is first defined (~2021-09) so "
            "warm-up zeros do not dilute the estimate — the prior study's "
            "convention, applied per cell."
        ),
        "daily_close_convention": (
            "Alpaca crypto 1Day bars, stamped at UTC midnight, covering the "
            "UTC calendar day; still-forming current UTC day dropped; data "
            "capped at 2026-08-12 (frozen 2026-08-13+ window untouched)."
        ),
        "annualization": (
            "Raw crypto streams annualized at 365 periods/year; "
            "portfolio-aligned streams keep the repo's 252 convention."
        ),
    }


def conditioning_disclosure() -> str:
    return (
        "PROMINENT CONDITIONING DISCLOSURE: this re-ask exists because the "
        "prior study's results were already SEEN — the candidate was picked "
        "for re-judging precisely because it passed the heldout-2x cell "
        "cleanly, improved Sharpe AND CAGR in both heldout cells, measured "
        "0.28 correlation to the production portfolio, and failed the "
        "return_enhancer gate only on 0.34-0.76pp drawdown adds. The "
        "diversifier objective class itself was added to "
        "backtest/promotion.py after, and partly because, results of "
        "exactly this shape were being rejected on drawdown noise; this "
        "study is that class's first user. The pre-registration is honest "
        "about its parameters (this block and the pre_registration block "
        "were written to this report file BEFORE any gate result was "
        "computed, and max_correlation was declared above the "
        "already-observed 0.28), but no pre-registration can undo "
        "candidate selection conditioned on observed outcomes. A pass here "
        "is therefore screening-tier evidence at its weakest: the real "
        "test remains the experiment tier plus live forward performance on "
        "the frozen 2026-08-13+ validation window, which this study does "
        "not touch and cannot substitute for. No config, engine, or "
        "execution change follows from this report."
    )


def limitations() -> list[str]:
    return [
        # -- carried verbatim in substance from crypto_trend_study --
        "Only ~5.5 years of crypto history (2021-01-01 floor at Alpaca) — "
        "three regimes but a single full trend cycle; far short of the "
        "multi-decade evidence behind the equity TSMOM sleeve.",
        "The early_2020_2022 gate cell is PARTIAL for crypto: the portfolio "
        "window starts 2020-07-28 but crypto data starts 2021-01-01 and the "
        "252-row signal is first defined ~2021-09; see cell_coverage for "
        "exact spans.",
        "25/50 bps costs are pre-registered planning figures; Alpaca paper "
        "crypto fill quality is UNVERIFIED — real paper fills must validate "
        "them before any deployment.",
        "Crypto trades 24/7 while the bot's cron runs on equity market "
        "hours; execution design is an open question deferred to a "
        "separately planned phase, and this study models UTC-midnight "
        "rebalances the current infrastructure cannot place. Even a gate "
        "pass here authorizes at most scoping that phase — deployment is a "
        "separate decision plus a 24/7 execution build, both out of scope.",
        "252-row lookback on a 24/7 market is ~8.3 calendar months, not 12 "
        "— kept deliberately to mirror the accepted implementation.",
        "Risk-free rate modeled as zero throughout (AGENTS.md open gap); "
        "identical on both sides of every comparison here.",
        "Long/flat only, no shorting, no intra-day stops; weekend crypto "
        "returns are compounded into the next equity trading day, which "
        "slightly smooths measured correlation vs a true 24/7 accounting.",
        # -- new to this study --
        "Screening-tier evidence only (windows predate the frozen "
        "2026-08-13+ validation window, and heldout_2023_plus has "
        "arbitrated many prior candidates — see AGENTS.md on its "
        "multiplicity). Nothing here is evidence from the frozen window.",
        "Conditioning: candidate selection and the existence of the "
        "objective class it is judged under were both informed by the "
        "prior study's observed results — see conditioning_disclosure. "
        "max_correlation=0.40 was declared above an already-observed 0.28.",
        "The per-cell noise band is estimated by paired block bootstrap "
        "from the SAME cell sample it is then used to judge — a mild "
        "in-sample dependence that is inherent to the "
        "paired_drawdown_noise_pp design and disclosed rather than "
        "resolved. The seed/block/simulation parameters are the module "
        "defaults, fixed before any band was computed.",
    ]


def main() -> None:
    print("1/6 Control reproduction: equity TSMOM sleeve...", flush=True)
    equity_repro = validate_control_reproduction()
    print(
        f"    max_abs_daily_return_diff={equity_repro['max_abs_daily_return_diff']:.2e} "
        f"reproduced={equity_repro['reproduced']}"
    )

    print("2/6 Control reproduction: crypto stream vs prior study...", flush=True)
    closes = load_crypto_closes()
    crypto_repro = reproduce_crypto_stream(closes)
    print(f"    reproduced={crypto_repro['reproduced']}")

    print("3/6 Control reproduction: production baseline vs report...", flush=True)
    streams = build_streams()
    controls = build_controls(streams)
    baseline_repro = reproduce_production_baseline(controls)
    print(f"    reproduced={baseline_repro['reproduced']}")

    control_reproduction = {
        "equity_tsmom": equity_repro,
        "crypto_stream_vs_prior_study": crypto_repro,
        "production_baseline_vs_report": baseline_repro,
    }
    if not (
        equity_repro["reproduced"]
        and crypto_repro["reproduced"]
        and baseline_repro["reproduced"]
    ):
        OUT_PATH.write_text(json.dumps({
            "decision": "aborted_control_reproduction_failed",
            "control_reproduction": control_reproduction,
        }, indent=2, default=str))
        raise SystemExit(
            "Control reproduction FAILED — nothing judged; see report."
        )

    # ---- Pre-registration is written to disk BEFORE any gate math runs ----
    print("4/6 Writing pre-registration block to report (before gates)...",
          flush=True)
    pre_registration = pre_registration_block()
    disclosure = conditioning_disclosure()
    OUT_PATH.write_text(json.dumps({
        "decision": "PENDING_pre_registered_gates_not_yet_computed",
        "conditioning_disclosure": disclosure,
        "pre_registration": pre_registration,
        "control_reproduction": control_reproduction,
    }, indent=2, default=str))

    print("5/6 Cells: summaries, correlations, noise bands, gates...",
          flush=True)
    primary, _ = tsmom_stream_from_closes(
        closes, lookback=PRIMARY_LOOKBACK, cost_bps=PRIMARY_COST_BPS
    )
    stress, _ = tsmom_stream_from_closes(
        closes, lookback=PRIMARY_LOOKBACK, cost_bps=STRESS_COST_BPS
    )

    def aligned(stream: pd.Series) -> pd.Series:
        return resample_returns(norm_index(stream), streams.index)

    def candidate_for(control: pd.Series, crypto_aligned: pd.Series) -> pd.Series:
        blended = (
            (1 - CRYPTO_ALLOCATION) * control + CRYPTO_ALLOCATION * crypto_aligned
        )
        return blended.where(crypto_aligned.notna(), control)

    first_signal = closes.shift(PRIMARY_LOOKBACK).notna().any(axis=1)
    first_signal_date = first_signal[first_signal].index[0]

    aligned_streams = {
        "primary_25bps": aligned(primary),
        "stress_50bps": aligned(stress),
    }

    gates = {}
    diagnostics = {}
    performance = {}
    cell_coverage = {}
    cell_control_summaries = {}

    for window, slicer in SCREEN_WINDOWS.items():
        win_index = streams.index.to_series().loc[slicer]
        overlap = aligned_streams["primary_25bps"].loc[slicer].dropna()
        cell_coverage[window] = {
            "portfolio_window": [
                win_index.index[0].date().isoformat(),
                win_index.index[-1].date().isoformat(),
            ],
            "crypto_stream_overlap": (
                [overlap.index[0].date().isoformat(),
                 overlap.index[-1].date().isoformat()]
                if len(overlap) else None
            ),
            "crypto_signal_defined_from": first_signal_date.date().isoformat(),
            "note": (
                "Candidate is identical to control on portfolio days before "
                "the crypto stream exists; between crypto data start and "
                "signal-defined date the 5% sleeve sits in modeled-0 cash "
                "(warm-up). The early cell is PARTIAL for crypto."
            ),
        }

    for cost_label, crypto_aligned in aligned_streams.items():
        cell_results = []
        diag_rows = []
        performance[cost_label] = {}
        for window, slicer in SCREEN_WINDOWS.items():
            rows = []
            for profile, control in controls.items():
                control_summary = returns_summary(
                    control.loc[slicer], f"{profile} control (no crypto)"
                )
                candidate = candidate_for(control, crypto_aligned)
                candidate_summary = returns_summary(
                    candidate.loc[slicer],
                    f"{profile} +5% crypto trend ({cost_label})",
                )
                if cost_label == "primary_25bps":
                    cell_control_summaries[(window, profile)] = control_summary
                rows.append(
                    {"variant": "control", "profile": profile, **control_summary}
                )
                rows.append(
                    {"variant": "candidate", "profile": profile,
                     **candidate_summary}
                )

                # Aligned daily returns for this cell.
                pair = pd.DataFrame(
                    {"control": control, "candidate": candidate}
                ).loc[slicer].dropna()
                band_pp = paired_drawdown_noise_pp(
                    pair["control"], pair["candidate"], **NOISE_BAND_PARAMS
                )

                # Per-cell measured correlation (prior study's convention,
                # applied within the cell): crypto stream vs this cell's
                # control, signal-defined days only.
                live = crypto_aligned.dropna()
                live = live[live.index >= first_signal_date]
                joint = pd.DataFrame(
                    {"crypto": live, "control": control}
                ).loc[slicer].dropna()
                cell_corr = float(joint["crypto"].corr(joint["control"]))

                result = passes_gate(
                    control_summary,
                    candidate_summary,
                    "diversifier",
                    max_dd_cost_pp=band_pp,
                    max_correlation=MAX_CORRELATION,
                    stream_correlation=cell_corr,
                )
                cell_results.append(
                    {"window": window, "profile": profile, **result.to_dict()}
                )

                d_max_dd_pp = round(
                    (candidate_summary["max_dd"] - control_summary["max_dd"])
                    * 100, 3,
                )
                diag_rows.append({
                    "window": window,
                    "profile": profile,
                    "noise_band_pp": round(band_pp, 4),
                    "d_max_dd_pp": d_max_dd_pp,
                    "dd_diff_in_bands": (
                        round(d_max_dd_pp / band_pp, 2) if band_pp else None
                    ),
                    "dd_reading": (
                        f"{d_max_dd_pp:+.2f}pp vs a ±{band_pp:.2f}pp 1-sigma "
                        f"paired-bootstrap band = "
                        f"{abs(d_max_dd_pp) / band_pp:.1f} bands "
                        f"{'worse' if d_max_dd_pp < 0 else 'better'}"
                    ),
                    "stream_correlation": round(cell_corr, 4),
                    "correlation_n_days": int(len(joint)),
                    "d_sharpe": result.inputs["d_sharpe"],
                    "d_cagr_pp": round(result.inputs["d_cagr"] * 100, 3),
                })
            performance[cost_label][window] = rows

        # Manual aggregation in passes_gate_all_cells's exact output shape:
        # per-cell max_dd_cost_pp bands make the uniform-kwargs helper
        # inapplicable (it applies one kwarg set to every cell).
        gates[cost_label] = {
            "objective_class": "diversifier",
            "passed": bool(cell_results)
            and all(r["passed"] for r in cell_results),
            "any_no_effect": any(r["no_effect"] for r in cell_results),
            "all_no_effect": bool(cell_results)
            and all(r["no_effect"] for r in cell_results),
            "cells": cell_results,
            "aggregation_note": (
                "Aggregated manually (same shape and all-cells-must-pass "
                "rule as passes_gate_all_cells) because max_dd_cost_pp is "
                "a per-cell paired-bootstrap band and that helper only "
                "supports one uniform kwarg set across cells. Each cell's "
                "band is echoed in its gate inputs."
            ),
        }
        diagnostics[cost_label] = diag_rows

    print("6/6 Bonus check + decision + final report...", flush=True)
    cell_control_check = check_cell_controls_vs_prior_study(
        cell_control_summaries
    )
    control_reproduction["cell_controls_vs_prior_study"] = cell_control_check

    gate_primary = gates["primary_25bps"]
    gate_stress = gates["stress_50bps"]
    if gate_primary["passed"] and gate_stress["passed"]:
        decision = "diversifier_screening_pass_both_costs_scope_execution_phase"
    elif gate_primary["passed"]:
        decision = (
            "diversifier_screening_pass_primary_cost_only_scope_with_cost_caution"
        )
    else:
        decision = "diversifier_screening_fail_do_not_scope_execution_phase"

    n_pass = sum(r["passed"] for r in gate_primary["cells"])
    n_dd_ok = sum(
        r["checks"]["max_dd_within_noise"] for r in gate_primary["cells"]
    )
    cell_lines = []
    for cell, diag in zip(gate_primary["cells"], diagnostics["primary_25bps"]):
        failed_checks = [k for k, v in cell["checks"].items() if not v]
        cell_lines.append(
            f"{cell['window']}/{cell['profile']}: "
            f"{'PASS' if cell['passed'] else 'FAIL'}"
            + (f" on {'+'.join(failed_checks)}" if failed_checks else "")
            + f" (d_sharpe {diag['d_sharpe']:+}, d_cagr {diag['d_cagr_pp']:+}pp,"
            f" dd {diag['dd_reading']}, corr {diag['stream_correlation']})"
        )
    go_no_go = (
        f"Diversifier gate at primary 25bps: {n_pass}/4 cells passed "
        f"(overall {'PASS' if gate_primary['passed'] else 'FAIL'}); stress "
        f"50bps: {sum(r['passed'] for r in gate_stress['cells'])}/4 "
        f"(overall {'PASS' if gate_stress['passed'] else 'FAIL'}). "
        f"max_dd_within_noise passed in {n_dd_ok}/4 primary cells. "
        f"Per cell (primary): " + " | ".join(cell_lines)
    )

    payload = {
        "decision": decision,
        "decision_meaning": (
            "Screening-tier evidence only, under the diversifier objective "
            "class. A pass means the evidence justifies SCOPING the "
            "(separately planned) 24/7 execution-design phase. It does not "
            "authorize any config change, order path, or allocation — "
            "deployment requires a separate decision plus an execution "
            "build that is explicitly out of scope, and the prior "
            "return_enhancer verdict in reports/crypto_trend_study.json "
            "stands unedited as the answer to its own, different question."
        ),
        "go_no_go": go_no_go,
        "conditioning_disclosure": disclosure,
        "pre_registration": pre_registration,
        "pre_registration_protocol": (
            "This report file was first written with ONLY the decision "
            "placeholder, conditioning_disclosure, pre_registration, and "
            "control_reproduction blocks — before any gate result, noise "
            "band, or per-cell correlation was computed — then extended "
            "with results. The pre_registration block is byte-identical "
            "between the two writes (same in-memory object serialized "
            "twice)."
        ),
        "control_reproduction": control_reproduction,
        "cell_coverage": cell_coverage,
        "performance": performance,
        "gate_primary_25bps": gate_primary,
        "gate_stress_50bps": gate_stress,
        "diagnostics": {
            "note": (
                "Non-gating legibility aids: each cell's paired-bootstrap "
                "1-sigma noise band (pp of max drawdown difference), the "
                "actual drawdown difference in pp and in units of that "
                "band, the measured per-cell stream correlation, and the "
                "Sharpe/CAGR deltas."
            ),
            **diagnostics,
        },
        "limitations": limitations(),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nDecision: {decision}")
    print(go_no_go)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
