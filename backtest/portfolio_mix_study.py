"""Pre-registered portfolio mix study: the six mixes declared in
`reports/absolute_return_campaign_registration.json`, each run through the
live-gate-faithful simulator at $10k and judged as `benchmark_beater`
against raw SPY, cell by cell, exactly as the registration's pass and
selection rules say.

Every sleeve frame comes from its own study module at sleeve weight 1.0
and is scaled by the mix weight here; the sleeves' standalone verdicts
(`reports/{trend_leveraged_index,long_only_momentum,haa}_study.json`) do
not pre-empt the mix verdict — AGENTS.md's second filter is that a stream
is judged by its marginal effect on THIS portfolio, so a sleeve that fails
standalone is still run inside every registered mix that names it. The
registration fixes the list of mixes; nothing is added here.

Cells and judging follow the sleeve studies: full = the latest first-live
day among the mix's sleeves through 2026-08-12; the two screen windows;
every stress window the mix's instruments cover (a mix containing the
stock-momentum sleeve covers none). Each cell's band is its own
`paired_drawdown_noise_pp(spy, mix)`. A mix passes if it passes every
required cell and is not `fails_stress` on the GFC. Selection: highest
heldout excess Sharpe among passers, ties within 0.02 to the lower
full-sample max drawdown; no passer -> `no_candidate_passed`.

Gross 1.0 is judged (base). Gross 2.0 (the 2x lab) is reported with the
2x profile's caps and margin financing, not judged — the lab runs the
overlay-scaled version of whatever base adopts.

Reproduce
---------
    .venv/bin/python -m backtest.portfolio_mix_study     # ~5-10 minutes
    .venv/bin/python -m pytest tests/test_portfolio_mix_study.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import windows
from backtest.deployable_sim import simulate_targets
from backtest.production_portfolio import returns_summary
from backtest.riskfree import load_rf_daily
from backtest.trend_leveraged_index_study import (
    _align,
    _json_default,
    _stress_flag,
    gate_all_cells_per_band,
    judge_cell,
    load_close as load_history_close,
    target_weights as lev_trend_weights,
)
from backtest import haa_study, long_only_momentum_study as mom_study

REGISTRATION = "reports/absolute_return_campaign_registration.json"
REPORT = Path("reports/portfolio_mix_study.json")
OBJECTIVE_CLASS = "benchmark_beater"
BENCHMARK = "SPY"
EQUITY = 10_000.0

MIXES: dict[str, dict[str, float]] = {
    "M1": {"equity_core": 0.40, "lev_trend": 0.20, "mom_long": 0.20, "haa": 0.20},
    "M2": {"equity_core": 0.55, "lev_trend": 0.20, "haa": 0.25},
    "M3": {"equity_core": 0.60, "lev_trend": 0.20, "mom_long": 0.20},
    "M4": {"equity_core": 0.50, "lev_trend": 0.20, "mom_long": 0.15, "haa": 0.15},
    "M5": {"haa": 0.50, "lev_trend": 0.20, "mom_long": 0.30},
    "M6": {"equity_core": 0.80, "lev_trend": 0.20},
}
BASELINE = {"equity_core": 0.55, "tsmom": 0.25, "trend": 0.20}
LEV_VARIANTS = {"SPY/SSO": ("SPY", "SSO"), "QQQ/QLD": ("QQQ", "QLD")}
PROFILES = {
    "base": dict(gross_leverage=1.0, position_cap_pct=0.15, elevated_pct=0.80, leveraged_cap_pct=0.20),
    "2x": dict(gross_leverage=2.0, position_cap_pct=0.30, elevated_pct=1.60, leveraged_cap_pct=0.40),
}
ETF_COST_BPS = 8.0
STOCK_COST_BPS = 15.0
REQUIRED_CELLS = ("full", "heldout_2023_plus", "early_2020_2022")
SELECTION_TIE_PP = 0.02


# --------------------------------------------------------------------------
# Sleeve frames (each at weight 1.0)
# --------------------------------------------------------------------------


def sleeve_frames() -> tuple[dict[str, pd.DataFrame], pd.DataFrame, set[str], dict[str, pd.Timestamp]]:
    """Returns (frames by sleeve/variant key, combined close panel,
    etf symbols, first-live day per sleeve key)."""
    etfs = sorted({"SPY", "QQQ", "SSO", "QLD", "BIL"} | set(haa_study.INSTRUMENTS))
    hist = load_history_close(etfs)
    close_panel, volume_panel = mom_study.load_panel()

    frames: dict[str, pd.DataFrame] = {}
    first: dict[str, pd.Timestamp] = {}

    core = pd.DataFrame({BENCHMARK: 1.0}, index=hist.index)
    core.loc[hist[BENCHMARK].isna(), BENCHMARK] = 0.0
    frames["equity_core"] = core
    first["equity_core"] = hist[BENCHMARK].first_valid_index()

    for key, (index, vehicle) in LEV_VARIANTS.items():
        w = lev_trend_weights(hist, index=index, vehicle=vehicle)
        frames[f"lev_trend:{key}"] = w
        first[f"lev_trend:{key}"] = hist[vehicle].first_valid_index()

    haa = haa_study.target_weights(hist)
    frames["haa"] = haa
    first["haa"] = haa.index[(haa.sum(axis=1) > 0).to_numpy().argmax()]

    mom = mom_study.target_weights(close_panel, volume_panel, top_n=20)
    frames["mom_long"] = mom
    first["mom_long"] = mom.index[(mom.sum(axis=1) > 0).to_numpy().argmax()]

    # Baseline ingredients (shipped 2026-09-14 portfolio) for the comparison row.
    from backtest.production_portfolio import build_streams  # noqa: WPS433 (diagnostic only)
    frames["_baseline_streams"] = build_streams()

    # Combined close panel on the union of dates; stock columns only where used.
    stock_cols = [c for c in mom.columns if c not in hist.columns]
    close = hist.join(close_panel[stock_cols], how="outer").sort_index()
    return frames, close, set(etfs), first


def mix_weights(mix: dict[str, float], frames: dict[str, pd.DataFrame], lev_key: str,
                index: pd.DatetimeIndex) -> pd.DataFrame:
    total: pd.DataFrame | None = None
    for sleeve, w in mix.items():
        frame = frames[f"lev_trend:{lev_key}"] if sleeve == "lev_trend" else frames[sleeve]
        part = frame.reindex(index).ffill().fillna(0.0) * w
        total = part if total is None else total.add(part, fill_value=0.0)
    assert total is not None
    return total.fillna(0.0)


def cost_map(symbols, etfs: set[str]) -> dict[str, float]:
    return {s: (ETF_COST_BPS if s in etfs else STOCK_COST_BPS) for s in symbols}


def simulate_mix(weights: pd.DataFrame, close: pd.DataFrame, *, profile: str, vehicle: str,
                 etfs: set[str]):
    p = PROFILES[profile]
    return simulate_targets(
        weights, close[weights.columns], equity=EQUITY,
        gross_leverage=p["gross_leverage"],
        position_cap_pct=p["position_cap_pct"],
        elevated_cap=(etfs, p["elevated_pct"]),
        leveraged_symbols={vehicle}, leveraged_cap_pct=p["leveraged_cap_pct"],
        cost_bps_by_symbol=cost_map(weights.columns, etfs),
    )


def cell_windows(first_live: pd.Timestamp, end: pd.Timestamp, stress_ok: bool) -> dict:
    cells: dict = {"full": (first_live.date().isoformat(), end.date().isoformat())}
    for label, bounds in windows.SCREEN_WINDOWS.items():
        cells[label] = (
            (max(first_live, pd.Timestamp(bounds[0])).date().isoformat(), bounds[1])
            if first_live <= pd.Timestamp(bounds[1]) else None
        )
    for label, bounds in windows.STRESS_WINDOWS.items():
        cells[f"stress:{label}"] = bounds if (stress_ok and first_live <= pd.Timestamp(bounds[0])) else None
    return cells


def judge(returns: pd.Series, spy_raw: pd.Series, rf: pd.Series, cells: dict, label: str) -> dict:
    judged = {name: (judge_cell(spy_raw, returns, bounds, rf=rf, bench_label=BENCHMARK, cand_label=label)
                     if bounds is not None else None)
              for name, bounds in cells.items()}
    return {"cells": judged, "gate_all_cells": gate_all_cells_per_band(judged), "stress": _stress_flag(judged)}


def calendar_years(cand: pd.Series, bench: pd.Series) -> dict:
    b = bench.reindex(cand.index).fillna(0.0)
    years = cand.index.year
    return {str(y): {"mix": round(float((1 + cand[years == y]).prod() - 1), 4),
                     "spy": round(float((1 + b[years == y]).prod() - 1), 4)}
            for y in sorted(set(years))}


def baseline_returns(streams: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """The shipped 2026-09-14 portfolio from production_portfolio's own
    streams (idealised, close-to-close, not through the simulator): a
    like-for-like-enough comparison row, never judged."""
    s = streams.copy()
    s.index = pd.DatetimeIndex(s.index)
    if s.index.tz is not None:
        s.index = s.index.tz_convert("UTC").tz_localize(None)
    s.index = s.index.normalize()
    names = {"equity_core": "spy"}  # production_portfolio's column for the SPY core
    cols = {names.get(k, k): v for k, v in weights.items() if names.get(k, k) in s.columns}
    r = sum(s[k].fillna(0.0) * v for k, v in cols.items())
    return r


def main() -> None:
    frames, close, etfs, first = sleeve_frames()
    spy_raw = close[BENCHMARK].pct_change(fill_method=None)
    rf = load_rf_daily()
    end = pd.Timestamp(windows.HELDOUT[1])
    close = close.loc[close.index <= end]

    report: dict = {
        "study": "portfolio_mix",
        "pre_registration": REGISTRATION,
        "objective_class": OBJECTIVE_CLASS,
        "benchmark": "raw SPY close-to-close total return, zero cost",
        "sleeve_first_live_day": {k: v.date().isoformat() for k, v in first.items()},
        "sleeve_standalone_decisions": {
            name: json.loads(Path(f"reports/{name}_study.json").read_text()).get("decision")
            for name in ("trend_leveraged_index", "long_only_momentum", "haa")
            if Path(f"reports/{name}_study.json").exists()
        },
        "mixes": {},
    }

    for mix_name, mix in MIXES.items():
        for lev_key, (index, vehicle) in LEV_VARIANTS.items():
            keys = [f"lev_trend:{lev_key}" if s == "lev_trend" else s for s in mix]
            first_live = max(first[k] for k in keys)
            stress_ok = "mom_long" not in mix
            weights = mix_weights(mix, frames, lev_key, close.index)
            weights = weights.loc[weights.index >= first_live]
            label = f"{mix_name}[{lev_key}]"
            entry: dict = {"weights": mix, "lev_trend_variant": lev_key, "first_live_day": first_live.date().isoformat()}
            for profile in PROFILES:
                returns, diag = simulate_mix(weights, close, profile=profile, vehicle=vehicle, etfs=etfs)
                cells = cell_windows(first_live, close.index[-1], stress_ok)
                res = judge(returns, spy_raw, rf, cells, label)
                live_diag = diag.loc[diag.index >= first_live]
                res["diagnostics"] = {
                    "realized_vol_annualized": round(float(returns.loc[returns.index >= first_live].std() * np.sqrt(252)), 4),
                    "annual_turnover_one_way": round(float(live_diag["turnover"].groupby(live_diag.index.year).sum().mean()), 2),
                    "avg_gross_exposure_pct": round(float((live_diag["gross_exposure"] / live_diag["equity"]).mean()), 4),
                    "calendar_years": calendar_years(returns.loc[returns.index >= first_live], spy_raw),
                }
                entry[profile] = res
            report["mixes"][label] = entry
            g = entry["base"]["gate_all_cells"]
            print(f"{label}: base passed={g['passed']} fails_stress={entry['base']['stress'].get('fails_stress')} | " + " | ".join(
                f"{c}: {v['candidate']['cagr']:.1%}/{v['candidate'].get('excess_sharpe', float('nan')):.2f}/{v['candidate']['max_dd']:.1%} vs "
                f"{v['benchmark']['cagr']:.1%}/{v['benchmark'].get('excess_sharpe', float('nan')):.2f}/{v['benchmark']['max_dd']:.1%} "
                f"[{v['gate'].get('passed', v['gate'].get('verdict'))}]"
                for c, v in entry["base"]["cells"].items() if v is not None and c in REQUIRED_CELLS))

    # Baseline comparison row (shipped 2026-09-14 portfolio, idealised streams).
    base_r = baseline_returns(frames["_baseline_streams"], BASELINE)
    base_r = base_r.loc[base_r.index <= end]
    # production_portfolio's streams start with the cross-sectional panel
    # (2020-07-28), so the baseline covers no stress window.
    base_first = base_r.index[0]
    base_cells = cell_windows(base_first, close.index[-1], False)
    report["baseline_2026_09_14"] = {
        "weights": BASELINE,
        "construction": "production_portfolio.build_streams() sleeve streams (idealised close-to-close, 8/15 bps), NOT the simulator; comparison only",
        **{k: v for k, v in judge(base_r, spy_raw, rf, base_cells, "baseline").items()},
    }

    # Selection.
    passers = []
    for label, entry in report["mixes"].items():
        res = entry["base"]
        if res["gate_all_cells"]["passed"] and not bool(res["stress"].get("fails_stress")):
            heldout = res["cells"]["heldout_2023_plus"]["candidate"]
            passers.append((label, float(heldout["excess_sharpe"]), float(res["cells"]["full"]["candidate"]["max_dd"])))
    chosen = None
    if passers:
        passers.sort(key=lambda t: -t[1])
        top = passers[0][1]
        tied = [p for p in passers if top - p[1] <= SELECTION_TIE_PP]
        tied.sort(key=lambda t: -t[2])  # max_dd is negative; larger = shallower
        chosen = tied[0][0]
    report["passing_mixes"] = [{"mix": p[0], "heldout_excess_sharpe": p[1], "full_max_dd": p[2]} for p in passers]
    report["decision"] = f"select:{chosen}" if chosen else "no_candidate_passed"
    report["selection_rule"] = (
        "highest heldout_2023_plus excess Sharpe among mixes passing every required cell at gross 1.0 and not "
        f"fails_stress on the GFC; ties within {SELECTION_TIE_PP} to the shallower full-sample max drawdown"
    )
    report["limitations"] = [
        "Mixes with the stock-momentum sleeve start 2021-08-26 (panel warm-up) and cover no stress window; their early cell is 2021-08-26..2022-12-30.",
        "The stock-momentum sleeve is survivors-only and an upper bound; any mix it lifts is optimistic.",
        "The baseline row uses production_portfolio's idealised streams, not the simulator, and is not judged.",
        "Gross 2.0 rows use the 2x profile's caps and 5% margin financing but not the (now active) volatility overlay.",
        "Passing on a 2023+ window whose SPY excess Sharpe is 1.16 is a high bar; a thin margin (a few hundredths) is inside selection noise and is why Phase 4's paper validation, not this study, decides promotion.",
    ]
    report["reproduce"] = [".venv/bin/python -m backtest.portfolio_mix_study",
                           ".venv/bin/python -m pytest tests/test_portfolio_mix_study.py -q"]
    REPORT.write_text(json.dumps(report, indent=2, default=_json_default))
    print("passing:", report["passing_mixes"])
    print("decision:", report["decision"])


if __name__ == "__main__":
    main()
