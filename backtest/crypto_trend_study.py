"""Pre-registered crypto trend-sleeve study: TSMOM long/flat on BTC/ETH.

Why this exists: backtest/frontier_study.py screened crypto trend positively
but shelved it on two specific limitations its own report recorded —
"crypto streams carry zero modeled transaction cost" and "crypto history
covers only 2021 onward, one regime." This study addresses both directly:
real pre-registered cost levels (25 bps primary / 50 bps stress per one-way
turnover unit), and a 2021-2026 sample that now spans three distinct regimes
(2021 mania, the 2022 -77% BTC crash, 2023 chop, the 2024+ recovery) rather
than the single regime available when frontier_study ran. It also replaces
frontier_study's ad-hoc 100-day moving-average variant with the exact
production TSMOM mechanic (engine/portfolio.py tsmom_targets /
backtest/production_portfolio.py tsmom_stream): long iff trailing
252-row return > 0, inverse-vol weighted across the assets that are on,
unlit weight = cash.

Pre-registration (declared before any results were computed)
------------------------------------------------------------
* PRIMARY candidate: long/flat TSMOM on BTC/USD + ETH/USD. Signal: trailing
  252-row return > 0 on completed daily bars; weights inverse-vol
  (63-row rolling std, min_periods 40, annualized floor 0.04) among lit
  assets, renormalized to 1, applied with a one-day lag (weights.shift(1))
  — byte-for-byte the tsmom_stream() math, proven below by control
  reproduction on the equity ETF universe before it is trusted on crypto.
  NOTE: on a 24/7 market, 252 rows ~= 8.3 calendar months, not 12; kept
  row-based deliberately to mirror the accepted implementation's math
  exactly rather than introducing an untested calendar variant.
* SENSITIVITY-ONLY variants (not promotable, reported for robustness):
  90-row and 180-row lookbacks at primary cost.
* Costs: 25 bps per unit of one-way turnover primary, 50 bps stress.
  These are pre-registered planning figures — Alpaca crypto spreads are
  much wider than equities and NO real paper fill has verified either
  number; verification against actual paper fills is a hard precondition
  to any deployment.
* Daily-close convention: Alpaca crypto "1Day" bars are stamped at UTC
  midnight and cover the UTC calendar day; the still-forming current UTC
  day is dropped, and all data is capped at 2026-08-12 so the frozen
  2026-08-13+ validation window is untouched (AGENTS.md).
* Marginal test: 5% allocation funded pro-rata from all existing sleeves
  (candidate = 0.95 * control + 0.05 * crypto stream, per profile), crypto
  returns compounded onto the equity trading calendar via
  resample_returns. Before the crypto data exists the candidate is
  identical to the control (the sleeve cannot be funded before its data
  starts); during the signal warm-up the 5% sits in modeled-0 cash, the
  same idle-cash convention as everywhere else in this repo.
* Objective class: return_enhancer (AGENTS.md default), evaluated via
  backtest.promotion.passes_gate_all_cells on both profiles and both
  screening windows. Screening-tier evidence only — crypto data begins
  2021-01-01, so the early_2020_2022 cell is PARTIAL for the crypto sleeve
  (the actual coverage of every cell is recorded in the report); nothing
  here is evidence against the frozen 2026-08-13+ window, and no config
  change follows from this study. Execution-path design (24/7 market vs
  the bot's equity-hours cron) is explicitly a later, separately-planned
  phase; the decision field only says whether scoping that phase is
  justified.
"""

from __future__ import annotations

import datetime as dt
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
    resample_returns,
    returns_summary,
    tsmom_stream,
)
from backtest.promotion import passes_gate_all_cells
from engine.crypto_data import completed_daily_closes, fetch_daily_bars
from engine.tiingo import load_parquet

TD_CRYPTO = 365  # 24/7 market: ~365 daily observations per year

CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]
DATA_START = dt.date(2021, 1, 1)
FROZEN_WINDOW_START = "2026-08-13"  # AGENTS.md frozen validation window — excluded

PRIMARY_LOOKBACK = 252
SENSITIVITY_LOOKBACKS = (90, 180)
PRIMARY_COST_BPS = 25.0
STRESS_COST_BPS = 50.0
CRYPTO_ALLOCATION = 0.05

REGIMES = {
    "mania_2021": slice("2021-01-01", "2021-12-31"),
    "crash_2022": slice("2022-01-01", "2022-12-31"),
    "chop_2023": slice("2023-01-01", "2023-12-31"),
    "recovery_2024_plus": slice("2024-01-01", None),
}
SCREEN_WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}


# --------------------------------------------------------------------------
# Stream construction — one parameterized implementation of the accepted math
# --------------------------------------------------------------------------


def tsmom_stream_from_closes(
    close: pd.DataFrame,
    *,
    lookback: int,
    cost_bps: float,
) -> tuple[pd.Series, pd.DataFrame]:
    """production_portfolio.tsmom_stream()'s exact construction,
    parameterized by close panel, lookback, and cost so the control
    reproduction below and the crypto variant share one implementation.
    (The sqrt(TD) inside the inverse-vol annualization cancels in the
    cross-sectional renormalization, so using it unchanged on a 365-day
    market alters nothing except the — never binding for crypto — 0.04
    vol floor.)"""
    rets = close.pct_change()
    signal = (close / close.shift(lookback) - 1).gt(0)
    inverse_vol = 1 / (
        rets.rolling(63, min_periods=40).std() * np.sqrt(TD)
    ).clip(lower=0.04)
    weights = signal * inverse_vol
    weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    weights = weights.shift(1)
    gross = (weights * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    return gross - turnover * cost_bps / 10_000, weights


def validate_control_reproduction() -> dict:
    """AGENTS.md "validate a new study against the accepted one": prove the
    parameterized reimplementation reproduces production tsmom_stream() on
    the equity ETF universe before trusting the crypto variant built on it."""
    from engine.config import load_config

    cfg = load_config()
    symbols = cfg.sleeves_paper["tsmom_universe"]
    frames = load_parquet(symbols, Path("state/history_assets"))
    close = pd.DataFrame(
        {sym: norm_index(frame["close"]) for sym, frame in frames.items()}
    )
    mine, _ = tsmom_stream_from_closes(close, lookback=252, cost_bps=8.0)
    theirs = norm_index(tsmom_stream())
    diff = (norm_index(mine) - theirs).abs()
    return {
        "method": (
            "Recompute the equity TSMOM sleeve with this study's "
            "parameterized tsmom_stream_from_closes(lookback=252, cost_bps=8) "
            "and diff daily returns against production_portfolio.tsmom_stream()."
        ),
        "n_days_compared": int(diff.notna().sum()),
        "max_abs_daily_return_diff": float(diff.max()),
        "reproduced": bool(diff.max() < 1e-12),
    }


def load_crypto_closes() -> pd.DataFrame:
    frames = fetch_daily_bars(CRYPTO_SYMBOLS, DATA_START)
    closes = pd.DataFrame(
        {
            sym.split("/")[0]: norm_index(completed_daily_closes(frame))
            for sym, frame in frames.items()
        }
    )
    return closes.loc[: pd.Timestamp(FROZEN_WINDOW_START) - pd.Timedelta(days=1)]


def summarize(r: pd.Series, label: str, periods_per_year: int) -> dict:
    """returns_summary's shape with an explicit annualization factor —
    raw crypto streams have ~365 observations/year, so sqrt(252) vol and
    252x mean annualization would misstate them."""
    r = r.dropna()
    if r.empty:
        return {"portfolio": label, "empty": True}
    equity = (1 + r).cumprod()
    years = max((r.index[-1] - r.index[0]).days / 365.25, 1 / 365.25)
    vol = float(r.std() * np.sqrt(periods_per_year))
    return {
        "portfolio": label,
        "from": r.index[0].date().isoformat(),
        "to": r.index[-1].date().isoformat(),
        "years": round(years, 2),
        "cagr": round(float(equity.iloc[-1] ** (1 / years) - 1), 4),
        "ann_return": round(float(r.mean() * periods_per_year), 4),
        "vol": round(vol, 4),
        "sharpe": round(float(r.mean() * periods_per_year / vol), 3) if vol else 0.0,
        "max_dd": round(float((equity / equity.cummax() - 1).min()), 4),
        "x_money": round(float(equity.iloc[-1]), 3),
    }


def coverage(weights: pd.DataFrame, closes: pd.DataFrame, slicer: slice, lookback: int) -> dict:
    """Honest per-window coverage: a lookback-warm-up day is weight 0 and
    indistinguishable from signal-off in the return stream, so record how
    much of each window the signal was actually defined and how much it
    was long."""
    w = weights.loc[slicer]
    valid = closes.shift(lookback).notna().shift(1).loc[slicer]
    if w.empty:
        return {"n_days": 0}
    any_valid = valid.any(axis=1)
    return {
        "n_days": int(len(w)),
        "window_from": w.index[0].date().isoformat(),
        "window_to": w.index[-1].date().isoformat(),
        "n_days_signal_defined": int(any_valid.sum()),
        "n_days_any_long": int((w.sum(axis=1) > 0).sum()),
        "avg_gross_when_defined": round(float(w.sum(axis=1)[any_valid].mean()), 4)
        if any_valid.any()
        else 0.0,
    }


def main() -> None:
    out_path = Path("reports/crypto_trend_study.json")

    print("1/5 Control reproduction (equity TSMOM sleeve)...", flush=True)
    reproduction = validate_control_reproduction()
    print(
        f"    max_abs_daily_return_diff={reproduction['max_abs_daily_return_diff']:.2e} "
        f"reproduced={reproduction['reproduced']}"
    )
    if not reproduction["reproduced"]:
        raise SystemExit(
            "Control reproduction FAILED — crypto variant results would be "
            "untrustworthy; not proceeding."
        )

    print("2/5 Crypto streams...", flush=True)
    closes = load_crypto_closes()
    rets = closes.pct_change()

    primary, primary_w = tsmom_stream_from_closes(
        closes, lookback=PRIMARY_LOOKBACK, cost_bps=PRIMARY_COST_BPS
    )
    stress, _ = tsmom_stream_from_closes(
        closes, lookback=PRIMARY_LOOKBACK, cost_bps=STRESS_COST_BPS
    )
    sensitivity = {
        lb: tsmom_stream_from_closes(closes, lookback=lb, cost_bps=PRIMARY_COST_BPS)[0]
        for lb in SENSITIVITY_LOOKBACKS
    }
    # Single-asset BTC trend, for the crash-2022 hold-vs-trend comparison.
    btc_trend, _ = tsmom_stream_from_closes(
        closes[["BTC"]], lookback=PRIMARY_LOOKBACK, cost_bps=PRIMARY_COST_BPS
    )

    print("3/5 Standalone + per-regime tables...", flush=True)
    standalone = {
        "full_period": [
            summarize(primary, f"crypto trend {PRIMARY_COST_BPS:.0f}bps (PRIMARY)", TD_CRYPTO),
            summarize(stress, f"crypto trend {STRESS_COST_BPS:.0f}bps (stress)", TD_CRYPTO),
            *[
                summarize(s, f"sensitivity lookback={lb} @25bps", TD_CRYPTO)
                for lb, s in sensitivity.items()
            ],
            summarize(rets["BTC"], "BTC buy & hold", TD_CRYPTO),
            summarize(rets["ETH"], "ETH buy & hold", TD_CRYPTO),
        ]
    }

    regimes = {}
    for name, slicer in REGIMES.items():
        regimes[name] = {
            "coverage": coverage(primary_w, closes, slicer, PRIMARY_LOOKBACK),
            "rows": [
                summarize(primary.loc[slicer], "crypto trend 25bps (PRIMARY)", TD_CRYPTO),
                summarize(stress.loc[slicer], "crypto trend 50bps (stress)", TD_CRYPTO),
                *[
                    summarize(s.loc[slicer], f"sensitivity lookback={lb} @25bps", TD_CRYPTO)
                    for lb, s in sensitivity.items()
                ],
                summarize(rets["BTC"].loc[slicer], "BTC buy & hold", TD_CRYPTO),
                summarize(rets["ETH"].loc[slicer], "ETH buy & hold", TD_CRYPTO),
            ],
        }

    crash = REGIMES["crash_2022"]
    crash_2022_focus = {
        "note": (
            "The load-bearing cell: a trend filter's value proposition is "
            "sidestepping exactly this drawdown. BTC-only trend vs BTC "
            "buy-and-hold, 2022 calendar year, primary cost."
        ),
        "btc_hold": summarize(rets["BTC"].loc[crash], "BTC hold 2022", TD_CRYPTO),
        "btc_trend": summarize(btc_trend.loc[crash], "BTC trend 2022 (25bps)", TD_CRYPTO),
        "sleeve_trend": summarize(primary.loc[crash], "BTC+ETH trend 2022 (25bps)", TD_CRYPTO),
    }

    print("4/5 Marginal addition to production portfolio (build_streams)...", flush=True)
    streams = build_streams()
    raw = (
        0.40 * streams["spy"]
        + 0.25 * streams["tsmom"]
        + 0.20 * streams["trend"]
        + 0.30 * streams["mom_ls"]
    )
    controls = {
        "base": raw - (0.15 * SHORT_BORROW / TD),
        "2x": 2 * raw - (MARGIN_RATE / TD) - (0.30 * SHORT_BORROW / TD),
    }

    def aligned(stream: pd.Series) -> pd.Series:
        return resample_returns(norm_index(stream), streams.index)

    def candidate_for(control: pd.Series, crypto_aligned: pd.Series) -> pd.Series:
        blended = (1 - CRYPTO_ALLOCATION) * control + CRYPTO_ALLOCATION * crypto_aligned
        return blended.where(crypto_aligned.notna(), control)

    crypto_primary_aligned = aligned(primary)
    crypto_stress_aligned = aligned(stress)

    gate_inputs = {}
    performance = {}
    cell_coverage = {}
    for cost_label, crypto_aligned in (
        ("primary_25bps", crypto_primary_aligned),
        ("stress_50bps", crypto_stress_aligned),
    ):
        cells = []
        performance[cost_label] = {}
        for window, slicer in SCREEN_WINDOWS.items():
            rows = []
            for profile, control in controls.items():
                control_summary = returns_summary(
                    control.loc[slicer], f"{profile} control (no crypto)"
                )
                candidate = candidate_for(control, crypto_aligned)
                candidate_summary = returns_summary(
                    candidate.loc[slicer], f"{profile} +5% crypto trend ({cost_label})"
                )
                rows.append({"variant": "control", "profile": profile, **control_summary})
                rows.append({"variant": "candidate", "profile": profile, **candidate_summary})
                cells.append((window, profile, control_summary, candidate_summary))
            performance[cost_label][window] = rows
        gate_inputs[cost_label] = passes_gate_all_cells(cells, "return_enhancer")

    first_signal = closes.shift(PRIMARY_LOOKBACK).notna().any(axis=1)
    first_signal_date = first_signal[first_signal].index[0].date().isoformat()
    for window, slicer in SCREEN_WINDOWS.items():
        win = streams.index.to_series().loc[slicer]
        overlap = crypto_primary_aligned.loc[slicer].dropna()
        cell_coverage[window] = {
            "portfolio_window": [win.index[0].date().isoformat(), win.index[-1].date().isoformat()],
            "crypto_stream_overlap": (
                [overlap.index[0].date().isoformat(), overlap.index[-1].date().isoformat()]
                if len(overlap)
                else None
            ),
            "crypto_signal_defined_from": first_signal_date,
            "note": (
                "Candidate is identical to control on portfolio days before "
                "the crypto stream exists; between crypto data start and "
                "signal-defined date the 5% sleeve sits in modeled-0 cash "
                "(warm-up)."
            ),
        }

    print("5/5 Correlation diagnostics...", flush=True)
    live = crypto_primary_aligned.dropna()
    live = live[live.index >= pd.Timestamp(first_signal_date)]

    def corr(a: pd.Series, b: pd.Series) -> dict:
        j = pd.DataFrame({"a": a, "b": b}).dropna()
        return {"corr": round(float(j["a"].corr(j["b"])), 4), "n_days": int(len(j))}

    correlations = {
        "note": (
            "Daily correlations on the equity trading calendar (crypto "
            "returns compounded onto it), restricted to days after the "
            "crypto 252-row signal is first defined so warm-up zeros do "
            "not dilute the estimate."
        ),
        "crypto_trend_vs_production_base": corr(live, controls["base"]),
        "crypto_trend_vs_spy": corr(live, streams["spy"]),
        "crypto_trend_vs_equity_tsmom_sleeve": corr(live, streams["tsmom"]),
        "btc_hold_vs_spy_reference": corr(aligned(rets["BTC"]), streams["spy"]),
    }

    gate_primary = gate_inputs["primary_25bps"]
    gate_stress = gate_inputs["stress_50bps"]
    if gate_primary["passed"] and gate_stress["passed"]:
        decision = "screening_pass_both_costs_scope_execution_phase"
    elif gate_primary["passed"]:
        decision = "screening_pass_primary_cost_only_scope_with_cost_caution"
    else:
        decision = "screening_fail_do_not_scope_execution_phase"

    payload = {
        "decision": decision,
        "decision_meaning": (
            "Screening-tier evidence only. A pass means the evidence "
            "justifies SCOPING the (separately planned) execution-design "
            "phase — 24/7 crypto market vs the bot's daily equity-hours "
            "cron, real fill-cost verification, sleeve plumbing. It does "
            "not authorize any config change, order path, or allocation."
        ),
        "pre_registration": {
            "primary_candidate": (
                "TSMOM long/flat on BTC/USD + ETH/USD: long iff trailing "
                "252-row return > 0 on completed UTC-daily bars, "
                "inverse-vol weighted (63-row vol, floor 0.04), weights "
                "lagged one day, unlit = cash — the exact tsmom_stream() "
                "mechanic, control-reproduced before use."
            ),
            "sensitivity_only_variants": "90-row and 180-row lookbacks (not promotable)",
            "costs_bps_one_way": {"primary": PRIMARY_COST_BPS, "stress": STRESS_COST_BPS},
            "cost_caveat": (
                "Planning figures only — Alpaca crypto spreads are much "
                "wider than equities and neither number has been verified "
                "against real paper fills; that verification is a hard "
                "precondition to any deployment."
            ),
            "daily_close_convention": (
                "Alpaca crypto 1Day bars, stamped at UTC midnight, covering "
                "the UTC calendar day; still-forming current UTC day "
                "dropped; data capped at 2026-08-12 (frozen 2026-08-13+ "
                "window untouched)."
            ),
            "marginal_test": (
                "5% allocation funded pro-rata from all sleeves "
                "(candidate = 0.95*control + 0.05*crypto), crypto returns "
                "compounded onto the equity trading calendar; candidate == "
                "control before crypto data exists."
            ),
            "objective_class": "return_enhancer",
            "promotion_rule": (
                "passes_gate_all_cells: higher Sharpe, CAGR not lower, max "
                "drawdown not worse, both profiles x both screening windows, "
                "at primary cost. Stress-cost gate reported alongside. "
                "Screening only — never a direct promotion."
            ),
            "annualization": (
                "Raw crypto streams annualized at 365 periods/year; "
                "portfolio-aligned streams keep the repo's 252 convention."
            ),
        },
        "control_reproduction": reproduction,
        "data": {
            "source": "Alpaca /v1beta3/crypto/us/bars via engine/crypto_data.py, cached state/crypto/",
            "symbols": CRYPTO_SYMBOLS,
            "span": [closes.index[0].date().isoformat(), closes.index[-1].date().isoformat()],
            "n_days": int(len(closes)),
            "history_floor_note": (
                "Alpaca crypto bar history starts 2021-01-01 (verified "
                "directly against the endpoint) — ~5.5 years, three regimes."
            ),
        },
        "standalone": standalone,
        "regimes": regimes,
        "crash_2022_focus": crash_2022_focus,
        "marginal_addition": {
            "allocation": CRYPTO_ALLOCATION,
            "cell_coverage": cell_coverage,
            "performance": performance,
            "gate_primary_25bps": gate_primary,
            "gate_stress_50bps": gate_stress,
        },
        "correlations": correlations,
        "limitations": [
            "Only ~5.5 years of crypto history (2021-01-01 floor at Alpaca) "
            "— three regimes but a single full trend cycle; far short of the "
            "multi-decade evidence behind the equity TSMOM sleeve.",
            "The early_2020_2022 gate cell is PARTIAL for crypto: the "
            "portfolio window starts 2020-07-27 but crypto data starts "
            "2021-01-01 and the 252-row signal is first defined ~2021-09; "
            "see marginal_addition.cell_coverage for exact spans.",
            "25/50 bps costs are pre-registered planning figures; Alpaca "
            "paper crypto fill quality is UNVERIFIED — real paper fills "
            "must validate them before any deployment.",
            "Crypto trades 24/7 while the bot's cron runs on equity market "
            "hours; how/when a daily crypto rebalance would actually "
            "execute is an open execution-design question deferred to a "
            "separately planned phase, and this study models UTC-midnight "
            "rebalances that the current infrastructure cannot place.",
            "252-row lookback on a 24/7 market is ~8.3 calendar months, "
            "not 12 — kept deliberately to mirror the accepted "
            "implementation's row-based math.",
            "Risk-free rate modeled as zero throughout (AGENTS.md open "
            "gap); identical on both sides of every comparison here.",
            "Long/flat only, no shorting, no intra-day stops; weekend "
            "crypto returns are compounded into the next equity trading "
            "day for the marginal test, which slightly smooths measured "
            "correlation vs a true 24/7 accounting.",
        ],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nDecision: {decision}")
    print(
        f"Gate primary passed: {gate_primary['passed']} | "
        f"stress passed: {gate_stress['passed']}"
    )
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
