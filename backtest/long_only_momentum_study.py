"""Pre-registered `mom_long` candidate sleeve: long-only 12-1 momentum with a
rank buffer, monthly cadence averaged over every rebalance-date offset, and
volatility scaling that can only de-lever.

Registration: `reports/absolute_return_campaign_registration.json`
(`candidate_sleeves.mom_long`). Grid, cadence, offsets, buffer, vol target
and the pass rule all come from there; nothing here adds a grid point.

Why this sleeve exists
----------------------
The 2026-09-14 leg decomposition of the deployed MOM_LS sleeve
(docs/research.md, "Live results and the 2026-09-14 stand-down") showed the
long book carried the whole 2023+ window while the short book's only good
year was 2022 — and that the long book is a 44%-vol, 0.65-beta stream whose
single-path monthly numbers looked like rebalance-date luck. This sleeve
keeps the long book, drops the shorts, and addresses both caveats by
construction: a rank buffer (hold an incumbent until it falls outside the
top 2N) to cut turnover, offset averaging across all 21 monthly rebalance
dates so no single calendar path is the result, and a 63-day realized-vol
scale clipped to [0.25, 1.0] so the sleeve sizes itself down in a crash
and never levers.

Signal
------
Momentum on day t is `close[t-21] / close[t-252] - 1` on the cross-sectional
panel (`backtest.xsec_data.load`), eligible when `close[t-21] >= $5` and the
20-day mean dollar volume at t-21 is `>= $5M` — the identical rule to
`backtest.xsec_momentum.build_portfolio` and `deployable_sim.mom_ls_target_weights`.
Membership changes on a rebalance day and is held (forward-filled) until
the next one; positions are equal weight across holdings. Everything on day
t uses closes `<= t-1`-style shifted inputs (the momentum and eligibility
frames are built from shifted closes), so there is no look-ahead.

Vol scaling
-----------
The unscaled equal-weight book's daily return r_t = sum_i w_{i,t-1} * ret_{i,t}
gives a trailing 63-day realized vol (annualized). The scale applied on day
t is `clip(vol_target / vol_{t-1}, 0.25, 1.0)` — yesterday's vol, never
today's, and never above 1.0. Before 63 observations exist the scale is
1.0 (no information, no de-risking).

Standalone vs in a mix
----------------------
`target_weights` returns the sleeve at weight 1.0 (rows sum to the scale,
<= 1.0); the mix study scales it by the sleeve's mix weight.

Benchmark
---------
Raw SPY close-to-close total return (Tiingo adjusted close, zero cost) on
the panel's own dates — the registration benchmark. A like-for-like
simulated-SPY row (through `simulate_targets` with identical settings) is
reported as a diagnostic, not judged.

Decision rule (declared before any result was seen)
---------------------------------------------------
`sleeve_screened_for_mixes` if the top-20, vol-scaled, offset-mean form
passes `passes_gate(objective_class="benchmark_beater")` in every one of
full + heldout_2023_plus + early_2020_2022 at 15 bps against the
registration benchmark; the top-10 form is a registered sensitivity and
does not rescue the sleeve on its own. MTUM is judged separately (raw
vs raw, both zero-cost buy-and-hold) as the ETF vehicle and its verdict is
recorded as `mtum_decision`. The mix study, not this file, makes the
promotion call.

Panel caveats (AGENTS.md, "The cross-sectional panel has no COVID-crash
coverage"): the panel starts 2020-07-27 and is survivors-only, so the
sleeve's first live day is 2021-08-26 (252 + 21 bars of warm-up), the
"early_2020_2022" cell is really 2021-08-26..2022-12-30, and every
long-only positive is an upper bound. No stress window is evaluable here.

Reproduce
---------
    .venv/bin/python -m backtest.long_only_momentum_study   # ~3-5 minutes
    .venv/bin/python -m pytest tests/test_long_only_momentum_study.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import windows
from backtest.deployable_sim import simulate_targets
from backtest.production_portfolio import norm_index, returns_summary
from backtest.riskfree import load_rf_daily
from backtest.trend_leveraged_index_study import (
    _align,
    _json_default,
    gate_all_cells_per_band,
    judge_cell,
    load_close,
)
from backtest.xsec_data import load as load_xsec

REGISTRATION = "reports/absolute_return_campaign_registration.json"
REPORT = Path("reports/long_only_momentum_study.json")
OBJECTIVE_CLASS = "benchmark_beater"
BENCHMARK = "SPY"
MTUM = "MTUM"
MTUM_INCEPTION = "2013-04-18"

EQUITY = 10_000.0
POSITION_CAP_PCT = 0.15
COST_BPS = 15.0
COST_BPS_STRESS = 20.0
LOOKBACK = 252
SKIP = 21
REBALANCE = 21
OFFSETS = tuple(range(REBALANCE))
RANK_BUFFER_MULT = 2
VOL_TARGET = 0.20
VOL_LOOKBACK = 63
VOL_SCALE_BOUNDS = (0.25, 1.0)
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 5e6
REQUIRED_CELLS = ("full", "heldout_2023_plus", "early_2020_2022")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


def load_panel(end: str = windows.HELDOUT[1]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The cross-sectional close/volume panel, tz-naive, truncated to
    `<= end`. Never reads past the registration's frozen boundary."""
    if pd.Timestamp(end) >= pd.Timestamp(windows.FROZEN_START):
        raise ValueError(f"end {end} reaches the frozen window ({windows.FROZEN_START}+)")
    close, volume = load_xsec()
    close, volume = norm_index(close), norm_index(volume)
    keep = close.index <= pd.Timestamp(end)
    return close.loc[keep], volume.loc[keep]


# --------------------------------------------------------------------------
# Signal and membership
# --------------------------------------------------------------------------


def momentum_and_eligibility(
    close: pd.DataFrame, volume: pd.DataFrame, *,
    lookback: int = LOOKBACK, skip: int = SKIP,
    min_price: float = MIN_PRICE, min_dollar_volume: float = MIN_DOLLAR_VOLUME,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Identical to `deployable_sim.mom_ls_target_weights`'s rule."""
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    momentum = close.shift(skip) / close.shift(lookback) - 1.0
    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )
    return momentum, eligible


def membership_path(
    momentum: pd.DataFrame, eligible: pd.DataFrame, *,
    top_n: int, rebalance: int = REBALANCE, offset: int = 0,
    rank_buffer_mult: int | None = RANK_BUFFER_MULT,
    lookback: int = LOOKBACK, skip: int = SKIP,
) -> list[tuple[pd.Timestamp, list[str]]]:
    """Holdings after each rebalance day for one calendar offset.

    Rank buffer: an incumbent stays while its momentum rank is
    `<= rank_buffer_mult * top_n`; freed slots are filled by the best-ranked
    non-holdings. `rank_buffer_mult=None` (or 1) is the plain top-N. Days
    with fewer than `top_n` eligible names keep the previous holdings.
    """
    keep_rank = (rank_buffer_mult or 1) * top_n
    rebalance_days = momentum.index[lookback + skip + offset :: rebalance]
    held: list[str] = []
    out: list[tuple[pd.Timestamp, list[str]]] = []
    for date in rebalance_days:
        ranked = momentum.loc[date].where(eligible.loc[date]).dropna()
        if len(ranked) < top_n:
            continue
        ranked = ranked.sort_values(ascending=False)
        ranks = pd.Series(np.arange(1, len(ranked) + 1), index=ranked.index)
        keep = [s for s in held if s in ranks.index and ranks[s] <= keep_rank]
        for symbol in ranked.index:
            if len(keep) >= top_n:
                break
            if symbol not in keep:
                keep.append(symbol)
        held = keep[:top_n]
        out.append((date, list(held)))
    return out


def weights_from_membership(
    membership: list[tuple[pd.Timestamp, list[str]]], index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Equal-weight holdings, forward-held between rebalance days."""
    symbols = sorted({s for _, held in membership for s in held})
    weights = pd.DataFrame(0.0, index=index, columns=symbols)
    for k, (date, held) in enumerate(membership):
        start = index.get_loc(date)
        end = index.get_loc(membership[k + 1][0]) if k + 1 < len(membership) else len(index)
        cols = [weights.columns.get_loc(s) for s in held]
        weights.iloc[start:end, cols] = 1.0 / len(held)
    return weights


def vol_scale(
    unscaled: pd.DataFrame, close: pd.DataFrame, *,
    vol_target: float = VOL_TARGET, lookback: int = VOL_LOOKBACK,
    bounds: tuple[float, float] = VOL_SCALE_BOUNDS,
) -> pd.Series:
    """Daily scale in [bounds] from yesterday's trailing realized vol of the
    unscaled book; 1.0 until `lookback` observations exist."""
    rets = close[unscaled.columns].pct_change(fill_method=None)
    book = (unscaled.shift(1) * rets).sum(axis=1)
    realized = book.rolling(lookback, min_periods=lookback).std() * np.sqrt(252)
    scale = (vol_target / realized.shift(1)).clip(bounds[0], bounds[1])
    return scale.fillna(1.0)


def target_weights(
    close: pd.DataFrame, volume: pd.DataFrame, *,
    top_n: int = 20, rebalance: int = REBALANCE,
    rebalance_offsets: tuple[int, ...] = OFFSETS,
    rank_buffer_mult: int | None = RANK_BUFFER_MULT,
    vol_target: float | None = VOL_TARGET, vol_lookback: int = VOL_LOOKBACK,
    vol_scale_bounds: tuple[float, float] = VOL_SCALE_BOUNDS,
    lookback: int = LOOKBACK, skip: int = SKIP,
    min_price: float = MIN_PRICE, min_dollar_volume: float = MIN_DOLLAR_VOLUME,
    return_paths: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, dict[int, pd.DataFrame]]:
    """Daily long-only target weights (sleeve weight 1.0, rows sum to the
    vol scale <= 1.0), averaged across `rebalance_offsets`.

    `vol_target=None` skips scaling (the unscaled diagnostic form). With
    `return_paths=True` the per-offset UNSCALED frames are returned too.
    """
    momentum, eligible = momentum_and_eligibility(
        close, volume, lookback=lookback, skip=skip,
        min_price=min_price, min_dollar_volume=min_dollar_volume,
    )
    paths: dict[int, pd.DataFrame] = {}
    for offset in rebalance_offsets:
        membership = membership_path(
            momentum, eligible, top_n=top_n, rebalance=rebalance, offset=offset,
            rank_buffer_mult=rank_buffer_mult, lookback=lookback, skip=skip,
        )
        paths[offset] = weights_from_membership(membership, close.index)
    symbols = sorted({s for frame in paths.values() for s in frame.columns})
    mean = sum(frame.reindex(columns=symbols, fill_value=0.0) for frame in paths.values())
    mean = mean / len(paths)
    if vol_target is not None:
        scale = vol_scale(mean, close, vol_target=vol_target, lookback=vol_lookback,
                          bounds=vol_scale_bounds)
        mean = mean.mul(scale, axis=0)
    return (mean, paths) if return_paths else mean


def mtum_weights(close_mtum: pd.DataFrame) -> pd.DataFrame:
    """1.0 in MTUM from its first bar; cash before."""
    weights = pd.DataFrame(0.0, index=close_mtum.index, columns=[MTUM])
    weights.loc[close_mtum[MTUM].notna(), MTUM] = 1.0
    return weights


# --------------------------------------------------------------------------
# Simulation and judging
# --------------------------------------------------------------------------


def simulate_sleeve(weights: pd.DataFrame, close: pd.DataFrame, *, cost_bps: float = COST_BPS):
    frame = close[weights.columns]
    return simulate_targets(
        weights, frame, equity=EQUITY, gross_leverage=1.0,
        position_cap_pct=POSITION_CAP_PCT, elevated_cap=None,
        leveraged_cap_pct=1.0, cost_bps_by_symbol=cost_bps,
    )


def spy_like_for_like(spy_close: pd.Series, index: pd.DatetimeIndex, *, cost_bps: float = COST_BPS) -> pd.Series:
    frame = pd.DataFrame({BENCHMARK: spy_close.reindex(index)})
    weights = pd.DataFrame({BENCHMARK: 1.0}, index=index)
    returns, _ = simulate_targets(
        weights, frame, equity=EQUITY, gross_leverage=1.0,
        elevated_cap=({BENCHMARK}, 0.80), leveraged_cap_pct=1.0,
        cost_bps_by_symbol=cost_bps,
    )
    return returns


def cell_windows(first_live: pd.Timestamp, end: pd.Timestamp) -> dict:
    cells = {"full": (first_live.date().isoformat(), end.date().isoformat())}
    early = windows.SCREEN_WINDOWS["early_2020_2022"]
    cells["early_2020_2022"] = (max(first_live, pd.Timestamp(early[0])).date().isoformat(), early[1])
    cells["heldout_2023_plus"] = windows.SCREEN_WINDOWS["heldout_2023_plus"]
    return cells


def beta_to(bench: pd.Series, cand: pd.Series) -> float:
    b, c = bench.align(cand, join="inner")
    b, c = b.dropna(), c.reindex(b.index).dropna()
    b = b.reindex(c.index)
    if len(c) < 2 or float(b.var()) == 0:
        return float("nan")
    return float(np.cov(c, b)[0, 1] / b.var())


def calendar_years(cand: pd.Series, bench: pd.Series) -> dict:
    b = bench.reindex(cand.index).fillna(0.0)
    years = cand.index.year
    out = {}
    for y in sorted(set(years)):
        out[str(y)] = {
            "sleeve": round(float((1 + cand[years == y]).prod() - 1), 4),
            "spy": round(float((1 + b[years == y]).prod() - 1), 4),
        }
    return out


def judge_form(returns: pd.Series, spy_raw: pd.Series, rf: pd.Series, cells: dict, label: str) -> dict:
    judged = {
        name: judge_cell(spy_raw, returns, bounds, rf=rf, bench_label=BENCHMARK, cand_label=label)
        for name, bounds in cells.items()
    }
    return {"cells": judged, "gate_all_cells": gate_all_cells_per_band(judged)}


def form_diagnostics(weights: pd.DataFrame, returns: pd.Series, diag: pd.DataFrame,
                     spy_raw: pd.Series, first_live: pd.Timestamp) -> dict:
    live = weights.loc[weights.index >= first_live]
    held = (live > 0).sum(axis=1)
    years = live.index.year
    turnover = diag.loc[diag.index >= first_live, "turnover"]
    r = returns.loc[returns.index >= first_live]
    return {
        "avg_holdings": round(float(held.mean()), 2),
        "avg_gross_weight": round(float(live.sum(axis=1).mean()), 4),
        "annual_turnover_one_way": round(float(turnover.groupby(turnover.index.year).sum().mean()), 2),
        "realized_vol_annualized": round(float(r.std() * np.sqrt(252)), 4),
        "beta_to_spy": round(beta_to(spy_raw, r), 3),
        "calendar_years": calendar_years(r, spy_raw),
    }


def run_form(close, volume, spy_raw, rf, *, top_n, vol_target, rank_buffer_mult,
             cost_bps, label, offsets=OFFSETS) -> dict:
    weights, paths = target_weights(
        close, volume, top_n=top_n, rebalance_offsets=offsets,
        rank_buffer_mult=rank_buffer_mult, vol_target=vol_target, return_paths=True,
    )
    first_live = weights.index[(weights.sum(axis=1) > 0).to_numpy().argmax()]
    returns, diag = simulate_sleeve(weights, close, cost_bps=cost_bps)
    cells = cell_windows(first_live, close.index[-1])
    out = {
        "label": label,
        "params": {"top_n": top_n, "vol_target": vol_target, "rank_buffer_mult": rank_buffer_mult,
                   "cost_bps": cost_bps, "offsets": list(offsets), "first_live_day": first_live.date().isoformat()},
        **judge_form(returns, spy_raw, rf, cells, label),
        "diagnostics": form_diagnostics(weights, returns, diag, spy_raw, first_live),
    }
    return out, weights, paths, returns


def offset_dispersion(paths: dict[int, pd.DataFrame], close, spy_raw, rf, *, vol_target, cost_bps,
                      cells: dict) -> dict:
    """Per-offset (single calendar path) results for the registered
    dispersion deliverable: min / mean / max of CAGR, excess Sharpe and max
    drawdown across offsets, per required cell."""
    per_cell: dict[str, dict[str, list[float]]] = {c: {"cagr": [], "excess_sharpe": [], "max_dd": []} for c in cells}
    for offset, unscaled in paths.items():
        w = unscaled
        if vol_target is not None:
            w = unscaled.mul(vol_scale(unscaled, close, vol_target=vol_target), axis=0)
        returns, _ = simulate_sleeve(w, close, cost_bps=cost_bps)
        for name, bounds in cells.items():
            _, c = _align(spy_raw, returns, bounds)
            s = returns_summary(c, f"offset{offset}", rf=rf)
            for k in per_cell[name]:
                per_cell[name][k].append(float(s[k]))
    return {
        name: {k: {"min": round(min(v), 4), "mean": round(float(np.mean(v)), 4), "max": round(max(v), 4)}
               for k, v in stats.items()}
        for name, stats in per_cell.items()
    }


def main() -> None:
    close, volume = load_panel()
    hist = load_close([BENCHMARK, MTUM, "BIL"])
    spy_raw = hist[BENCHMARK].pct_change(fill_method=None)
    rf = load_rf_daily()

    report: dict = {
        "study": "long_only_momentum",
        "pre_registration": REGISTRATION,
        "objective_class": OBJECTIVE_CLASS,
        "benchmark": "raw SPY close-to-close total return (state/history/SPY.parquet), zero cost",
        "panel": {"start": close.index[0].date().isoformat(), "end": close.index[-1].date().isoformat(),
                  "symbols": int(close.shape[1])},
        "forms": {},
    }

    judged_forms = {}
    # Judged forms: vol-scaled, rank-buffered, offset-mean.
    for top_n in (20, 10):
        label = f"top{top_n}_volscaled_buffer_offsetmean"
        res, weights, paths, _ = run_form(close, volume, spy_raw, rf, top_n=top_n,
                                          vol_target=VOL_TARGET, rank_buffer_mult=RANK_BUFFER_MULT,
                                          cost_bps=COST_BPS, label=label)
        cells = cell_windows(pd.Timestamp(res["params"]["first_live_day"]), close.index[-1])
        res["offset_dispersion"] = offset_dispersion(paths, close, spy_raw, rf, vol_target=VOL_TARGET,
                                                     cost_bps=COST_BPS, cells=cells)
        stress, *_ = run_form(close, volume, spy_raw, rf, top_n=top_n, vol_target=VOL_TARGET,
                              rank_buffer_mult=RANK_BUFFER_MULT, cost_bps=COST_BPS_STRESS,
                              label=label + "_20bps")
        res["stress_20bps"] = {"gate_all_cells": stress["gate_all_cells"],
                               "cells": {c: {"candidate": v["candidate"]} for c, v in stress["cells"].items()}}
        report["forms"][label] = res
        judged_forms[top_n] = res

    # Diagnostic forms (top-20 only): unscaled, and no rank buffer.
    for label, kwargs in (
        ("top20_unscaled_buffer_offsetmean", dict(vol_target=None, rank_buffer_mult=RANK_BUFFER_MULT)),
        ("top20_volscaled_nobuffer_offsetmean", dict(vol_target=VOL_TARGET, rank_buffer_mult=None)),
    ):
        res, *_ = run_form(close, volume, spy_raw, rf, top_n=20, cost_bps=COST_BPS, label=label, **kwargs)
        report["forms"][label] = res

    # Like-for-like simulated SPY over the sleeve's live period (diagnostic).
    first_live = pd.Timestamp(judged_forms[20]["params"]["first_live_day"])
    sim_spy = spy_like_for_like(hist[BENCHMARK], close.index[close.index >= first_live])
    cells = cell_windows(first_live, close.index[-1])
    report["simulated_spy_diagnostic"] = {
        name: returns_summary(_align(spy_raw, sim_spy, bounds)[1], "SPY_simulated", rf=rf)
        for name, bounds in cells.items()
    }

    # MTUM: the ETF vehicle, raw vs raw (both zero-cost buy-and-hold), judged separately.
    mtum_raw = hist[MTUM].pct_change(fill_method=None).dropna()
    mtum_cells = {
        "full": (MTUM_INCEPTION, windows.HELDOUT[1]),
        "early_2020_2022": windows.SCREEN_WINDOWS["early_2020_2022"],
        "heldout_2023_plus": windows.SCREEN_WINDOWS["heldout_2023_plus"],
    }
    for label, bounds in windows.STRESS_WINDOWS.items():
        if pd.Timestamp(bounds[0]) >= pd.Timestamp(MTUM_INCEPTION):
            mtum_cells[f"stress:{label}"] = bounds
    mtum_judged = {name: judge_cell(spy_raw, mtum_raw, bounds, rf=rf, bench_label=BENCHMARK, cand_label=MTUM)
                   for name, bounds in mtum_cells.items()}
    report["mtum"] = {
        "construction": "MTUM buy-and-hold total return, raw vs raw SPY, zero cost both sides",
        "cells": mtum_judged,
        "gate_all_cells": gate_all_cells_per_band(mtum_judged),
        "calendar_years": calendar_years(mtum_raw, spy_raw),
        "beta_to_spy": round(beta_to(spy_raw, mtum_raw), 3),
    }

    primary = judged_forms[20]["gate_all_cells"]["passed"]
    secondary = judged_forms[10]["gate_all_cells"]["passed"]
    report["decision"] = "sleeve_screened_for_mixes" if primary else "sleeve_rejected"
    report["decision_rule"] = (
        "screened iff the top-20 vol-scaled rank-buffered offset-mean form passes benchmark_beater in "
        "full + heldout_2023_plus + early_2020_2022 at 15 bps against raw SPY; top-10 is a registered "
        "sensitivity and cannot rescue the sleeve on its own"
    )
    report["decision_detail"] = {
        "top20_passed": primary, "top10_passed": secondary,
        "top20_cells": {c: r["gate"].get("verdict", r["gate"].get("passed")) for c, r in judged_forms[20]["cells"].items()},
    }
    report["mtum_decision"] = "etf_vehicle_passes" if report["mtum"]["gate_all_cells"]["passed"] else "etf_vehicle_fails"
    report["limitations"] = [
        "Panel starts 2020-07-27 and contains only currently listed names (survivorship-biased); long-only positives are an upper bound (AGENTS.md).",
        "First live day is 2021-08-26 (252+21 bars of warm-up), so 'early_2020_2022' is really 2021-08-26..2022-12-30 and no stress window is evaluable on the panel; crash-era context only via reports/long_history_stress_study.json's French-Mom proxy.",
        "Offset averaging trades every day some offset rebalances; the mix study inherits that cadence. A live implementation would pick one offset (or a subset) — the per-offset dispersion block bounds what that choice can cost.",
        "Vol scaling uses the sleeve's own trailing 63-day realized vol with a one-day lag; it de-levers into a crash only after the crash has raised realized vol.",
        "Fractional long buys at $10k; position cap 15% never binds at 5-10% weights. Costs: 15 bps per unit turnover (registration), 20 bps stress reported.",
        "MTUM is judged raw-vs-raw (both zero-cost buy-and-hold); its expense ratio is embedded in its NAV.",
    ]
    report["reproduce"] = [
        ".venv/bin/python -m backtest.long_only_momentum_study",
        ".venv/bin/python -m pytest tests/test_long_only_momentum_study.py -q",
    ]
    REPORT.write_text(json.dumps(report, indent=2, default=_json_default))

    for label, res in report["forms"].items():
        g = res["gate_all_cells"]
        print(f"{label}: passed={g['passed']} " + " | ".join(
            f"{c}: {v['candidate']['cagr']:.1%}/{v['candidate'].get('excess_sharpe', float('nan')):.2f}/{v['candidate']['max_dd']:.1%} "
            f"vs {v['benchmark']['cagr']:.1%}/{v['benchmark'].get('excess_sharpe', float('nan')):.2f}/{v['benchmark']['max_dd']:.1%} "
            f"[{v['gate'].get('passed', v['gate'].get('verdict'))}]"
            for c, v in res["cells"].items()))
    print("MTUM:", report["mtum"]["gate_all_cells"]["passed"], {c: v["gate"].get("passed", v["gate"].get("verdict")) for c, v in report["mtum"]["cells"].items()})
    print("decision:", report["decision"], "| mtum:", report["mtum_decision"])


if __name__ == "__main__":
    main()
