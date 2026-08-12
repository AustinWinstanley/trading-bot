"""Screening study: drop FXE from the TSMOM universe (15 assets -> 14).

Premise (live-journal audit, `state/paper.db` / `state/paper_2x.db`, checked
2026-08-12): every `sleeve='tsmom'` rejection recorded since sleeve tagging
began on 2026-08-04 is FXE (8/8 in each profile), always for the same reason
-- `20d avg dollar volume ~$240k-$360k below the $1,000,000
tsmom_min_dollar_volume floor`. FXE's requested notional at those rejections
is consistently ~3.5% of base equity and ~7.1% of 2x equity (most recent
observation: $359.93 / $10,164.78 = 3.54% base; $728.18 / $10,281.87 = 7.08%
2x -- matching the audited 3.5%/7.1% figures almost exactly). That capital is
never deployed: it sits as unproductive cash every time TSMOM would otherwise
have sized a long FXE position.

This is a DIFFERENT question from `tsmom_liquidity_alignment_study.py`
(see `reports/tsmom_liquidity_alignment_study.json`, verdict `no_effect`/
`all_no_effect`). That study changed *when* the same $1M liquidity floor is
applied (pre- vs post-normalization) and found it changed nothing, because
FXE's volume is nowhere near the threshold either way -- a timing artifact
was ruled out, not FXE's presence in the universe. This study does not touch
liquidity-floor logic at all. It asks a blunter question: what happens to
risk-adjusted returns, and to that stranded cash, if FXE is simply removed
from `tsmom_universe` so its target allocation flows to the other 14 assets
instead of the floor rejecting it every time?

Objective class: return_enhancer (AGENTS.md default), evaluated the standard
way -- higher Sharpe, CAGR not lower, max drawdown not worse, in both
`early_2020_2022` and `heldout_2023_plus` windows and both `base`/`2x`
profiles, via `backtest.promotion.passes_gate_all_cells`. This is
screening-tier evidence only (AGENTS.md: 2026-08-04..2026-08-12 is demoted to
screening status, and this study uses cached history through 2026-07-22
regardless -- 2026-08-13 onward is the newly re-frozen final-validation
window and is not used here). It is explicitly NOT a hard-gate promotion
candidate; the relevant bar is the lighter `config_2x.yaml` experiment tier
(AGENTS.md "the experiment tier is a second, lighter-weight evidence bar").
A near-zero performance delta is a legitimate, reportable outcome here: the
independent case for dropping FXE is the cash-drag/redeployment argument
below, which does not depend on the backtest showing a Sharpe improvement --
the backtest below cannot even see the cash-drag effect directly, since (per
repo convention, see AGENTS.md's short-borrow section) both control and
candidate model idealized fractional fills for every included asset. FXE's
live unfillability is a fact about the $10k paper account and the liquidity
gate, not something `tsmom_stream`'s frictionless weights model.
"""

from __future__ import annotations

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
    require_history,
    returns_summary,
)
from backtest.promotion import passes_gate_all_cells
from engine.config import load_config
from engine.tiingo import load_parquet

WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}
DROP_SYMBOL = "FXE"
SLEEVE_ALLOCATION = 0.25  # paper_portfolio.sleeves.tsmom, both configs
GROSS_LEVERAGE = {"base": 1.0, "2x": 2.0}  # config.yaml / config_2x.yaml gross_leverage

# Live-journal cash-drag evidence, most recent observation as of 2026-08-12
# (state/paper.db, state/paper_2x.db; sleeve='tsmom' rejections, symbol='FXE').
LIVE_FXE_REJECTIONS = {
    "base": {"n_rejections_since_sleeve_tagging": 8, "requested_notional": 359.933126, "equity": 10164.78},
    "2x": {"n_rejections_since_sleeve_tagging": 8, "requested_notional": 728.182871, "equity": 10281.87},
}


def tsmom_stream_for_universe(symbols: list[str]) -> tuple[pd.Series, pd.DataFrame]:
    """`production_portfolio.tsmom_stream`'s construction, parameterized by
    universe instead of reading `cfg.sleeves_paper["tsmom_universe"]`
    directly, so control (15 assets) and candidate (14, FXE dropped) share
    one implementation. Weights are inverse-vol among assets with a positive
    12m signal, renormalized to sum to 1 each day -- dropping a symbol from
    `symbols` means it is simply never a column here, so its would-be share
    is redistributed across the remaining assets automatically, in
    proportion to their own inverse-vol weights. That is the renormalization
    convention this study uses: no separate reweighting step, just the
    existing mechanism operating on one fewer column.
    """
    frames = load_parquet(symbols, Path("state/history_assets"))
    require_history(symbols, frames, label="TSMOM (fxe-drop study)")
    close = pd.DataFrame({s: norm_index(frame["close"]) for s, frame in frames.items()})
    rets = close.pct_change()
    signal = (close / close.shift(252) - 1).gt(0)
    inverse_vol = 1 / (
        rets.rolling(63, min_periods=40).std() * np.sqrt(TD)
    ).clip(lower=0.04)
    weights = signal * inverse_vol
    weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    weights = weights.shift(1)
    gross = (weights * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    stream = gross - turnover * 8 / 10_000
    return stream, weights


def full_portfolio(streams: pd.DataFrame) -> dict[str, pd.Series]:
    """Reproduces `production_portfolio.main()`'s base/2x construction."""
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


def validate_control_reproduction(control_streams: pd.DataFrame) -> dict:
    """Per AGENTS.md "Validate a new study against the accepted one": prove
    the unmodified control reproduces the recorded production figures before
    trusting the FXE-drop variant built on the same machinery."""
    recorded = json.loads(Path("reports/production_portfolio.json").read_text())
    recorded_by_label = {r["portfolio"]: r for r in recorded["results"]}
    portfolios = full_portfolio(control_streams)
    fields = ("sharpe", "cagr", "max_dd", "ann_return", "vol")
    out = {}
    for label, key in (
        ("production SPY-core base (1.15x gross)", "base"),
        ("production SPY-core 2x (2.30x gross)", "2x"),
    ):
        recomputed = returns_summary(portfolios[key], key)
        ref = recorded_by_label[label]
        diffs = {f: round(abs(recomputed[f] - ref[f]), 8) for f in fields}
        out[key] = {
            "recomputed": recomputed,
            "recorded": ref,
            "abs_diffs": diffs,
            "max_abs_diff": max(diffs.values()),
        }
    return out


def cash_drag_analysis(control_weights: pd.DataFrame, candidate_weights: pd.DataFrame) -> dict:
    """Cross-checks the live-journal cash-drag figures against the
    frictionless backtest model's own implied FXE target weight, and
    quantifies where FXE's weight goes once dropped.

    These are two different quantities and are reported as such: the live
    figures are the *actual* requested notional at specific rejection
    events (always while FXE's signal was on); the backtest figures are the
    full-sample average sleeve weight the idealized model assigns FXE,
    including the days its 12m signal is flat (weight 0). They should be in
    the same ballpark but need not match -- the backtest has no whole-share
    or liquidity-floor rejection concept at all, by the idealized-fill
    convention documented in AGENTS.md.
    """
    fxe_on = control_weights[DROP_SYMBOL] > 0
    fxe_avg_weight_when_on = float(control_weights.loc[fxe_on, DROP_SYMBOL].mean())
    fxe_avg_weight_unconditional = float(control_weights[DROP_SYMBOL].mean())
    pct_days_signal_on = float(fxe_on.mean())
    avg_n_assets_long = float((control_weights > 0).sum(axis=1).mean())

    live = {}
    for profile, obs in LIVE_FXE_REJECTIONS.items():
        live[profile] = {
            **obs,
            "requested_notional_pct_of_equity": round(
                100 * obs["requested_notional"] / obs["equity"], 2
            ),
        }

    model_implied = {}
    for profile, leverage in GROSS_LEVERAGE.items():
        model_implied[profile] = {
            "avg_target_weight_when_signal_on_pct": round(
                100 * SLEEVE_ALLOCATION * leverage * fxe_avg_weight_when_on, 3
            ),
            "avg_target_weight_unconditional_pct": round(
                100 * SLEEVE_ALLOCATION * leverage * fxe_avg_weight_unconditional, 3
            ),
        }

    redeployed = candidate_weights.mean() - control_weights.drop(columns=[DROP_SYMBOL]).mean()
    redeployed = redeployed.sort_values(ascending=False)
    top_beneficiaries = {
        symbol: round(float(delta), 5) for symbol, delta in redeployed.head(6).items()
    }

    return {
        "live_journal_evidence": {
            "source": "state/paper.db, state/paper_2x.db (sleeve='tsmom' rejections)",
            "finding": (
                "Every tsmom-sleeve rejection since sleeve tagging began "
                "2026-08-04 is FXE (8/8 base, 8/8 2x); no other tsmom asset "
                "has ever been rejected. Reason is always the $1,000,000 "
                "20d-avg-dollar-volume floor; FXE's trailing volume is "
                "consistently ~$240k-$360k."
            ),
            "most_recent_observation_pct_of_equity": live,
        },
        "backtest_model_cross_check": {
            "note": (
                "Idealized frictionless model; cannot reproduce a fill "
                "rejection, only FXE's average target weight when the "
                "control (15-asset) model would have wanted to hold it."
            ),
            "pct_days_fxe_signal_on": round(100 * pct_days_signal_on, 2),
            "avg_n_assets_concurrently_long": round(avg_n_assets_long, 2),
            "avg_fxe_weight_within_tsmom_sleeve_when_signal_on": round(
                fxe_avg_weight_when_on, 4
            ),
            "model_implied_account_level_weight": model_implied,
        },
        "redeployment_on_drop": {
            "note": (
                "100% of FXE's weight within the TSMOM sleeve is "
                "renormalized across the other 14 assets on days it would "
                "have been selected (inverse-vol weighting mechanism, "
                "unchanged) -- these are the average per-asset target-weight "
                "increases (candidate minus control), account fraction "
                "before the 0.25 sleeve allocation and gross_leverage."
            ),
            "top_beneficiaries_avg_weight_increase": top_beneficiaries,
        },
    }


def main() -> None:
    cfg = load_config()
    universe = list(cfg.sleeves_paper["tsmom_universe"])
    if DROP_SYMBOL not in universe:
        raise ValueError(f"{DROP_SYMBOL} not found in configured tsmom_universe: {universe}")
    candidate_universe = [s for s in universe if s != DROP_SYMBOL]

    print("Building production streams (control)...", flush=True)
    base_streams = build_streams()  # unmodified production code -> the control

    print("Validating control reproduction against reports/production_portfolio.json...", flush=True)
    reproduction = validate_control_reproduction(base_streams)
    for profile, check in reproduction.items():
        print(f"  {profile}: max_abs_diff={check['max_abs_diff']}")

    print("Recomputing 15-asset TSMOM via the parameterized reimplementation...", flush=True)
    control_tsmom, control_weights = tsmom_stream_for_universe(universe)
    reimpl_diff = float(
        (norm_index(control_tsmom) - norm_index(base_streams["tsmom"])).abs().max()
    )
    print(f"  reimplementation_max_abs_diff vs production tsmom_stream(): {reimpl_diff}")

    print("Building 14-asset (FXE dropped) TSMOM candidate...", flush=True)
    candidate_tsmom, candidate_weights = tsmom_stream_for_universe(candidate_universe)

    control_streams = base_streams.copy()
    candidate_streams = base_streams.drop(columns=["tsmom"]).copy()
    candidate_streams["tsmom"] = norm_index(candidate_tsmom)
    candidate_streams = candidate_streams.dropna()
    control_streams = control_streams.dropna()

    control_portfolios = full_portfolio(control_streams)
    candidate_portfolios = full_portfolio(candidate_streams)

    performance = {}
    cells = []
    for window, slicer in WINDOWS.items():
        rows = []
        for profile in ("base", "2x"):
            control_summary = returns_summary(
                control_portfolios[profile].loc[slicer], f"{profile} control (15-asset, FXE included)"
            )
            candidate_summary = returns_summary(
                candidate_portfolios[profile].loc[slicer], f"{profile} candidate (14-asset, FXE dropped)"
            )
            control_summary["profile"] = profile
            candidate_summary["profile"] = profile
            rows.append({"variant": "control_15_asset", **control_summary})
            rows.append({"variant": "candidate_14_asset", **candidate_summary})
            cells.append((window, profile, control_summary, candidate_summary))
        performance[window] = rows

    gate = passes_gate_all_cells(cells, "return_enhancer")

    # Standalone TSMOM-sleeve-only comparison (diagnostic, not gating).
    # TSMOM's own asset-ETF history runs back to 2002 (TLT) -- much further
    # than the production portfolio's combined index, which is bounded by
    # the shorter cross-sectional (clone/mom_ls) panel. Slicing the raw
    # stream on WINDOWS directly would mislabel "early_2020_2022" as a
    # 20-year window instead of the ~2020-2022 span the name implies, so
    # this reuses the same portfolio-aligned index as the gated comparison.
    standalone = {}
    for window, slicer in WINDOWS.items():
        standalone[window] = [
            returns_summary(control_streams["tsmom"].loc[slicer], "tsmom control (15-asset)"),
            returns_summary(candidate_streams["tsmom"].loc[slicer], "tsmom candidate (14-asset)"),
        ]

    drag = cash_drag_analysis(control_weights, candidate_weights)

    if gate["all_no_effect"]:
        decision = "screening_no_effect"
    elif gate["passed"]:
        decision = "screening_supports_drop"
    else:
        # Not a hard-gate rejection in the promotion sense -- this study was
        # never eligible for the hard gate. Report the shortfall plainly and
        # let the cash-drag argument be judged on its own terms.
        decision = "screening_mixed_defer_to_cash_drag_case"

    payload = {
        "decision": decision,
        "decision_meaning": (
            "This is screening-tier evidence for the config_2x.yaml "
            "experiment tier, not a hard-gate promotion decision -- dropping "
            "FXE from tsmom_universe in config_2x.yaml is a follow-up a "
            "human makes after reviewing this report, not an action this "
            "study takes."
        ),
        "pre_registration": {
            "objective_class": "return_enhancer",
            "candidate": (
                "Drop FXE from paper_portfolio.tsmom_universe (15 -> 14 "
                "assets); weights renormalize automatically via the "
                "existing inverse-vol mechanism in tsmom_stream()."
            ),
            "promotion_rule": (
                "Higher Sharpe, no lower CAGR, no worse max drawdown, in "
                "both account profiles and both screening windows "
                "(AGENTS.md hard-gate bar, evaluated for reference only -- "
                "see decision_meaning)."
            ),
            "scope": (
                "Screening only. Cached history ends 2026-07-22; "
                "2026-08-04 onward is demoted-to-screening per AGENTS.md, "
                "and 2026-08-13 onward is the newly re-frozen "
                "final-validation window, not used or available here."
            ),
            "expected_result": (
                "Roughly neutral performance delta was the pre-study "
                "expectation -- FXE is one of fifteen inverse-vol-weighted "
                "assets, so removing it mechanically redistributes its "
                "small weight rather than removing a large return driver. "
                "The primary justification under test is the cash-drag / "
                "redeployment argument, not a Sharpe improvement."
            ),
        },
        "control_reproduction": {
            "method": (
                "Recompute base/2x production portfolio from unmodified "
                "build_streams() and diff against reports/"
                "production_portfolio.json's recorded full-sample figures."
            ),
            "checks": reproduction,
            "tsmom_reimplementation_check": {
                "method": (
                    "This study cannot call production_portfolio.tsmom_stream() "
                    "directly (it reads cfg.sleeves_paper['tsmom_universe'] "
                    "internally with no universe parameter), so it reimplements "
                    "the same construction parameterized by universe. Diffed "
                    "the 15-asset reimplementation against the original "
                    "function's output before trusting the 14-asset variant."
                ),
                "max_abs_daily_return_diff": round(reimpl_diff, 10),
            },
        },
        "promotion_gate_reference_only": gate,
        "performance": performance,
        "standalone_tsmom_sleeve": standalone,
        "cash_drag_analysis": drag,
        "limitations": [
            "Both control and candidate model idealized fractional fills "
            "for every included asset (repo convention, see AGENTS.md's "
            "short-borrow section) -- the backtest cannot directly observe "
            "FXE's live unfillability, only its modeled target weight.",
            "Screening-tier evidence: cached history ends 2026-07-22; "
            "results are not evaluated against the frozen 2026-08-13+ "
            "validation window.",
            "early_2020_2022 has thinner mom_ls/clone coverage than its "
            "name implies (panel starts 2020-07-27, no COVID-crash rows) "
            "per AGENTS.md; TSMOM's own asset-ETF history is unaffected "
            "since it comes from state/history_assets via Tiingo, not the "
            "cross-sectional panel.",
            "Risk-free rate is modeled as zero throughout (AGENTS.md open "
            "gap) -- does not bear on this study's control-vs-candidate "
            "comparison since the assumption is identical on both sides.",
            "8bps TSMOM turnover cost is a modeled constant, not FXE-specific "
            "-- dropping FXE changes the *set* of assets traded but this "
            "study does not model a different cost for FXE specifically.",
        ],
    }
    out = Path("reports/tsmom_fxe_drop_study.json")
    out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nDecision: {decision}")
    print(f"Gate passed (reference only): {gate['passed']} (all_no_effect={gate['all_no_effect']})")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
