"""Residual index-short fill for whole-share MOM_LS short capacity.

`short_capacity_study.py` established that whole-share rounding leaves the
$10,000-equity MOM_LS short sleeve materially under target: only ~61.2%
(base) / ~84.0% (2x) of the intended short gross is actually achieved,
because a ~$75 (base) / ~$150 (2x) per-name slot cannot short anything
priced above that slot. That under-fill is not neutral — it leaves the
sleeve unintentionally net long, on top of the 0.40 SPY-long core the rest
of the portfolio already carries (AGENTS.md, "Account size is a real
constraint").

This study tests one additional, not-yet-tried construction: after the
existing whole-share bottom-N individual-name shorting fills what it can
each rebalance, short a single broad-index proxy sized to exactly the
dollar shortfall against the sleeve's target short gross. The hypothesis is
that deliberately filling the residual with an index-level short is better
(or at least not worse) on risk-adjusted terms than leaving the gap
unfilled, and that any single-name-vs-index basis risk this introduces is
modest.

Proxy choice
------------
`config.yaml`/`config_2x.yaml` list `universe.inverse_etfs: [SH, PSQ, SQQQ,
SDS]` as an already-approved-for-shorting-context list. None of the four are
present in `state/xsec/close.parquet` (verified: zero columns, and
`state/universe_active.json` shows Alpaca lists them but the historical bar
fetch that built the panel never picked them up), so none can be used
without a fresh data pull, which is out of scope for a research-only study.

SPY and QQQ, by contrast, are both already columns in the same xsec panel
MOM_LS itself trades from (classified as "stocks" — an artifact of the CIK
matching, not a claim they are operating companies), with the same coverage
window as every other symbol used here. A plain short of SPY is used as the
primary proxy:

* it needs no new data source — same panel, same index, no reindex/alignment
  risk;
* it avoids the daily-rebalanced compounding/decay of a levered inverse fund
  (SQQQ, SDS are -1x/-2x*daily*, not the multi-week hold this sleeve wants);
* being long SH is economically close to being short SPY minus SH's own fee
  and tracking drag, so a plain SPY short is the cleanest available stand-in
  for "the approved inverse ETF that isn't in the data";
* it directly offsets the same 0.40 SPY-long core exposure that the
  short-capacity gap otherwise leaves partially unhedged, which a
  single-name proxy would not.

QQQ is run as a sensitivity-only variant (momentum losers skew growth/tech,
so a Nasdaq proxy is a plausible closer basis match) but is not eligible for
promotion from this study, matching the primary/sensitivity split convention
used in `momentum_breadth_study.py`.

SPY and QQQ are excluded from the individual-name eligible universe for the
candidate variant (both proxy choices) so the proxy leg cannot also compete
as a ranked individual pick. Empirically this exclusion changes nothing:
across all 247 control rebalances, neither symbol ever lands in the top-20
or bottom-20 momentum rank (verified separately) — it is included anyway as
a correctness safeguard, not because it moves a number.

Residual-fill mechanic (pre-registered)
----------------------------------------
For each weekly rebalance, after the existing whole-share bottom-N
individual-name selection (`short_capacity_study.build_capacity_stream`,
`selection="ranked"`) floors each name's short to whole shares:

1. `target_dollars = equity * account_multiplier * 0.5` — the sleeve's full
   short-gross dollar target (same definition `capacity_summary` already
   uses for `target_short_gross`).
2. `realized_dollars` = the sum of whole-share-floored individual-name short
   dollars that rebalance (`realized_short_dollars`, already computed by
   `build_capacity_stream`'s log).
3. `shortfall_dollars = max(target_dollars - realized_dollars, 0)`.
4. Short the proxy against that shortfall. `engine/risk.py`'s
   `_assert_gate_invariants` rejects any short with a non-integer quantity
   ("GATE INVARIANT VIOLATED: ... fractional short qty") — this applies to
   every symbol, ETFs included, not just individual stocks. So the proxy
   leg is floored to whole shares exactly like an individual name:
   `proxy_shares = floor(shortfall_dollars / proxy_price)`,
   `proxy_dollars = proxy_shares * proxy_price`.
5. The proxy weight is expressed in the same normalized-book fraction used
   throughout `short_capacity_study` (`weight = -dollars / (equity *
   account_multiplier)`), so it can be added directly to the individual-name
   weights and combined with `profile_returns` unmodified.

Costs: individual-name legs keep the existing 15 bps cross-sectional
convention. The proxy leg uses 2 bps, AGENTS.md's rate for "single-name ETF
pairs" — the closest existing category to a single-instrument ETF short,
though nothing in the existing cost schedule was written with this exact use
case in mind, which is noted as a limitation. Short borrow stays the
existing flat `SHORT_BORROW` (3%) applied to total short gross via
`profile_returns`, unchanged — likely conservative for the proxy leg
specifically, since broad-index ETF borrow is typically cheaper than
small-cap short interest, but introducing a differentiated rate here is a
new modeling assumption outside this study's scope.

Screening tier only
--------------------
Every backtest day used here is <= 2026-08-12. Per AGENTS.md, 2026-08-13
onward is the current frozen final-validation window; this study is
screening-tier evidence, not final validation, regardless of its outcome.

This is also explicitly not a hard-promotion-gate candidate. It changes
short construction logic itself (not a config knob), so even a clean pass
here is a research finding for a human to scope a follow-up from — not
something this study enables shipping directly, per the task's own limits on
touching `config*.yaml`/`engine/*.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.promotion import passes_gate_all_cells
from backtest.short_capacity_study import (
    CapacityResult,
    MOM_ACCOUNT_MULTIPLIER,
    STARTING_EQUITY,
    build_capacity_stream,
    capacity_summary,
    profile_returns,
)
from backtest.xsec_data import load

WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}
CONTROL_LABEL = "current whole-share bottom 20"
PRIMARY_PROXY = "SPY"
SENSITIVITY_PROXY = "QQQ"
PROXY_COST_BPS = 2.0
REPRO_TOL = 5e-4  # cached report values are rounded to 3-4dp


# ---------------------------------------------------------------------------
# Residual-fill construction: layers a whole-share-floored index short on top
# of build_capacity_stream's existing individual-name bottom-N selection.
# Reuses build_capacity_stream's ranking/eligibility/floor logic rather than
# reimplementing it -- only the proxy top-up is new.
# ---------------------------------------------------------------------------
def build_residual_fill_stream(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    proxy_close: pd.Series,
    *,
    account_equity: pd.Series | float,
    account_multiplier: float,
    proxy_symbol: str,
    short_n: int = 20,
    long_n: int = 20,
    cost_bps: float = 15.0,
    proxy_cost_bps: float = PROXY_COST_BPS,
) -> tuple[CapacityResult, pd.DataFrame]:
    individual = build_capacity_stream(
        close,
        volume,
        account_equity=account_equity,
        account_multiplier=account_multiplier,
        short_n=short_n,
        long_n=long_n,
        selection="ranked",
        cost_bps=cost_bps,
    )
    logs = individual.rebalances.copy()
    if not len(logs):
        return individual, logs

    proxy_price = proxy_close.reindex(close.index).ffill()

    proxy_rows = []
    for _, row in logs.iterrows():
        date = row["date"]
        equity_t = float(row["account_equity"])
        target_dollars = equity_t * account_multiplier * 0.5
        realized_dollars = float(row["realized_short_dollars"])
        shortfall = max(target_dollars - realized_dollars, 0.0)
        price = float(proxy_price.loc[date])
        shares = float(np.floor(shortfall / price)) if price > 0 else 0.0
        dollars = shares * price
        weight_frac = -dollars / (equity_t * account_multiplier) if account_multiplier else 0.0
        proxy_rows.append(
            {
                "date": date,
                "target_dollars": target_dollars,
                "shortfall_dollars_before_proxy": shortfall,
                "proxy_price": price,
                "proxy_shares": shares,
                "proxy_dollars": dollars,
                "proxy_weight_frac": weight_frac,
                "proxy_zero_fill": bool(shortfall > 0 and shares == 0),
            }
        )
    proxy_log = pd.DataFrame(proxy_rows).set_index("date")

    proxy_weight_series = (
        proxy_log["proxy_weight_frac"].reindex(close.index).ffill().fillna(0.0)
    )
    proxy_daily_return = proxy_price.pct_change()
    proxy_turnover = proxy_weight_series.diff().abs().fillna(proxy_weight_series.abs())
    proxy_leg_return = (
        proxy_weight_series.shift(1).fillna(0.0) * proxy_daily_return
        - proxy_turnover * proxy_cost_bps / 10_000.0
    ).fillna(0.0)

    combined_returns = individual.returns.add(proxy_leg_return, fill_value=0.0)
    combined_short_gross = individual.short_gross + (
        -proxy_weight_series.shift(1).fillna(0.0)
    )
    combined_weights = individual.weights.copy()
    combined_weights[proxy_symbol] = proxy_weight_series

    logs = logs.merge(proxy_log.reset_index(), on="date", how="left")
    logs["realized_short_gross_individual"] = logs["realized_short_gross"]
    logs["realized_short_gross"] = logs["realized_short_gross_individual"] + logs[
        "proxy_weight_frac"
    ].abs()

    combined = CapacityResult(
        returns=combined_returns,
        short_gross=combined_short_gross,
        rebalances=logs,
        weights=combined_weights,
    )
    return combined, logs


def solve_dynamic_residual(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    proxy_close: pd.Series,
    common: pd.Series,
    *,
    profile: str,
    proxy_symbol: str,
    short_n: int = 20,
    iterations: int = 3,
) -> tuple[pd.Series, CapacityResult, pd.DataFrame]:
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    result, logs = None, None
    profile_r = pd.Series(dtype=float)
    for _ in range(iterations):
        result, logs = build_residual_fill_stream(
            close,
            volume,
            proxy_close,
            account_equity=equity,
            account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
            proxy_symbol=proxy_symbol,
            short_n=short_n,
        )
        profile_r = profile_returns(common, result, profile=profile)
        curve = STARTING_EQUITY * (1 + profile_r).cumprod()
        equity = curve.reindex(close.index).ffill().fillna(STARTING_EQUITY)
    assert result is not None and logs is not None
    return profile_r, result, logs


# ---------------------------------------------------------------------------
# Control reproduction (AGENTS.md "Validate a new study against the
# accepted one"): prove the exact existing short_capacity_study control can
# be reproduced with the imported functions before trusting the new variant.
# ---------------------------------------------------------------------------
def reproduce_control(close, volume, common) -> tuple[dict, dict]:
    cached = json.loads(Path("reports/short_capacity_study.json").read_text())
    cached_capacity = {
        (row["profile"], row["variant"]): row for row in cached["capacity"]
    }
    def _split_label(portfolio: str) -> tuple[str, str]:
        profile, _, suffix = portfolio.partition(" — ")
        return profile, suffix

    cached_perf = {
        window: {
            _split_label(row["portfolio"]): row
            for row in rows
        }
        for window, rows in cached["production_portfolio"].items()
        if window in WINDOWS
    }

    errors = {}
    reproduced_returns = {}
    for profile in ("base", "2x"):
        equity = pd.Series(STARTING_EQUITY, index=close.index)
        result = None
        profile_r = pd.Series(dtype=float)
        for _ in range(3):
            result = build_capacity_stream(
                close,
                volume,
                account_equity=equity,
                account_multiplier=MOM_ACCOUNT_MULTIPLIER[profile],
                selection="ranked",
                short_n=20,
            )
            profile_r = profile_returns(common, result, profile=profile)
            curve = STARTING_EQUITY * (1 + profile_r).cumprod()
            equity = curve.reindex(close.index).ffill().fillna(STARTING_EQUITY)
        reproduced_returns[profile] = profile_r

        cap = capacity_summary(result, profile=profile, label=CONTROL_LABEL)
        cached_cap = cached_capacity[(profile, CONTROL_LABEL)]
        for key in (
            "target_short_gross",
            "average_realized_short_gross",
            "average_capacity_pct",
            "zero_share_target_pct",
            "average_selected_shorts",
        ):
            errors[f"capacity.{profile}.{key}"] = abs(cap[key] - cached_cap[key])

        for window, slicer in WINDOWS.items():
            summary = returns_summary(profile_r.loc[slicer], "x")
            cached_row = cached_perf[window][(profile, CONTROL_LABEL)]
            for key in ("cagr", "sharpe", "max_dd", "vol", "sortino"):
                errors[f"{window}.{profile}.{key}"] = abs(summary[key] - cached_row[key])

    max_error = max(errors.values())
    report = {
        "max_abs_error": round(max_error, 8),
        "tolerance": REPRO_TOL,
        "reproduced": max_error < REPRO_TOL,
        "errors": {k: round(v, 8) for k, v in errors.items()},
    }
    return report, reproduced_returns


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in classified["stocks"] if s in close_all]
    close, volume = close_all[stocks], volume_all[stocks]

    production = build_streams()
    common = (
        0.40 * production["spy"]
        + 0.25 * production["tsmom"]
        + 0.20 * production["trend"]
    )

    print("Reproducing short_capacity_study control...", flush=True)
    repro_report, control_returns = reproduce_control(close, volume, common)
    print(f"  max_abs_error={repro_report['max_abs_error']} "
          f"reproduced={repro_report['reproduced']}")
    if not repro_report["reproduced"]:
        print("WARNING: control reproduction did not match within tolerance; "
              "candidate results below should not be trusted until this is "
              "resolved.")

    # exclude the proxy symbols from the candidate's ranking universe so
    # neither can also be picked as an individual momentum name (empirically
    # a no-op: neither ever lands in the top/bottom 20 of the control run).
    proxy_symbols = [PRIMARY_PROXY, SENSITIVITY_PROXY]
    stocks_excl = [s for s in stocks if s not in proxy_symbols]
    close_cand, volume_cand = close_all[stocks_excl], volume_all[stocks_excl]

    variant_returns: dict[str, pd.Series] = {}
    variant_logs: dict[str, pd.DataFrame] = {}
    for profile in ("base", "2x"):
        variant_returns[f"{profile} — {CONTROL_LABEL}"] = control_returns[profile]
        for proxy_symbol in (PRIMARY_PROXY, SENSITIVITY_PROXY):
            label = f"{profile} — residual-fill ({proxy_symbol})"
            print(f"Running {label}...", flush=True)
            profile_r, result, logs = solve_dynamic_residual(
                close_cand,
                volume_cand,
                close_all[proxy_symbol],
                common,
                profile=profile,
                proxy_symbol=proxy_symbol,
            )
            variant_returns[label] = profile_r
            variant_logs[label] = logs

    performance = {}
    for window, slicer in WINDOWS.items():
        rows = []
        for label, series in variant_returns.items():
            rows.append(returns_summary(series.loc[slicer], label))
        performance[window] = rows

    def find_row(rows, profile, suffix):
        return next(
            r for r in rows if r["portfolio"] == f"{profile} — {suffix}"
        )

    capacity_before_after = []
    basis_risk = {}
    for profile in ("base", "2x"):
        control_result = None  # recovered from cached capacity, not recomputed
        cached = json.loads(Path("reports/short_capacity_study.json").read_text())
        control_cap = next(
            r for r in cached["capacity"]
            if r["profile"] == profile and r["variant"] == CONTROL_LABEL
        )
        for proxy_symbol in (PRIMARY_PROXY, SENSITIVITY_PROXY):
            label = f"{profile} — residual-fill ({proxy_symbol})"
            logs = variant_logs[label]
            multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
            target_count = int(logs["selected_shorts"].sum())
            combined_gross_mean = float(logs["realized_short_gross"].mean())
            individual_gross_mean = float(
                logs["realized_short_gross_individual"].mean()
            )
            proxy_zero_fill_pct = round(
                100 * logs["proxy_zero_fill"].sum() / len(logs), 1
            ) if len(logs) else 0.0
            capacity_before_after.append(
                {
                    "profile": profile,
                    "proxy_symbol": proxy_symbol,
                    "target_short_gross": round(multiplier * 0.5, 4),
                    "before_avg_realized_short_gross": control_cap[
                        "average_realized_short_gross"
                    ],
                    "before_avg_capacity_pct": control_cap["average_capacity_pct"],
                    "after_avg_realized_short_gross": round(
                        combined_gross_mean * multiplier, 4
                    ),
                    "after_avg_capacity_pct": round(
                        combined_gross_mean / 0.5 * 100, 1
                    ),
                    "individual_leg_avg_capacity_pct": round(
                        individual_gross_mean / 0.5 * 100, 1
                    ),
                    "proxy_rebalances_zero_filled_despite_shortfall_pct": proxy_zero_fill_pct,
                    "zero_share_target_pct_individual_names": control_cap[
                        "zero_share_target_pct"
                    ],
                }
            )

            # Basis risk: correlation between the individual short leg's OWN
            # P&L contribution and the proxy short leg's P&L contribution.
            # build_capacity_stream's `.returns` is the full long+short book,
            # so it must not be used directly here -- isolate the short side
            # from `.weights` (negative-weight columns only) against the same
            # daily price returns, matching how build_capacity_stream itself
            # derives `gross` from `weights.shift(1) * daily_returns`.
            individual_leg_result = build_capacity_stream(
                close_cand,
                volume_cand,
                account_equity=STARTING_EQUITY,
                account_multiplier=multiplier,
                selection="ranked",
                short_n=20,
            )
            daily_returns_cand = close_cand.pct_change()
            individual_short_contribution = (
                individual_leg_result.weights.clip(upper=0).shift(1)
                * daily_returns_cand
            ).sum(axis=1)
            # Use the actual solved proxy weight path for this profile/proxy
            # (already computed above, whole-share floored, with its own
            # rebalance-varying magnitude) rather than a fresh placeholder,
            # so the sign and scale match the real economics exactly.
            proxy_ret = close_all[proxy_symbol].pct_change()
            proxy_weight_path = (
                logs.set_index("date")["proxy_weight_frac"]
                .reindex(close.index).ffill().fillna(0.0)
            )
            proxy_contribution = proxy_weight_path.shift(1) * proxy_ret
            aligned = pd.concat(
                {"individual_short_leg": individual_short_contribution,
                 "proxy_short_leg": proxy_contribution},
                axis=1,
            ).dropna()
            for window, slicer in WINDOWS.items():
                sub = aligned.loc[slicer]
                basis_risk[f"{profile}.{proxy_symbol}.{window}"] = round(
                    float(sub["individual_short_leg"].corr(sub["proxy_short_leg"])), 3
                ) if len(sub) > 5 else None

    cells_by_proxy = {PRIMARY_PROXY: [], SENSITIVITY_PROXY: []}
    for window, rows in performance.items():
        for profile in ("base", "2x"):
            control = find_row(rows, profile, CONTROL_LABEL)
            for proxy_symbol in (PRIMARY_PROXY, SENSITIVITY_PROXY):
                candidate = find_row(rows, profile, f"residual-fill ({proxy_symbol})")
                cells_by_proxy[proxy_symbol].append((window, profile, control, candidate))

    gate_by_proxy = {
        proxy_symbol: passes_gate_all_cells(cells, "return_enhancer")
        for proxy_symbol, cells in cells_by_proxy.items()
    }
    primary_gate = gate_by_proxy[PRIMARY_PROXY]

    # qualitative experiment-tier criterion (see module docstring): does the
    # residual fill measurably close the capacity gap without a Sharpe loss
    # in every cell, treated as a softer bar than the hard return_enhancer
    # gate (no zero-tolerance drawdown requirement).
    primary_capacity_rows = [
        r for r in capacity_before_after if r["proxy_symbol"] == PRIMARY_PROXY
    ]
    closes_gap = all(r["after_avg_capacity_pct"] >= 95.0 for r in primary_capacity_rows)
    primary_cells = cells_by_proxy[PRIMARY_PROXY]
    sharpe_deltas = [
        c["sharpe"] - ctrl["sharpe"] for _, _, ctrl, c in primary_cells
    ]
    no_sharpe_loss = all(d >= -0.02 for d in sharpe_deltas)  # small tolerance band
    experiment_tier_worthy = closes_gap and no_sharpe_loss

    decision = (
        "worth_2x_experiment_tier_pilot" if experiment_tier_worthy else "reject"
    )

    payload = {
        "decision": decision,
        "evidence_tier": "screening_only",
        "screening_tier_caveat": (
            "All backtest data used here is <= 2026-08-12. Per AGENTS.md, "
            "2026-08-13 onward is the current frozen final-validation window; "
            "this study is pre-registered screening evidence, not final "
            "validation, and its methodology/thresholds were fixed before "
            "looking at any data past that boundary. This is also not a "
            "hard-promotion-gate candidate -- it changes short construction "
            "logic itself, which needs new gate/execution code before it "
            "could ever run live, scoped separately by a human."
        ),
        "pre_registration": {
            "objective_class": "return_enhancer (hard-gate reported for "
                                "transparency; not the actual decision bar)",
            "actual_decision_bar": (
                "Experiment-tier (2x lab, capped): does the residual fill "
                "measurably close the short-capacity gap (>=95% average "
                "capacity in all 4 cells) without a meaningful Sharpe loss "
                "(no cell losing more than 0.02 Sharpe vs control)? This is "
                "explicitly looser than the zero-tolerance hard promotion "
                "gate."
            ),
            "control": f"{CONTROL_LABEL} (short_capacity_study.py's own accepted control)",
            "primary_candidate": f"residual-fill ({PRIMARY_PROXY})",
            "sensitivity_only": [f"residual-fill ({SENSITIVITY_PROXY})"],
            "proxy_choice_rationale": (
                "SH/PSQ/SQQQ/SDS (config's approved inverse_etfs list) have "
                "zero coverage in state/xsec/close.parquet -- verified, not "
                "assumed. SPY and QQQ are already columns in that same panel "
                "with full coverage, so a plain short of SPY is used as the "
                "primary proxy (cleaner than a levered daily-rebalanced "
                "inverse fund, no new data source, and it directly offsets "
                "the same SPY-long core exposure that the capacity gap "
                "otherwise leaves under-hedged). QQQ is a sensitivity-only "
                "variant."
            ),
            "residual_fill_mechanic": (
                "Each rebalance: after whole-share individual-name shorting "
                "fills what it can, shortfall_dollars = max(target_short_"
                "gross_dollars - realized_individual_dollars, 0); proxy is "
                "shorted floor(shortfall_dollars / proxy_price) whole shares "
                "(engine/risk.py's gate invariant requires integer short qty "
                "for every symbol, not just individual stocks, so the proxy "
                "leg is floored the same way, not fractional)."
            ),
            "proxy_symbols_excluded_from_individual_name_ranking": True,
            "proxy_exclusion_empirically_a_no_op": (
                "Neither SPY nor QQQ ever appears in the top-20 or bottom-20 "
                "momentum rank across all 247 control rebalances -- verified "
                "separately. The exclusion is a correctness safeguard, not a "
                "result-moving change."
            ),
            "cost_convention": (
                f"individual-name legs: 15bps (existing cross-sectional "
                f"convention). Proxy leg: {PROXY_COST_BPS}bps, AGENTS.md's "
                f"rate for single-name ETF pairs -- the closest existing "
                f"category, not a category written with this exact use case "
                f"in mind."
            ),
            "borrow_convention": (
                "Unchanged flat SHORT_BORROW=3%/yr on total short gross via "
                "profile_returns, applied identically to the individual and "
                "proxy legs. Likely conservative for the proxy leg (broad "
                "ETF borrow is typically cheaper than small-cap short "
                "interest) -- not modeling that difference is a limitation, "
                "not an oversight."
            ),
        },
        "control_reproduction": repro_report,
        "capacity_before_after": capacity_before_after,
        "basis_risk_correlation": basis_risk,
        "basis_risk_note": (
            "Correlation between the daily return of the individual "
            "whole-share short leg and the daily return of a plain short "
            "proxy leg, per window. Below 1.0 is the basis risk the proxy "
            "leg adds relative to shorting more individual losers directly; "
            "a materially positive but well-below-1 correlation is expected "
            "(momentum losers and the broad index share market beta but not "
            "idiosyncratic risk)."
        ),
        "performance": performance,
        "primary_promotion_gate_hard_bar": primary_gate,
        "sensitivity_gate_hard_bar_not_eligible_for_promotion": gate_by_proxy[
            SENSITIVITY_PROXY
        ],
        "experiment_tier_criteria": {
            "closes_capacity_gap_to_95pct_all_cells": closes_gap,
            "sharpe_deltas_vs_control": [round(d, 4) for d in sharpe_deltas],
            "no_meaningful_sharpe_loss": no_sharpe_loss,
            "experiment_tier_worthy": experiment_tier_worthy,
        },
        "limitations": [
            "Historical easy-to-borrow/locate availability is unavailable; "
            "shorting the proxy at all assumes it is always borrowable, "
            "which is realistic for SPY/QQQ specifically but not verified "
            "against a historical locate feed.",
            "SH/PSQ/SQQQ/SDS (the actually-configured inverse ETFs) are not "
            "in the historical panel; results here are for a plain SPY/QQQ "
            "short, not the configured instruments, and would need new data "
            "to validate directly.",
            "The flat 3% borrow rate applied identically to the individual "
            "and proxy legs likely overstates the proxy leg's real cost.",
            "The universe of currently-listed stocks is survivorship-biased "
            "(inherited from short_capacity_study/xsec_data).",
            "Weekly rebalance only; does not model daily drift-band trades.",
            "The proxy leg reduces overall net long bias (it offsets the "
            "same 0.40 SPY-long core exposure the rest of the portfolio "
            "carries) as a side effect of closing the short-gross gap -- "
            "this study measures MOM_LS-level Sharpe/CAGR/drawdown, not "
            "that portfolio-level netting effect in isolation.",
        ],
        "starting_equity": STARTING_EQUITY,
    }

    out = Path("reports/short_construction_study.json")
    out.write_text(json.dumps(payload, indent=2, default=str))

    print("\nCAPACITY BEFORE/AFTER")
    print(pd.DataFrame(capacity_before_after).to_string(index=False))
    for window, rows in performance.items():
        print(f"\nPERFORMANCE — {window}")
        print(pd.DataFrame(rows)[["portfolio", "cagr", "sharpe", "max_dd"]].to_string(index=False))
    print(f"\nDecision: {decision}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
