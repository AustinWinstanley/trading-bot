"""Pre-registered `lev_trend` candidate sleeve: a 200-day trend filter on an
index, expressed through that index's daily-reset leveraged ETF.

Registration: `reports/absolute_return_campaign_registration.json`
(`candidate_sleeves.lev_trend`). Grid, signal convention, simulator settings
and the pass rule all come from there; nothing here adds a grid point.

Signal
------
On day t the sleeve is ON when `close[t-1] > SMA200[t-1]` of the INDEX
(SPY or QQQ) — the same `iloc[-2]` "yesterday's MA, no peeking" convention
as `engine.portfolio.trend_targets`. ON holds weight 1.0 in the vehicle
(SSO/UPRO for SPY, QLD/TQQQ for QQQ); OFF holds weight 1.0 in the
off-vehicle (BIL). Before BIL's 2007-05-30 inception, OFF is idle cash
(weight 0). Before the vehicle's own inception the sleeve is also cash: no
bar means no position, as everywhere else in this repo — the judged cells
start at the vehicle's first real bar, and no synthetic pre-inception series
is used for anything judged.

Standalone vs in a mix
----------------------
`target_weights` returns the sleeve at weight 1.0; the mix study scales it.
Standalone, the simulator's index-ETF elevated cap (0.80 of equity, the
registration's `elevated_cap.base`) throttles the vehicle, so the
"standalone" rows below are 80% leveraged ETF + 20% idle cash. That is
characterisation only: the registration admits the sleeve into mixes at
<= 0.20, and `leveraged_cap_pct` is a PORTFOLIO-level control, so it is set
to 1.0 here and enforced at 0.20 in the mix study. The `m6_preview` block
is the cap-feasible form (0.20 sleeve + 0.80 SPY, registered mix M6) run
with the real 0.20 leveraged cap.

Benchmarks
----------
Two are reported for every cell. The gate uses the REGISTRATION benchmark:
raw SPY close-to-close total return (Tiingo adjusted close, zero cost). The
second — SPY at weight 1.0 through `simulate_targets` with the identical
settings (so the same 80% cap, band, min-notional and 8 bps apply) — is a
like-for-like diagnostic of what the simulator itself does to a passive
holding, and its gate is reported as secondary, not judged.

Decision rule (declared before any result was seen)
---------------------------------------------------
`sleeve_screened_for_mixes` if, for at least one registration mix vehicle
(SSO or QLD — the 3x vehicles are standalone-only per the registration),
EITHER the standalone sleeve OR its M6 preview passes
`passes_gate(objective_class="benchmark_beater")` in every one of
full + heldout_2023_plus + early_2020_2022 (aggregated exactly as
`passes_gate_all_cells` does, with each cell's own paired band) at 8 bps against the registration
benchmark, AND that same form's GFC max drawdown is not worse than SPY's by
more than the paired noise band (`fails_stress` false). Otherwise
`sleeve_rejected`. The mix study, not this file, makes the promotion call.

Reproduce
---------
    .venv/bin/python -m backtest.trend_leveraged_index_study
    .venv/bin/python -m pytest tests/test_trend_leveraged_index_study.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import windows
from backtest.deployable_sim import simulate_targets
from backtest.production_portfolio import norm_index, returns_summary
from backtest.promotion import paired_drawdown_noise_pp, passes_gate
from backtest.riskfree import load_rf_daily
from engine.tiingo import load_parquet

REGISTRATION = "reports/absolute_return_campaign_registration.json"
REPORT = Path("reports/trend_leveraged_index_study.json")
OBJECTIVE_CLASS = "benchmark_beater"

INSTRUMENTS = {"SPY": ["SSO", "UPRO"], "QQQ": ["QLD", "TQQQ"]}
MIX_VEHICLES = {"SSO", "QLD"}  # the registration's in-mix vehicles
OFF_VEHICLE = "BIL"
BIL_INCEPTION = "2007-05-30"
MA_DAYS = 200
BENCHMARK = "SPY"

EQUITY = 10_000.0
ELEVATED_CAP_PCT = 0.80
LEVERAGED_CAP_PCT = 0.20
COST_BPS = 8.0
COST_BPS_STRESS = 20.0
M6 = {"equity_core": 0.80, "lev_trend": 0.20}
REQUIRED_CELLS = ("full", "heldout_2023_plus", "early_2020_2022")
MIN_BAND_OBS = 63  # paired_drawdown_noise_pp's default block_size


def load_close(symbols: list[str], end: str = windows.HELDOUT[1]) -> pd.DataFrame:
    """tz-naive daily adjusted closes from `state/history`, outer-joined on
    date and truncated to `<= end`. Never reads past the registration's
    frozen boundary (`end` defaults to `windows.HELDOUT[1]`, 2026-08-12)."""
    if pd.Timestamp(end) >= pd.Timestamp(windows.FROZEN_START):
        raise ValueError(
            f"end {end} reaches the frozen window ({windows.FROZEN_START}+)"
        )
    frames = load_parquet(list(symbols))
    missing = sorted(set(symbols) - set(frames))
    if missing:
        raise FileNotFoundError(f"state/history is missing {missing}")
    close = pd.DataFrame({
        symbol: norm_index(frames[symbol]["close"].astype(float))
        for symbol in symbols
    }).sort_index()
    return close.loc[close.index <= pd.Timestamp(end)]


def trend_on(index_close: pd.Series, ma_days: int = MA_DAYS) -> pd.Series:
    """Boolean ON/OFF per day using yesterday's close vs yesterday's SMA.
    False during the SMA warm-up (no signal -> no position)."""
    prior = index_close.shift(1)
    average = prior.rolling(ma_days, min_periods=ma_days).mean()
    return (prior > average) & average.notna()


def target_weights(
    close: pd.DataFrame,
    *,
    index: str = "SPY",
    vehicle: str = "SSO",
    off_vehicle: str = OFF_VEHICLE,
    ma_days: int = MA_DAYS,
) -> pd.DataFrame:
    """Daily sleeve weights, columns `[vehicle, off_vehicle]`, sleeve weight 1.0.

    ON (index close[t-1] > SMA[t-1]) -> 1.0 in `vehicle`; OFF -> 1.0 in
    `off_vehicle`. Either leg is 0 on days its instrument has no bar (before
    inception, or a gap), so the sleeve is cash before BIL's 2007-05-30
    start and before the vehicle's own listing. Row sums never exceed 1.0.
    """
    for column in (index, vehicle, off_vehicle):
        if column not in close.columns:
            raise KeyError(f"close frame lacks {column}")
    on = trend_on(close[index].astype(float), ma_days)
    vehicle_has_bar = close[vehicle].notna()
    off_has_bar = close[off_vehicle].notna()
    weights = pd.DataFrame(0.0, index=close.index, columns=[vehicle, off_vehicle])
    weights.loc[on & vehicle_has_bar, vehicle] = 1.0
    weights.loc[~on & off_has_bar, off_vehicle] = 1.0
    return weights


def sim_settings(vehicle: str | None, *, cost_bps: float, leveraged_cap_pct: float,
                 elevated: set[str]) -> dict:
    return dict(
        equity=EQUITY,
        gross_leverage=1.0,
        elevated_cap=(set(elevated), ELEVATED_CAP_PCT),
        leveraged_symbols={vehicle} if vehicle else set(),
        leveraged_cap_pct=leveraged_cap_pct,
        cost_bps_by_symbol=cost_bps,
    )


def simulate_sleeve(
    close: pd.DataFrame, *, index: str, vehicle: str, cost_bps: float = COST_BPS
) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    """Standalone sleeve from the vehicle's first bar. Returns
    (returns, diagnostics, weights-as-simulated)."""
    # Weights on the FULL index history first, then trim: an SMA computed on
    # the trimmed frame would fake a 200-day cash warm-up at inception
    # (the trap `production_portfolio.trend_stream` documents).
    start = close[vehicle].first_valid_index()
    weights = target_weights(close, index=index, vehicle=vehicle)
    frame = close.loc[close.index >= start]
    weights = weights.loc[frame.index]
    returns, diag = simulate_targets(
        weights, frame,
        **sim_settings(vehicle, cost_bps=cost_bps, leveraged_cap_pct=1.0,
                       elevated={vehicle, OFF_VEHICLE}),
    )
    return returns, diag, weights


def simulate_m6(
    close: pd.DataFrame, *, index: str, vehicle: str, cost_bps: float = COST_BPS
) -> tuple[pd.Series, pd.DataFrame]:
    """Mix M6: 0.80 SPY core + 0.20 lev_trend, with the real 0.20 leveraged cap."""
    start = close[vehicle].first_valid_index()
    frame = close.loc[close.index >= start]
    sleeve = target_weights(close, index=index, vehicle=vehicle).loc[frame.index] * M6["lev_trend"]
    sleeve[BENCHMARK] = M6["equity_core"]
    weights = sleeve[[BENCHMARK, vehicle, OFF_VEHICLE]]
    return simulate_targets(
        weights, frame,
        **sim_settings(vehicle, cost_bps=cost_bps, leveraged_cap_pct=LEVERAGED_CAP_PCT,
                       elevated={BENCHMARK, vehicle, OFF_VEHICLE}),
    )


def simulate_spy_like_for_like(close: pd.DataFrame, start, *, cost_bps: float = COST_BPS) -> pd.Series:
    frame = close.loc[close.index >= start]
    weights = pd.DataFrame({BENCHMARK: 1.0}, index=frame.index)
    returns, _ = simulate_targets(
        weights, frame,
        **sim_settings(None, cost_bps=cost_bps, leveraged_cap_pct=1.0, elevated={BENCHMARK}),
    )
    return returns


def cell_windows(close: pd.DataFrame, vehicle: str) -> dict[str, tuple[str, str] | None]:
    """Every cell label -> bounds, or None when the vehicle's bars do not
    cover the window start (not_evaluable; never synthesised)."""
    first = close[vehicle].first_valid_index()
    end = close.index[-1]
    cells: dict[str, tuple[str, str] | None] = {
        "full": (first.date().isoformat(), end.date().isoformat()),
    }
    for label, bounds in windows.SCREEN_WINDOWS.items():
        cells[label] = bounds if first <= pd.Timestamp(bounds[0]) else None
    for label, bounds in windows.STRESS_WINDOWS.items():
        cells[f"stress:{label}"] = bounds if first <= pd.Timestamp(bounds[0]) else None
    return cells


def _align(bench: pd.Series, cand: pd.Series, bounds) -> tuple[pd.Series, pd.Series]:
    b = windows.slice_window(bench, bounds).dropna()
    c = windows.slice_window(cand, bounds).dropna()
    idx = b.index.intersection(c.index)
    return b.loc[idx], c.loc[idx]


def judge_cell(
    bench: pd.Series, cand: pd.Series, bounds, *, rf: pd.Series,
    bench_label: str, cand_label: str,
) -> dict:
    """Summaries for both sides plus the benchmark_beater GateResult. When
    the window is shorter than the bootstrap block (COVID crash: 24 days)
    the paired band cannot be computed and the gate is `not_evaluable`."""
    b, c = _align(bench, cand, bounds)
    out = {
        "observations": int(len(c)),
        "benchmark": returns_summary(b, bench_label, rf=rf),
        "candidate": returns_summary(c, cand_label, rf=rf),
    }
    if len(c) >= MIN_BAND_OBS:
        band = paired_drawdown_noise_pp(b, c)
        gate = passes_gate(
            out["benchmark"], out["candidate"], OBJECTIVE_CLASS, max_dd_cost_pp=band
        )
        out["gate"] = gate.to_dict()
    else:
        out["gate"] = {
            "objective_class": OBJECTIVE_CLASS,
            "verdict": "not_evaluable",
            "reason": f"{len(c)} observations < block_size {MIN_BAND_OBS}; "
                      "paired drawdown band undefined",
            "d_max_dd": round(out["candidate"]["max_dd"] - out["benchmark"]["max_dd"], 4),
        }
    return out


def sleeve_diagnostics(weights: pd.DataFrame, returns: pd.Series, spy_raw: pd.Series,
                       vehicle: str, vehicle_close: pd.Series) -> dict:
    on = weights[vehicle] > 0
    switches = on.astype(int).diff().abs().fillna(0)
    years = on.index.year
    by_year = pd.DataFrame({
        "switches": switches.groupby(years).sum(),
        "sleeve": (1 + returns).groupby(years).prod() - 1,
        "spy": (1 + spy_raw.reindex(returns.index).fillna(0)).groupby(years).prod() - 1,
        "vehicle_buy_hold": vehicle_close.reindex(returns.index).groupby(years).apply(
            lambda s: s.iloc[-1] / s.iloc[0] - 1
        ),
        "time_in_market": on.groupby(years).mean(),
    })
    # First partial year's vehicle b&h uses first bar -> last bar of the year.
    worst_year = int(by_year["switches"].idxmax())
    return {
        "time_in_market": round(float(on.mean()), 4),
        "switches_total": int(switches.sum()),
        "switches_per_year": round(float(switches.sum() / (len(on) / 252)), 2),
        "worst_whipsaw_year": {
            "year": worst_year,
            **{k: round(float(v), 4) for k, v in by_year.loc[worst_year].items()},
        },
        "realized_vol_annualized": round(float(returns.std() * np.sqrt(252)), 4),
        "calendar_years": {
            str(y): {k: round(float(v), 4) for k, v in row.items()}
            for y, row in by_year.iterrows()
        },
    }


def gate_all_cells_per_band(judged: dict) -> dict:
    """`passes_gate_all_cells`-shaped aggregate over the required cells.

    `passes_gate_all_cells` applies ONE `max_dd_cost_pp` to every cell, but
    the registration's per-cell rule computes a separate paired band on each
    cell, so each cell's `GateResult` (already produced by `passes_gate` in
    `judge_cell` with that cell's band) is aggregated here with the same
    keys and the same all-must-pass rule. A required cell the vehicle cannot
    cover is listed in `cells_not_evaluable` and is not a pass.
    """
    evaluated = [c for c in REQUIRED_CELLS if judged.get(c) is not None]
    results = [{"window": c, "profile": "base", **judged[c]["gate"]} for c in evaluated]
    return {
        "objective_class": OBJECTIVE_CLASS,
        "passed": bool(results) and all(r["passed"] for r in results),
        "any_no_effect": any(r["no_effect"] for r in results),
        "all_no_effect": bool(results) and all(r["no_effect"] for r in results),
        "cells": results,
        "cells_not_evaluable": [c for c in REQUIRED_CELLS if judged.get(c) is None],
        "note": "each cell's max_dd_cost_pp is its own paired_drawdown_noise_pp(spy_returns, candidate_returns); aggregation mirrors backtest.promotion.passes_gate_all_cells",
    }


def _stress_flag(judged: dict) -> dict:
    gfc = judged.get("stress:GFC")
    if gfc is None:
        return {"gfc_evaluable": False, "fails_stress": None,
                "note": "vehicle listed after 2007-10-09; no GFC evidence"}
    check = gfc["gate"].get("checks", {}).get("max_dd_within_noise")
    return {
        "gfc_evaluable": True,
        "fails_stress": (not check) if check is not None else None,
        "max_dd_within_noise": check,
        "benchmark_max_dd": gfc["benchmark"]["max_dd"],
        "candidate_max_dd": gfc["candidate"]["max_dd"],
        "max_dd_cost_pp": gfc["gate"].get("inputs", {}).get("max_dd_cost_pp"),
    }


def run_form(close, spy_raw, rf, *, index, vehicle, cost_bps, form) -> dict:
    """One (index, vehicle, cost, standalone|m6) block of the report."""
    if form == "standalone":
        returns, diag, weights = simulate_sleeve(close, index=index, vehicle=vehicle, cost_bps=cost_bps)
    else:
        returns, diag = simulate_m6(close, index=index, vehicle=vehicle, cost_bps=cost_bps)
        weights = None
    label = f"{form}:{index}/{vehicle}@{cost_bps:g}bps"
    spy_sim = simulate_spy_like_for_like(close, returns.index[0], cost_bps=cost_bps)
    cells = cell_windows(close, vehicle)
    judged: dict = {}
    secondary: dict = {}
    for cell, bounds in cells.items():
        if bounds is None:
            judged[cell] = None
            continue
        judged[cell] = judge_cell(
            spy_raw, returns, bounds, rf=rf,
            bench_label="SPY buy-and-hold (raw, registration benchmark)", cand_label=label,
        )
        secondary[cell] = judge_cell(
            spy_sim, returns, bounds, rf=rf,
            bench_label="SPY via simulate_targets (same settings, 80% cap)", cand_label=label,
        )
    gate_all = gate_all_cells_per_band(judged)
    out = {
        "label": label,
        "cost_bps": cost_bps,
        "from": returns.index[0].date().isoformat(),
        "to": returns.index[-1].date().isoformat(),
        "cells": judged,
        "cells_vs_simulated_spy": secondary,
        "gate_all_cells": gate_all,
        "stress": _stress_flag(judged),
        "turnover_per_year": round(float(diag["turnover"].sum() / (len(diag) / 252)), 3),
        "trades_total": int(diag["trades"].sum()),
        "trade_cost_total": round(float(diag["trade_cost"].sum()), 2),
    }
    if weights is not None:
        out["diagnostics"] = sleeve_diagnostics(
            weights, returns, spy_raw, vehicle, close[vehicle].dropna()
        )
    return out


def main() -> None:
    symbols = [BENCHMARK, "QQQ", OFF_VEHICLE] + [v for vs in INSTRUMENTS.values() for v in vs]
    close = load_close(symbols)
    rf = load_rf_daily()
    spy_raw = close[BENCHMARK].pct_change(fill_method=None)

    results: dict = {}
    for index, vehicles in INSTRUMENTS.items():
        for vehicle in vehicles:
            key = f"{index}/{vehicle}"
            print(f"running {key} ...", flush=True)
            results[key] = {
                "index": index,
                "vehicle": vehicle,
                "vehicle_first_bar": close[vehicle].first_valid_index().date().isoformat(),
                "in_registration_mixes": vehicle in MIX_VEHICLES,
                "standalone_8bps": run_form(close, spy_raw, rf, index=index, vehicle=vehicle,
                                            cost_bps=COST_BPS, form="standalone"),
                "standalone_20bps_stress": run_form(close, spy_raw, rf, index=index, vehicle=vehicle,
                                                    cost_bps=COST_BPS_STRESS, form="standalone"),
                "m6_preview_8bps": run_form(close, spy_raw, rf, index=index, vehicle=vehicle,
                                            cost_bps=COST_BPS, form="m6"),
            }

    screened = {}
    for key, block in results.items():
        if block["vehicle"] not in MIX_VEHICLES:
            continue
        for form in ("standalone_8bps", "m6_preview_8bps"):
            passed = block[form]["gate_all_cells"]["passed"]
            fails_stress = block[form]["stress"]["fails_stress"]
            screened[f"{key}:{form}"] = bool(passed and fails_stress is False)
    decision = "sleeve_screened_for_mixes" if any(screened.values()) else "sleeve_rejected"

    def _hs(block, form):
        c = block[form]["cells"]["heldout_2023_plus"]
        return c["candidate"]["excess_sharpe"], c["benchmark"]["excess_sharpe"]

    reason_bits = []
    for key, block in results.items():
        for form in ("standalone_8bps", "m6_preview_8bps"):
            g = block[form]["gate_all_cells"]
            hs = _hs(block, form)
            reason_bits.append(
                f"{key} {form}: all-cells {'PASS' if g['passed'] else 'FAIL'} "
                f"(heldout excess Sharpe {hs[0]:.2f} vs SPY {hs[1]:.2f}; "
                f"fails_stress={block[form]['stress']['fails_stress']})"
            )
    reason = (
        f"Decision rule: screened if any SSO/QLD form (standalone or M6 preview) passes "
        f"benchmark_beater in full+heldout+early at 8 bps and is not flagged fails_stress on GFC. "
        f"Outcomes: {'; '.join(reason_bits)}. "
        + ("At least one registration-eligible form cleared every required cell, so the sleeve "
           "proceeds to the mix study, which makes the promotion call at the portfolio level."
           if decision == "sleeve_screened_for_mixes" else
           "No registration-eligible form cleared every required cell within the paired "
           "drawdown band, so the sleeve does not proceed to the mix study.")
    )

    payload = {
        "pre_registration": REGISTRATION,
        "study": "trend_leveraged_index_study",
        "objective_class": OBJECTIVE_CLASS,
        "benchmark": {
            "primary_gated": "raw SPY close-to-close total return (Tiingo adjusted close, zero cost) — the registration benchmark",
            "secondary_reported": "SPY at weight 1.0 through simulate_targets with identical settings (80% elevated cap, 20% band, $25 min notional, same cost bps); its gate is in cells_vs_simulated_spy and is NOT judged",
            "rf": "backtest.riskfree.load_rf_daily() (BIL daily total return)",
        },
        "signal": "index close[t-1] > SMA200[t-1] (engine.portfolio.trend_targets iloc[-2] convention); ON -> vehicle, OFF -> BIL (cash before 2007-05-30)",
        "grid": INSTRUMENTS,
        "data_end": close.index[-1].date().isoformat(),
        "frozen_window_untouched": windows.FROZEN_START,
        "simulator": {
            "module": "backtest.deployable_sim.simulate_targets",
            "standalone": {
                "equity": EQUITY, "gross_leverage": 1.0, "rebalance_band": 0.20,
                "band_equity_cap": 0.05, "min_order_notional": 25.0,
                "elevated_cap": {"symbols": "{vehicle, BIL}", "pct": ELEVATED_CAP_PCT},
                "leveraged_symbols": "{vehicle}",
                "leveraged_cap_pct": 1.0,
                "leveraged_cap_note": "the 0.20 leveraged cap is a portfolio-level control; it is set to 1.0 standalone so the sleeve can be characterised at full weight, and enforced at 0.20 in the mix study and in m6_preview_8bps",
                "effective_exposure_note": "sleeve weight 1.0 standalone is throttled to 80% vehicle + 20% idle cash by the elevated cap; characterisation only, the registration limits the sleeve to <= 0.20 in mixes",
                "cost_bps": [COST_BPS, COST_BPS_STRESS],
            },
            "m6_preview": {
                "weights": M6, "leveraged_cap_pct": LEVERAGED_CAP_PCT,
                "elevated_cap": {"symbols": "{SPY, vehicle, BIL}", "pct": ELEVATED_CAP_PCT},
                "cost_bps": COST_BPS,
            },
        },
        "cells": {
            "required_for_gate": list(REQUIRED_CELLS),
            "stress": "every backtest.windows.STRESS_WINDOWS row the vehicle's real bars cover; UPRO (2009-06-25) and TQQQ (2010-02-11) have no GFC row and are not_evaluable there — never synthesised",
            "per_cell_rule": "passes_gate(objective_class='benchmark_beater', max_dd_cost_pp=paired_drawdown_noise_pp(spy_returns, candidate_returns)) on that cell; windows shorter than 63 observations (COVID crash) cannot carry a paired band and are marked not_evaluable",
        },
        "decision_rule": (
            "declared before results: sleeve_screened_for_mixes if any SSO/QLD form "
            "(standalone_8bps or m6_preview_8bps) has gate_all_cells.passed and stress.fails_stress == false; "
            "else sleeve_rejected. 3x vehicles are reported standalone only."
        ),
        "decision": decision,
        "decision_reason": reason,
        "screen_outcomes": screened,
        "results": results,
        "limitations": [
            "Daily-reset (volatility) decay and the leveraged ETFs' expense ratios are embedded in the real ETF bars; nothing is modelled on top, and nothing is removed.",
            "Judged cells start at each vehicle's real first bar (SSO/QLD 2006-06-21, UPRO 2009-06-25, TQQQ 2010-02-11); no synthetic pre-inception series is used anywhere in this report, so the 3x vehicles carry no GFC evidence at all.",
            "Standalone rows hold 80% in a 2x/3x ETF (the elevated cap) plus 20% idle cash and set leveraged_cap_pct=1.0; they characterise the sleeve, they are not a deployable configuration. The deployable form is the sleeve at <= 0.20 inside a mix (m6_preview_8bps here).",
            "The 20% drift band with a 5%-of-equity cap and the $25 minimum order are the live gate's settings; the trend switch itself always trades (a full exit is never banded).",
            "BIL underlies both the OFF leg and the rf series; before 2007-05-30 the OFF leg is idle cash earning nothing, which understates the sleeve's 2006-2007 return relative to a cash-yielding account.",
            "heldout_2023_plus has arbitrated many prior studies and is a screen, not a clean hold-out; 2026-08-13 onward was not read.",
            "The like-for-like simulated-SPY benchmark is throttled by the same 80% cap, so it is a weaker benchmark than raw SPY; it is reported for the simulator's own effect and is not the gated comparison.",
        ],
        "reproduce": [
            ".venv/bin/python -m backtest.trend_leveraged_index_study",
            ".venv/bin/python -m pytest tests/test_trend_leveraged_index_study.py -q",
        ],
    }
    REPORT.write_text(json.dumps(payload, indent=2, default=_json_default))
    print(f"Decision: {decision}")
    print(json.dumps(screened, indent=2))
    print(f"Wrote {REPORT}")


def _json_default(obj):
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.date().isoformat()
    raise TypeError(f"not JSON serialisable: {type(obj)}")


if __name__ == "__main__":
    main()
